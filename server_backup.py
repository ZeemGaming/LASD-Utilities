"""Guild backup storage and best-effort Discord server-state restore."""

import asyncio
import copy
import datetime
import io
import json
import os
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord


BACKUP_FILE = Path(__file__).with_name("backup.json")
AUTO_BACKUP_RETENTION = 168
BACKUP_REASON = "Restore from LASD Utilities server backup"
SAFE_ASSET_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}


class BackupStoreError(Exception):
    """Raised when the local backup store cannot be read or written safely."""


class BackupPermissionError(Exception):
    """Raised when the bot lacks permissions required for a restore."""


def _value(value):
    return getattr(value, "value", value)


def _text(value):
    return str(value) if value is not None else None


def _record_error(report: dict, label: str, error: Exception) -> None:
    status = getattr(error, "status", None)
    detail = f"HTTP {status}" if status else type(error).__name__
    if len(report["errors"]) < 50:
        report["errors"].append(f"{label}: {detail}")
    else:
        report["errors_omitted"] += 1


def _record_capture_warning(warnings: list, label: str, error: Exception) -> None:
    status = getattr(error, "status", None)
    detail = f"HTTP {status}" if status else type(error).__name__
    if len(warnings) < 50:
        warnings.append(f"{label}: {detail}")
    else:
        warnings.append("Additional capture warnings omitted.")


def _serialize_overwrites(channel) -> list:
    records = []
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Role):
            target_type = "role"
            target_name = target.name
        elif isinstance(target, discord.Member):
            target_type = "member"
            target_name = None
        else:
            continue

        allow, deny = overwrite.pair()
        records.append({
            "target_type": target_type,
            "target_id": str(target.id),
            "target_name": target_name,
            "allow": allow.value,
            "deny": deny.value,
        })
    return records


def _channel_kind(channel) -> str:
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    if isinstance(channel, discord.StageChannel):
        return "stage"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    if isinstance(channel, discord.ForumChannel):
        return "forum"
    if isinstance(channel, discord.TextChannel):
        return "text"
    return "unsupported"


def _serialize_channel(channel) -> dict:
    kind = _channel_kind(channel)
    record = {
        "id": str(channel.id),
        "name": channel.name,
        "kind": kind,
        "type_value": int(channel.type.value),
        "position": int(getattr(channel, "position", 0)),
        "category_id": str(channel.category_id) if channel.category_id else None,
        "overwrites": _serialize_overwrites(channel),
    }

    if kind in {"text", "forum"}:
        record.update({
            "topic": getattr(channel, "topic", None),
            "slowmode_delay": int(getattr(channel, "slowmode_delay", 0)),
            "nsfw": bool(getattr(channel, "nsfw", False)),
            "default_auto_archive_duration": int(
                getattr(channel, "default_auto_archive_duration", 1440)
            ),
            "default_thread_slowmode_delay": int(
                getattr(channel, "default_thread_slowmode_delay", 0)
            ),
        })
    if kind == "text":
        record["news"] = bool(channel.is_news())
    if kind in {"voice", "stage"}:
        record.update({
            "bitrate": int(getattr(channel, "bitrate", 64000)),
            "user_limit": int(getattr(channel, "user_limit", 0)),
            "rtc_region": _text(getattr(channel, "rtc_region", None)),
            "nsfw": bool(getattr(channel, "nsfw", False)),
            "video_quality_mode": _value(
                getattr(channel, "video_quality_mode", None)
            ),
        })
    if kind == "stage":
        record["topic"] = getattr(channel, "topic", None)
    if kind == "forum":
        record["available_tags"] = [
            {
                "name": tag.name,
                "emoji": _text(tag.emoji),
                "moderated": bool(tag.moderated),
            }
            for tag in channel.available_tags
        ]
        record["default_reaction_emoji"] = _text(
            getattr(channel, "default_reaction_emoji", None)
        )
        record["default_sort_order"] = _value(
            getattr(channel, "default_sort_order", None)
        )
        record["default_layout"] = _value(
            getattr(channel, "default_layout", None)
        )
    return record


def _serialize_automod_rule(rule) -> dict:
    trigger = rule.trigger
    actions = []
    for action in rule.actions:
        actions.append({
            "type": _value(action.type),
            "channel_id": str(action.channel_id) if action.channel_id else None,
            "duration_seconds": (
                action.duration.total_seconds() if action.duration else None
            ),
            "custom_message": action.custom_message,
        })

    return {
        "id": str(rule.id),
        "name": rule.name,
        "event_type": _value(rule.event_type),
        "enabled": bool(rule.enabled),
        "trigger": {
            "type": _value(trigger.type),
            "keyword_filter": list(trigger.keyword_filter or []),
            "regex_patterns": list(trigger.regex_patterns or []),
            "presets": _value(trigger.presets),
            "allow_list": list(trigger.allow_list or []),
            "mention_limit": getattr(trigger, "mention_limit", None),
            "mention_raid_protection": getattr(
                trigger, "mention_raid_protection", None
            ),
        },
        "actions": actions,
        "exempt_roles": [str(role.id) for role in rule.exempt_roles],
        "exempt_channels": [str(channel.id) for channel in rule.exempt_channels],
    }


def _serialize_guild_settings(guild) -> dict:
    def channel_id(name):
        channel = getattr(guild, name, None)
        return str(channel.id) if channel else None

    def asset_url(name):
        asset = getattr(guild, name, None)
        return str(asset.url) if asset else None

    return {
        "name": guild.name,
        "description": getattr(guild, "description", None),
        "verification_level": _value(
            getattr(guild, "verification_level", None)
        ),
        "default_notifications": _value(
            getattr(guild, "default_notifications", None)
        ),
        "explicit_content_filter": _value(
            getattr(guild, "explicit_content_filter", None)
        ),
        "afk_timeout": getattr(guild, "afk_timeout", None),
        "afk_channel_id": channel_id("afk_channel"),
        "system_channel_id": channel_id("system_channel"),
        "rules_channel_id": channel_id("rules_channel"),
        "public_updates_channel_id": channel_id("public_updates_channel"),
        "widget_channel_id": channel_id("widget_channel"),
        "system_channel_flags": _value(
            getattr(guild, "system_channel_flags", None)
        ),
        "preferred_locale": _value(getattr(guild, "preferred_locale", None)),
        "premium_progress_bar_enabled": getattr(
            guild, "premium_progress_bar_enabled", None
        ),
        "widget_enabled": getattr(guild, "widget_enabled", None),
        "features": list(getattr(guild, "features", []) or []),
        "icon_url": asset_url("icon"),
        "banner_url": asset_url("banner"),
        "splash_url": asset_url("splash"),
        "discovery_splash_url": asset_url("discovery_splash"),
    }


def _deserialize_automod_presets(value):
    value = int(value or 0)
    return discord.AutoModPresets(
        profanity=bool(value & 1),
        sexual_content=bool(value & 2),
        slurs=bool(value & 4),
    )


class ServerBackupManager:
    """Persist per-guild snapshots in one private, atomically written JSON file."""

    def __init__(self, path: Path = BACKUP_FILE):
        self.path = Path(path)
        self._lock = asyncio.Lock()

    def _read_store(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "guilds": {}}
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise BackupStoreError(
                f"Could not read {self.path.name}; the file may be damaged."
            ) from error
        if not isinstance(data, dict) or not isinstance(data.get("guilds"), dict):
            raise BackupStoreError(f"{self.path.name} has an invalid format.")
        return data

    def _write_store(self, data: dict) -> None:
        temp_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".backup-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = Path(file.name)
                os.chmod(temp_path, 0o600)
                json.dump(data, file, indent=2, ensure_ascii=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self.path)
            os.chmod(self.path, 0o600)
        except OSError as error:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise BackupStoreError(
                f"Could not safely write {self.path.name}."
            ) from error

    async def initialize(self) -> None:
        async with self._lock:
            if self.path.exists():
                try:
                    await asyncio.to_thread(os.chmod, self.path, 0o600)
                except OSError as error:
                    raise BackupStoreError(
                        f"Could not secure {self.path.name}."
                    ) from error
                return
            await asyncio.to_thread(
                self._write_store, {"version": 1, "guilds": {}}
            )

    async def list_backups(self, guild_id: int) -> list:
        async with self._lock:
            data = await asyncio.to_thread(self._read_store)
        guild_data = data["guilds"].get(str(guild_id), {})
        return [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "created_at": item.get("created_at"),
                "type": item.get("type"),
            }
            for item in guild_data.get("backups", [])
        ]

    async def find_backup(self, guild_id: int, identifier: str) -> dict | None:
        async with self._lock:
            data = await asyncio.to_thread(self._read_store)
        backups = data["guilds"].get(str(guild_id), {}).get("backups", [])
        for item in reversed(backups):
            if item.get("id") == identifier or item.get("name") == identifier:
                return copy.deepcopy(item)
        return None

    async def create_backup(
        self,
        guild,
        name: str | None = None,
        *,
        automatic: bool = False,
    ) -> dict:
        snapshot = await self._capture_guild(guild)
        now = datetime.datetime.now(datetime.timezone.utc)
        created_at = now.isoformat(timespec="seconds")
        timestamp_label = now.strftime("%Y-%m-%d %H:%M UTC")
        guild_key = str(guild.id)

        async with self._lock:
            store = await asyncio.to_thread(self._read_store)
            guild_data = store["guilds"].setdefault(
                guild_key,
                {"guild_name": guild.name, "next_auto_backup": 1, "backups": []},
            )
            guild_data["guild_name"] = guild.name
            if automatic:
                number = int(guild_data.get("next_auto_backup", 1))
                guild_data["next_auto_backup"] = number + 1
                backup_name = f"{timestamp_label} Auto Backup: {number}"
                backup_type = "auto"
            else:
                clean_name = (name or "").strip()[:80]
                if not clean_name:
                    raise ValueError("A backup name is required.")
                backup_name = f"{clean_name} - {timestamp_label}"
                backup_type = "manual"

            entry = {
                "id": uuid.uuid4().hex[:12],
                "name": backup_name,
                "created_at": created_at,
                "type": backup_type,
                "data": snapshot,
            }
            backups = guild_data.setdefault("backups", [])
            backups.append(entry)
            auto_entries = [item for item in backups if item.get("type") == "auto"]
            if len(auto_entries) > AUTO_BACKUP_RETENTION:
                keep_ids = {
                    item["id"]
                    for item in sorted(
                        auto_entries, key=lambda item: item.get("created_at", "")
                    )[-AUTO_BACKUP_RETENTION:]
                }
                guild_data["backups"] = [
                    item for item in backups
                    if item.get("type") != "auto" or item.get("id") in keep_ids
                ]
            await asyncio.to_thread(self._write_store, store)
        return copy.deepcopy(entry)

    async def _capture_guild(self, guild) -> dict:
        warnings = []
        roles = []
        channels = []
        members = []
        bots = []
        automod_rules = []
        webhooks = []
        emojis = []
        stickers = []
        integrations = []
        channels_complete = True
        member_roles_complete = True
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            _record_capture_warning(
                warnings, "Bot permissions", ValueError("bot member is unavailable")
            )
        else:
            bot_permissions = bot_member.guild_permissions
            for attribute, label in (
                ("manage_roles", "Manage Roles"),
                ("manage_channels", "Manage Channels"),
                ("manage_guild", "Manage Server"),
                ("manage_webhooks", "Manage Webhooks"),
                ("manage_emojis_and_stickers", "Manage Expressions"),
            ):
                if not getattr(bot_permissions, attribute, False):
                    warnings.append(f"Bot lacks {label}; related restore features may be unavailable.")

        for role in guild.roles:
            roles.append({
                "id": str(role.id),
                "name": role.name,
                "permissions": int(role.permissions.value),
                "color": int(role.color.value),
                "hoist": bool(role.hoist),
                "mentionable": bool(role.mentionable),
                "position": int(role.position),
                "managed": bool(role.managed),
                "is_default": bool(role.is_default()),
            })

        for channel in guild.channels:
            try:
                channels.append(_serialize_channel(channel))
            except Exception as error:
                channels_complete = False
                _record_capture_warning(
                    warnings, f"Channel {getattr(channel, 'name', 'unknown')}",
                    error,
                )

        try:
            async for member in guild.fetch_members(limit=None):
                member_roles = [
                    {"id": str(role.id), "name": role.name}
                    for role in member.roles
                    if not role.is_default()
                ]
                members.append({
                    "id": str(member.id),
                    "roles": member_roles,
                })
                if member.bot:
                    bots.append({
                        "id": str(member.id),
                        "name": member.name,
                        "display_name": member.display_name,
                    })
        except Exception as error:
            member_roles_complete = False
            _record_capture_warning(warnings, "Member role mappings", error)

        try:
            for rule in await guild.fetch_automod_rules():
                automod_rules.append(_serialize_automod_rule(rule))
        except Exception as error:
            _record_capture_warning(warnings, "AutoMod rules", error)

        webhook_channels = [
            channel for channel in guild.channels
            if callable(getattr(channel, "webhooks", None))
        ]
        for channel in webhook_channels:
            try:
                for webhook in await channel.webhooks():
                    # Webhook tokens are credentials and are intentionally never
                    # copied into the backup file.
                    webhooks.append({
                        "id": str(webhook.id),
                        "name": webhook.name,
                        "channel_id": str(channel.id),
                        "type": _value(webhook.type),
                        "user_id": (
                            str(webhook.user.id) if webhook.user else None
                        ),
                        "avatar_url": (
                            str(webhook.avatar.url) if webhook.avatar else None
                        ),
                    })
            except Exception as error:
                _record_capture_warning(
                    warnings, f"Webhooks in #{channel.name}", error
                )

        for emoji in guild.emojis:
            emojis.append({
                "id": str(emoji.id),
                "name": emoji.name,
                "animated": bool(emoji.animated),
                "available": bool(emoji.available),
                "roles": [
                    {"id": str(role.id), "name": role.name}
                    for role in emoji.roles
                ],
                "asset_url": str(emoji.url),
            })

        try:
            for sticker in await guild.fetch_stickers():
                stickers.append({
                    "id": str(sticker.id),
                    "name": sticker.name,
                    "description": sticker.description,
                    "emoji": sticker.emoji,
                    "format": _value(sticker.format),
                    "available": bool(sticker.available),
                    "asset_url": str(sticker.url),
                })
        except Exception as error:
            _record_capture_warning(warnings, "Stickers", error)

        try:
            for integration in await guild.integrations():
                application = getattr(integration, "application", None)
                account = getattr(integration, "account", None)
                integrations.append({
                    "id": str(integration.id),
                    "name": integration.name,
                    "type": _value(integration.type),
                    "enabled": bool(integration.enabled),
                    "account": {
                        "id": str(account.id) if account else None,
                        "name": getattr(account, "name", None),
                    },
                    "application": {
                        "id": (
                            str(application.id)
                            if application and getattr(application, "id", None)
                            else None
                        ),
                        "name": getattr(application, "name", None),
                    },
                })
        except Exception as error:
            _record_capture_warning(warnings, "Integrations", error)

        return {
            "guild_id": str(guild.id),
            "guild_name": guild.name,
            "guild_settings": _serialize_guild_settings(guild),
            "captured_at": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(timespec="seconds"),
            "roles": roles,
            "channels": channels,
            "member_roles": members,
            "automod_rules": automod_rules,
            "webhooks": webhooks,
            "emojis": emojis,
            "stickers": stickers,
            "integrations": integrations,
            "bots": bots,
            "channels_complete": channels_complete,
            "member_roles_complete": member_roles_complete,
            "capture_warnings": warnings,
            "limitations": [
                "Webhook credentials are not stored; only webhook metadata is backed up.",
                "Bot and integration metadata is informational; Discord does not allow their automatic reinstallation.",
                "Emoji and sticker media are referenced by Discord CDN URL, not copied into backup.json.",
                "Threads, messages, and pins are not part of this server-structure backup.",
                "Unsupported channel types are recorded but cannot be recreated if deleted.",
            ],
        }

    async def restore(
        self,
        guild,
        backup: dict,
        *,
        delete_newer: bool = False,
    ) -> dict:
        snapshot = backup.get("data", {})
        if str(snapshot.get("guild_id")) != str(guild.id):
            raise ValueError("This backup does not belong to this server.")
        if not isinstance(snapshot.get("roles"), list) or not isinstance(
            snapshot.get("channels"), list
        ):
            raise ValueError("This backup is missing its role or channel snapshot.")

        bot_member = guild.me
        if bot_member is None:
            raise BackupPermissionError("The bot's server permissions are unavailable.")
        permissions = bot_member.guild_permissions
        missing_required = []
        if not permissions.manage_roles:
            missing_required.append("Manage Roles")
        if not permissions.manage_channels:
            missing_required.append("Manage Channels")
        if missing_required:
            raise BackupPermissionError(
                "The bot needs " + " and ".join(missing_required) + " to restore."
            )

        report = {
            "created": {},
            "updated": {},
            "deleted": {},
            "skipped": {},
            "errors": [],
            "errors_omitted": 0,
            "warnings": list(snapshot.get("capture_warnings", [])),
            "limitations": list(snapshot.get("limitations", [])),
            "delete_newer": delete_newer,
        }
        original_roles = list(guild.roles)
        original_channels = list(guild.channels)
        matched_role_ids = set()
        matched_channel_ids = set()
        role_map = {}
        channel_map = {}
        member_map = {member.id: member for member in guild.members}
        current_members_complete = True

        async def fetch_members():
            nonlocal current_members_complete
            try:
                async for member in guild.fetch_members(limit=None):
                    member_map[member.id] = member
            except Exception as error:
                current_members_complete = False
                _record_capture_warning(
                    report["warnings"], "Fetching current members", error
                )

        await fetch_members()

        # Restore requests are deliberately awaited serially, not gathered in
        # parallel, so discord.py can honor each route's rate-limit bucket.
        # Roles first so permission overwrites and member mappings can resolve.
        roles_by_id = {role.id: role for role in original_roles}
        unused_roles = list(original_roles)
        role_records = sorted(
            snapshot["roles"], key=lambda item: int(item.get("position", 0))
        )
        for record in role_records:
            old_id = str(record.get("id", ""))
            if record.get("is_default") or old_id == str(guild.id):
                role = guild.default_role
                role_map[old_id] = role
                matched_role_ids.add(role.id)
                try:
                    if int(role.permissions.value) != int(record.get("permissions", 0)):
                        await role.edit(
                            permissions=discord.Permissions(
                                int(record.get("permissions", 0))
                            ),
                            reason=BACKUP_REASON,
                        )
                        report["updated"]["roles"] = (
                            report["updated"].get("roles", 0) + 1
                        )
                except (discord.HTTPException, discord.Forbidden) as error:
                    _record_error(report, "@everyone permissions", error)
                continue

            existing = roles_by_id.get(int(old_id)) if old_id.isdigit() else None
            if existing is None:
                existing = next(
                    (
                        role for role in unused_roles
                        if not role.is_default()
                        and role.name == record.get("name")
                        and role.id not in matched_role_ids
                    ),
                    None,
                )

            if record.get("managed"):
                if existing:
                    role_map[old_id] = existing
                    matched_role_ids.add(existing.id)
                else:
                    _record_capture_warning(
                        report["warnings"],
                        f"Managed role {record.get('name')}",
                        ValueError("managed roles cannot be recreated by the bot"),
                    )
                continue

            if existing:
                # A backed-up role must never be treated as "newer" and
                # deleted merely because the bot could not edit it.
                role_map[old_id] = existing
                matched_role_ids.add(existing.id)
            try:
                if existing:
                    role = await existing.edit(
                        name=record.get("name", existing.name),
                        permissions=discord.Permissions(
                            int(record.get("permissions", existing.permissions.value))
                        ),
                        color=discord.Colour(
                            int(record.get("color", existing.color.value))
                        ),
                        hoist=bool(record.get("hoist", existing.hoist)),
                        mentionable=bool(
                            record.get("mentionable", existing.mentionable)
                        ),
                        reason=BACKUP_REASON,
                    )
                    if role is None:
                        role = existing
                    report["updated"]["roles"] = (
                        report["updated"].get("roles", 0) + 1
                    )
                else:
                    role = await guild.create_role(
                        name=record.get("name", "Restored Role"),
                        permissions=discord.Permissions(
                            int(record.get("permissions", 0))
                        ),
                        color=discord.Colour(int(record.get("color", 0))),
                        hoist=bool(record.get("hoist", False)),
                        mentionable=bool(record.get("mentionable", False)),
                        reason=BACKUP_REASON,
                    )
                    report["created"]["roles"] = (
                        report["created"].get("roles", 0) + 1
                    )
                role_map[old_id] = role
                matched_role_ids.add(role.id)
            except Exception as error:
                _record_error(report, f"Role {record.get('name')}", error)

        positions = {
            role_map[record["id"]]: int(record.get("position", 1))
            for record in role_records
            if record.get("id") in role_map
            and not role_map[record["id"]].is_default()
            and not role_map[record["id"]].managed
        }
        if positions:
            try:
                await guild.edit_role_positions(positions, reason=BACKUP_REASON)
            except Exception as error:
                _record_error(report, "Role hierarchy", error)

        async def resolve_overwrites(records):
            resolved = {}
            for item in records:
                target = None
                target_id = item.get("target_id", "")
                if item.get("target_type") == "role":
                    target = role_map.get(target_id)
                    if target is None and target_id.isdigit():
                        target = guild.get_role(int(target_id))
                    if target is None and item.get("target_name"):
                        target = next(
                            (
                                role for role in guild.roles
                                if role.name == item["target_name"]
                            ),
                            None,
                        )
                elif item.get("target_type") == "member" and target_id.isdigit():
                    target = member_map.get(int(target_id))
                if target is None:
                    report["skipped"]["permission_overwrites"] = (
                        report["skipped"].get("permission_overwrites", 0) + 1
                    )
                    continue
                resolved[target] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(int(item.get("allow", 0))),
                    discord.Permissions(int(item.get("deny", 0))),
                )
            return resolved

        def current_channel_for(record, unused):
            old_id = record.get("id", "")
            by_id = next(
                (channel for channel in unused if str(channel.id) == old_id),
                None,
            )
            if by_id:
                return by_id
            kind = record.get("kind")
            return next(
                (
                    channel for channel in unused
                    if _channel_kind(channel) == kind
                    and channel.name == record.get("name")
                ),
                None,
            )

        category_records = [
            item for item in snapshot["channels"]
            if item.get("kind") == "category"
        ]
        other_records = [
            item for item in snapshot["channels"]
            if item.get("kind") != "category"
        ]
        unused_channels = list(original_channels)
        for record in category_records:
            old_id = str(record.get("id", ""))
            existing = current_channel_for(record, unused_channels)
            if existing:
                # Preserve matched channels even if an edit fails.
                channel_map[old_id] = existing
                matched_channel_ids.add(existing.id)
                unused_channels.remove(existing)
            try:
                overwrites = await resolve_overwrites(
                    record.get("overwrites", [])
                )
                if existing:
                    channel = await existing.edit(
                        name=record.get("name", existing.name),
                        position=int(record.get("position", existing.position)),
                        overwrites=overwrites,
                        reason=BACKUP_REASON,
                    )
                    channel = channel or existing
                    report["updated"]["categories"] = (
                        report["updated"].get("categories", 0) + 1
                    )
                else:
                    channel = await guild.create_category(
                        record.get("name", "Restored Category"),
                        overwrites=overwrites,
                        position=int(record.get("position", 0)),
                        reason=BACKUP_REASON,
                    )
                    report["created"]["categories"] = (
                        report["created"].get("categories", 0) + 1
                    )
                channel_map[old_id] = channel
                matched_channel_ids.add(channel.id)
            except Exception as error:
                _record_error(report, f"Category {record.get('name')}", error)

        for record in other_records:
            kind = record.get("kind")
            if kind == "unsupported":
                existing = current_channel_for(record, unused_channels)
                if existing:
                    channel_map[str(record.get("id", ""))] = existing
                    matched_channel_ids.add(existing.id)
                    unused_channels.remove(existing)
                report["skipped"]["unsupported_channels"] = (
                    report["skipped"].get("unsupported_channels", 0) + 1
                )
                continue
            old_id = str(record.get("id", ""))
            existing = current_channel_for(record, unused_channels)
            if existing:
                # Do not classify a failed-to-update backup channel as an
                # extra channel eligible for deletion.
                channel_map[old_id] = existing
                matched_channel_ids.add(existing.id)
                unused_channels.remove(existing)
            category_id = record.get("category_id")
            category = channel_map.get(str(category_id)) if category_id else None
            if category_id and category is None:
                report["warnings"].append(
                    f"Parent category for #{record.get('name')} could not be restored."
                )
            try:
                overwrites = await resolve_overwrites(
                    record.get("overwrites", [])
                )
                if existing:
                    edit_options = self._channel_edit_options(
                        record, category, overwrites
                    )
                    if category_id and category is None:
                        # Do not detach a live channel if its snapshot category
                        # could not be recreated.
                        edit_options.pop("category", None)
                    channel = await existing.edit(
                        **edit_options,
                        reason=BACKUP_REASON,
                    )
                    channel = channel or existing
                    report["updated"]["channels"] = (
                        report["updated"].get("channels", 0) + 1
                    )
                else:
                    channel = await self._create_channel(
                        guild, record, category, overwrites
                    )
                    report["created"]["channels"] = (
                        report["created"].get("channels", 0) + 1
                    )
                channel_map[old_id] = channel
                matched_channel_ids.add(channel.id)
            except Exception as error:
                _record_error(report, f"Channel {record.get('name')}", error)

        delete_newer_channels = delete_newer and snapshot.get(
            "channels_complete", True
        )
        if delete_newer and not delete_newer_channels:
            report["warnings"].append(
                "Newer channels were preserved because the backup's channel snapshot is incomplete."
            )

        if delete_newer_channels:
            extra_channels = [
                channel for channel in original_channels
                if channel.id not in matched_channel_ids
            ]
            extra_channels.sort(
                key=lambda channel: isinstance(channel, discord.CategoryChannel)
            )
            for channel in extra_channels:
                try:
                    await channel.delete(reason=BACKUP_REASON)
                    report["deleted"]["channels"] = (
                        report["deleted"].get("channels", 0) + 1
                    )
                except Exception as error:
                    _record_error(report, f"Delete channel {channel.name}", error)

            extra_roles = [
                role for role in original_roles
                if not role.is_default()
                and not role.managed
                and role.id not in matched_role_ids
            ]
            for role in extra_roles:
                try:
                    await role.delete(reason=BACKUP_REASON)
                    report["deleted"]["roles"] = (
                        report["deleted"].get("roles", 0) + 1
                    )
                except Exception as error:
                    _record_error(report, f"Delete role {role.name}", error)

        await self._restore_guild_settings(
            guild, snapshot, channel_map, report
        )
        await self._restore_automod(guild, snapshot, role_map, channel_map, report)
        await self._restore_webhooks(guild, snapshot, channel_map, report)
        await self._restore_assets(guild, snapshot, role_map, report)
        await self._restore_member_roles(
            guild,
            snapshot,
            role_map,
            member_map,
            delete_newer,
            current_members_complete,
            report,
        )
        return report

    async def _restore_guild_settings(self, guild, snapshot, channel_map, report):
        settings = snapshot.get("guild_settings")
        if not settings:
            return
        if not guild.me.guild_permissions.manage_guild:
            report["warnings"].append(
                "Server settings were not restored because the bot lacks Manage Server."
            )
            return

        options = {}
        for key in (
            "name",
            "description",
            "afk_timeout",
            "premium_progress_bar_enabled",
            "widget_enabled",
        ):
            if key in settings:
                options[key] = settings[key]

        for key, enum_type in (
            ("verification_level", discord.VerificationLevel),
            ("default_notifications", discord.NotificationLevel),
            ("explicit_content_filter", discord.ContentFilter),
            ("preferred_locale", discord.Locale),
        ):
            value = settings.get(key)
            if value is not None:
                try:
                    options[key] = enum_type(value)
                except ValueError as error:
                    _record_error(report, f"Server setting {key}", error)

        flags = settings.get("system_channel_flags")
        if flags is not None:
            try:
                options["system_channel_flags"] = (
                    discord.SystemChannelFlags._from_value(int(flags))
                )
            except (TypeError, ValueError) as error:
                _record_error(report, "System channel flags", error)

        for key, option_name in (
            ("afk_channel_id", "afk_channel"),
            ("system_channel_id", "system_channel"),
            ("rules_channel_id", "rules_channel"),
            ("public_updates_channel_id", "public_updates_channel"),
            ("widget_channel_id", "widget_channel"),
        ):
            if key not in settings:
                continue
            channel_id = settings[key]
            if channel_id is None:
                options[option_name] = None
                continue
            channel = channel_map.get(str(channel_id))
            if channel is None and str(channel_id).isdigit():
                channel = guild.get_channel(int(channel_id))
            if channel is None:
                report["warnings"].append(
                    f"Server setting {option_name} was not changed because its channel is missing."
                )
            else:
                options[option_name] = channel

        if options:
            try:
                await guild.edit(**options, reason=BACKUP_REASON)
                report["updated"]["guild_settings"] = len(options)
            except Exception as error:
                _record_error(report, "Server settings batch", error)
                # Retry individually so one unsupported field does not prevent
                # independent settings from being restored.
                for key, value in options.items():
                    try:
                        await guild.edit(**{key: value}, reason=BACKUP_REASON)
                        report["updated"]["guild_settings"] = (
                            report["updated"].get("guild_settings", 0) + 1
                        )
                    except Exception as field_error:
                        _record_error(report, f"Server setting {key}", field_error)

        asset_fields = (
            ("icon_url", "icon"),
            ("banner_url", "banner"),
            ("splash_url", "splash"),
            ("discovery_splash_url", "discovery_splash"),
        )
        asset_records = [
            (key, option, settings[key])
            for key, option in asset_fields
            if key in settings
        ]
        if asset_records:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for key, option, url in asset_records:
                    try:
                        asset = (
                            await self._download_asset(session, url)
                            if url else None
                        )
                        await guild.edit(
                            **{option: asset},
                            reason=BACKUP_REASON,
                        )
                        report["updated"]["guild_settings"] = (
                            report["updated"].get("guild_settings", 0) + 1
                        )
                    except Exception as error:
                        _record_error(report, f"Guild image {key}", error)

    @staticmethod
    def _channel_edit_options(record, category, overwrites):
        kind = record.get("kind")
        options = {
            "name": record.get("name"),
            "position": int(record.get("position", 0)),
            "overwrites": overwrites,
        }
        if kind != "category":
            options["category"] = category
        if kind in {"text", "forum"}:
            options.update({
                "topic": record.get("topic"),
                "slowmode_delay": int(record.get("slowmode_delay", 0)),
                "nsfw": bool(record.get("nsfw", False)),
                "default_auto_archive_duration": int(
                    record.get("default_auto_archive_duration", 1440)
                ),
                "default_thread_slowmode_delay": int(
                    record.get("default_thread_slowmode_delay", 0)
                ),
            })
        if kind == "text":
            options["news"] = bool(record.get("news", False))
        if kind in {"voice", "stage"}:
            options.update({
                "bitrate": int(record.get("bitrate", 64000)),
                "user_limit": int(record.get("user_limit", 0)),
                "rtc_region": record.get("rtc_region"),
                "nsfw": bool(record.get("nsfw", False)),
            })
            if record.get("video_quality_mode") is not None:
                options["video_quality_mode"] = discord.VideoQualityMode(
                    record["video_quality_mode"]
                )
        if kind == "stage":
            options["topic"] = record.get("topic")
        if kind == "forum":
            options["available_tags"] = [
                discord.ForumTag(
                    name=tag["name"],
                    emoji=tag.get("emoji"),
                    moderated=bool(tag.get("moderated", False)),
                )
                for tag in record.get("available_tags", [])
            ]
            if record.get("default_reaction_emoji") is not None:
                options["default_reaction_emoji"] = record[
                    "default_reaction_emoji"
                ]
            if record.get("default_sort_order") is not None:
                options["default_sort_order"] = discord.ForumOrderType(
                    record["default_sort_order"]
                )
            if record.get("default_layout") is not None:
                options["default_layout"] = discord.ForumLayoutType(
                    record["default_layout"]
                )
        return options

    async def _create_channel(self, guild, record, category, overwrites):
        kind = record["kind"]
        name = record.get("name", "Restored Channel")
        position = int(record.get("position", 0))
        if kind == "text":
            return await guild.create_text_channel(
                name,
                category=category,
                news=bool(record.get("news", False)),
                position=position,
                topic=record.get("topic"),
                slowmode_delay=int(record.get("slowmode_delay", 0)),
                nsfw=bool(record.get("nsfw", False)),
                overwrites=overwrites,
                default_auto_archive_duration=int(
                    record.get("default_auto_archive_duration", 1440)
                ),
                default_thread_slowmode_delay=int(
                    record.get("default_thread_slowmode_delay", 0)
                ),
                reason=BACKUP_REASON,
            )
        if kind == "voice":
            options = {
                "category": category,
                "position": position,
                "bitrate": int(record.get("bitrate", 64000)),
                "user_limit": int(record.get("user_limit", 0)),
                "overwrites": overwrites,
                "rtc_region": record.get("rtc_region"),
                "nsfw": bool(record.get("nsfw", False)),
                "reason": BACKUP_REASON,
            }
            if record.get("video_quality_mode") is not None:
                options["video_quality_mode"] = discord.VideoQualityMode(
                    record["video_quality_mode"]
                )
            return await guild.create_voice_channel(name, **options)
        if kind == "stage":
            channel = await guild.create_stage_channel(
                name,
                category=category,
                position=position,
                bitrate=int(record.get("bitrate", 64000)),
                user_limit=int(record.get("user_limit", 0)),
                overwrites=overwrites,
                rtc_region=record.get("rtc_region"),
                nsfw=bool(record.get("nsfw", False)),
                reason=BACKUP_REASON,
            )
            if record.get("topic"):
                await channel.edit(topic=record["topic"], reason=BACKUP_REASON)
            return channel
        if kind == "forum":
            options = {
                "topic": record.get("topic"),
                "position": position,
                "category": category,
                "slowmode_delay": int(record.get("slowmode_delay", 0)),
                "nsfw": bool(record.get("nsfw", False)),
                "overwrites": overwrites,
                "default_auto_archive_duration": int(
                    record.get("default_auto_archive_duration", 1440)
                ),
                "default_thread_slowmode_delay": int(
                    record.get("default_thread_slowmode_delay", 0)
                ),
                "available_tags": [
                    discord.ForumTag(
                        name=tag["name"],
                        emoji=tag.get("emoji"),
                        moderated=bool(tag.get("moderated", False)),
                    )
                    for tag in record.get("available_tags", [])
                ],
                "reason": BACKUP_REASON,
            }
            if record.get("default_reaction_emoji") is not None:
                options["default_reaction_emoji"] = record[
                    "default_reaction_emoji"
                ]
            if record.get("default_sort_order") is not None:
                options["default_sort_order"] = discord.ForumOrderType(
                    record["default_sort_order"]
                )
            if record.get("default_layout") is not None:
                options["default_layout"] = discord.ForumLayoutType(
                    record["default_layout"]
                )
            return await guild.create_forum(name, **options)
        raise ValueError(f"Unsupported channel type: {kind}")

    async def _restore_automod(self, guild, snapshot, role_map, channel_map, report):
        rules = snapshot.get("automod_rules", [])
        if not rules:
            return
        if not guild.me.guild_permissions.manage_guild:
            report["warnings"].append(
                "AutoMod rules were not restored because the bot lacks Manage Server."
            )
            return
        try:
            existing_rules = await guild.fetch_automod_rules()
        except Exception as error:
            _record_error(report, "Read AutoMod rules", error)
            return

        for record in rules:
            trigger_data = record.get("trigger", {})
            try:
                trigger = discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType(trigger_data["type"]),
                    keyword_filter=trigger_data.get("keyword_filter"),
                    regex_patterns=trigger_data.get("regex_patterns"),
                presets=_deserialize_automod_presets(
                    trigger_data.get("presets")
                ),
                    allow_list=trigger_data.get("allow_list"),
                    mention_limit=trigger_data.get("mention_limit"),
                    mention_raid_protection=trigger_data.get(
                        "mention_raid_protection"
                    ),
                )
                actions = []
                for item in record.get("actions", []):
                    action_type = discord.AutoModRuleActionType(item["type"])
                    duration = item.get("duration_seconds")
                    channel_id = item.get("channel_id")
                    if channel_id:
                        channel = channel_map.get(str(channel_id))
                        if channel is None and str(channel_id).isdigit():
                            channel = guild.get_channel(int(channel_id))
                        if action_type == discord.AutoModRuleActionType.send_alert_message:
                            if channel is None:
                                continue
                            actions.append(
                                discord.AutoModRuleAction(channel_id=channel.id)
                            )
                            continue
                    if action_type == discord.AutoModRuleActionType.timeout and duration:
                        actions.append(discord.AutoModRuleAction(
                            duration=datetime.timedelta(seconds=float(duration))
                        ))
                    elif action_type == discord.AutoModRuleActionType.block_message:
                        actions.append(discord.AutoModRuleAction(
                            type=action_type,
                            custom_message=item.get("custom_message"),
                        ))
                    else:
                        actions.append(discord.AutoModRuleAction(type=action_type))

                exempt_roles = [
                    role_map.get(str(role_id)) or guild.get_role(int(role_id))
                    for role_id in record.get("exempt_roles", [])
                    if str(role_id).isdigit()
                ]
                exempt_channels = [
                    channel_map.get(str(channel_id))
                    or guild.get_channel(int(channel_id))
                    for channel_id in record.get("exempt_channels", [])
                    if str(channel_id).isdigit()
                ]
                exempt_roles = [role for role in exempt_roles if role is not None]
                exempt_channels = [
                    channel for channel in exempt_channels if channel is not None
                ]
                options = {
                    "name": record.get("name", "Restored AutoMod rule"),
                    "event_type": discord.AutoModRuleEventType(
                        record["event_type"]
                    ),
                    "trigger": trigger,
                    "actions": actions,
                    "enabled": bool(record.get("enabled", False)),
                    "exempt_roles": exempt_roles,
                    "exempt_channels": exempt_channels,
                    "reason": BACKUP_REASON,
                }
                existing = next(
                    (
                        rule for rule in existing_rules
                        if str(rule.id) == str(record.get("id"))
                    ),
                    None,
                ) or next(
                    (
                        rule for rule in existing_rules
                        if rule.name == record.get("name")
                    ),
                    None,
                )
                if existing:
                    await existing.edit(**options)
                    report["updated"]["automod_rules"] = (
                        report["updated"].get("automod_rules", 0) + 1
                    )
                else:
                    await guild.create_automod_rule(**options)
                    report["created"]["automod_rules"] = (
                        report["created"].get("automod_rules", 0) + 1
                    )
            except Exception as error:
                _record_error(report, f"AutoMod rule {record.get('name')}", error)

    async def _restore_webhooks(self, guild, snapshot, channel_map, report):
        if not snapshot.get("webhooks"):
            return
        if not guild.me.guild_permissions.manage_webhooks:
            report["warnings"].append(
                "Webhooks were not restored because the bot lacks Manage Webhooks."
            )
            return
        for record in snapshot["webhooks"]:
            channel_id = str(record.get("channel_id", ""))
            channel = channel_map.get(channel_id)
            if channel is None and channel_id.isdigit():
                channel = guild.get_channel(int(channel_id))
            if channel is None or not callable(getattr(channel, "webhooks", None)):
                report["skipped"]["webhooks"] = (
                    report["skipped"].get("webhooks", 0) + 1
                )
                continue
            try:
                existing = await channel.webhooks()
                if any(webhook.name == record.get("name") for webhook in existing):
                    report["skipped"]["webhooks"] = (
                        report["skipped"].get("webhooks", 0) + 1
                    )
                    continue
                await channel.create_webhook(
                    name=(record.get("name") or "Restored webhook")[:80],
                    reason=BACKUP_REASON,
                )
                report["created"]["webhooks"] = (
                    report["created"].get("webhooks", 0) + 1
                )
            except Exception as error:
                _record_error(report, f"Webhook {record.get('name')}", error)

    async def _restore_assets(self, guild, snapshot, role_map, report):
        emoji_records = snapshot.get("emojis", [])
        sticker_records = snapshot.get("stickers", [])
        if not emoji_records and not sticker_records:
            return
        if not guild.me.guild_permissions.manage_emojis_and_stickers:
            report["warnings"].append(
                "Emojis and stickers were not restored because the bot lacks Manage Expressions."
            )
            return

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for record in emoji_records:
                if any(
                    str(emoji.id) == str(record.get("id"))
                    or emoji.name == record.get("name")
                    for emoji in guild.emojis
                ):
                    report["skipped"]["emojis"] = (
                        report["skipped"].get("emojis", 0) + 1
                    )
                    continue
                try:
                    asset = await self._download_asset(
                        session, record.get("asset_url")
                    )
                    if not asset:
                        raise ValueError("Discord CDN asset is unavailable")
                    roles = [
                        role_map.get(str(item.get("id")))
                        for item in record.get("roles", [])
                    ]
                    await guild.create_custom_emoji(
                        name=(record.get("name") or "restored")[:32],
                        image=asset,
                        roles=[role for role in roles if role is not None],
                        reason=BACKUP_REASON,
                    )
                    report["created"]["emojis"] = (
                        report["created"].get("emojis", 0) + 1
                    )
                except Exception as error:
                    _record_error(report, f"Emoji {record.get('name')}", error)

            try:
                existing_stickers = await guild.fetch_stickers()
            except Exception as error:
                existing_stickers = []
                _record_error(report, "List stickers", error)
            for record in sticker_records:
                if any(
                    str(sticker.id) == str(record.get("id"))
                    or sticker.name == record.get("name")
                    for sticker in existing_stickers
                ):
                    report["skipped"]["stickers"] = (
                        report["skipped"].get("stickers", 0) + 1
                    )
                    continue
                try:
                    asset_url = record.get("asset_url")
                    asset = await self._download_asset(session, asset_url)
                    if not asset:
                        raise ValueError("Discord CDN asset is unavailable")
                    suffix = Path(urlparse(asset_url).path).suffix.lower()
                    if suffix not in {".png", ".apng", ".json", ".gif"}:
                        suffix = ".png"
                    file = discord.File(
                        io.BytesIO(asset),
                        filename=f"restore-sticker{suffix}",
                    )
                    await guild.create_sticker(
                        name=(record.get("name") or "restored")[:30],
                        description=(record.get("description") or "")[:100],
                        emoji=record.get("emoji") or "👍",
                        file=file,
                        reason=BACKUP_REASON,
                    )
                    report["created"]["stickers"] = (
                        report["created"].get("stickers", 0) + 1
                    )
                except Exception as error:
                    _record_error(report, f"Sticker {record.get('name')}", error)

    @staticmethod
    async def _download_asset(session, url):
        if not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in SAFE_ASSET_HOSTS:
            raise ValueError("Asset URL is not a Discord CDN URL")
        async with session.get(url, allow_redirects=True) as response:
            final_host = urlparse(str(response.url)).hostname
            if final_host not in SAFE_ASSET_HOSTS:
                raise ValueError("Asset redirect is not a Discord CDN URL")
            response.raise_for_status()
            content = await response.read()
            if len(content) > 2_000_000:
                raise ValueError("Asset is too large to restore")
            return content

    async def _restore_member_roles(
        self,
        guild,
        snapshot,
        role_map,
        member_map,
        delete_newer,
        current_members_complete,
        report,
    ):
        if not current_members_complete:
            report["warnings"].append(
                "Member roles were not changed because the bot could not read every current server member."
            )
            return
        if not snapshot.get("member_roles_complete", True):
            report["warnings"].append(
                "Member roles were not changed because the saved member mapping is incomplete."
            )
            return
        records = snapshot.get("member_roles", [])
        if not records:
            if snapshot.get("capture_warnings"):
                report["warnings"].append(
                    "No member-role mappings were captured; member roles were not changed."
                )
            return
        processed = 0
        for record in records:
            member_id = record.get("id", "")
            if not str(member_id).isdigit():
                continue
            member = member_map.get(int(member_id))
            if member is None:
                report["skipped"]["members"] = (
                    report["skipped"].get("members", 0) + 1
                )
                continue
            processed += 1
            desired_roles = []
            for role_record in record.get("roles", []):
                role = role_map.get(str(role_record.get("id")))
                if role is None:
                    role = next(
                        (
                            item for item in guild.roles
                            if item.name == role_record.get("name")
                        ),
                        None,
                    )
                if role and not role.is_default() and not role.managed:
                    desired_roles.append(role)

            current = [role for role in member.roles if not role.is_default()]
            bot_top_position = guild.me.top_role.position
            manageable_current = [
                role for role in current
                if not role.managed and role.position < bot_top_position
            ]
            manageable_desired = [
                role for role in desired_roles
                if role.position < bot_top_position
            ]
            if len(manageable_desired) < len(desired_roles):
                report["skipped"]["unmanageable_member_roles"] = (
                    report["skipped"].get("unmanageable_member_roles", 0)
                    + len(desired_roles) - len(manageable_desired)
                )

            current_ids = {role.id for role in current}
            desired_ids = {role.id for role in manageable_desired}
            roles_to_add = [
                role for role in manageable_desired
                if role.id not in current_ids
            ]
            roles_to_remove = (
                [
                    role for role in manageable_current
                    if role.id not in desired_ids
                ]
                if delete_newer else []
            )
            changed = False
            if roles_to_add:
                try:
                    await member.add_roles(
                        *roles_to_add, reason=BACKUP_REASON
                    )
                    changed = True
                except Exception as error:
                    _record_error(
                        report, f"Add roles for member {member.id}", error
                    )
            if roles_to_remove:
                try:
                    await member.remove_roles(
                        *roles_to_remove, reason=BACKUP_REASON
                    )
                    changed = True
                except Exception as error:
                    _record_error(
                        report, f"Remove roles for member {member.id}", error
                    )
            if changed:
                report["updated"]["members"] = (
                    report["updated"].get("members", 0) + 1
                )
            if processed and processed % 25 == 0:
                await asyncio.sleep(0.5)
