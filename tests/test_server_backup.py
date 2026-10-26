import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import discord

import server_backup


class FakeGuild:
    id = 123456789012345678
    name = "Backup Test Server"
    roles = []
    channels = []
    emojis = []

    async def fetch_members(self, limit=None):
        if False:
            yield None

    async def fetch_automod_rules(self):
        return []

    async def fetch_stickers(self):
        return []

    async def integrations(self):
        return []


class FakeRole:
    id = 987654321
    name = "Newer role"
    managed = False
    position = 1

    def __init__(self):
        self.deleted = False
        self.default = False

    def is_default(self):
        return self.default

    async def delete(self, reason=None):
        self.deleted = True


class FakeMember:
    id = 100

    def __init__(self, roles):
        self.roles = list(roles)

    async def add_roles(self, *roles, reason=None):
        self.roles.extend(role for role in roles if role not in self.roles)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [role for role in self.roles if role not in roles]


class FakeChannel:
    id = 987654322
    name = "newer-channel"

    def __init__(self):
        self.deleted = False

    async def delete(self, reason=None):
        self.deleted = True


class FakeRestoreGuild:
    id = 123456789012345678
    name = "Restore Test Server"
    members = []
    default_role = None
    emojis = []

    def __init__(self):
        self.role = FakeRole()
        self.channel = FakeChannel()
        self.roles = [self.role]
        self.channels = [self.channel]
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(
                manage_roles=True,
                manage_channels=True,
                manage_guild=True,
                manage_webhooks=True,
                manage_emojis_and_stickers=True,
            ),
            top_role=SimpleNamespace(position=10),
        )

    async def fetch_members(self, limit=None):
        if False:
            yield None


class ServerBackupStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "backup.json"
        self.manager = server_backup.ServerBackupManager(self.path)
        self.guild = FakeGuild()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_manual_backup_is_named_timestamped_and_private(self):
        await self.manager.initialize()
        entry = await self.manager.create_backup(self.guild, "Before changes")

        self.assertTrue(entry["name"].startswith("Before changes - "))
        self.assertEqual(entry["type"], "manual")
        self.assertEqual(entry["data"]["guild_id"], str(self.guild.id))
        self.assertTrue(self.entry_exists(entry["id"]))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_automatic_backup_counter_and_retention(self):
        with patch.object(server_backup, "AUTO_BACKUP_RETENTION", 2):
            entries = [
                await self.manager.create_backup(self.guild, automatic=True)
                for _ in range(3)
            ]
        self.assertIn("Auto Backup: 1", entries[0]["name"])
        self.assertIn("Auto Backup: 2", entries[1]["name"])
        self.assertIn("Auto Backup: 3", entries[2]["name"])
        backups = await self.manager.list_backups(self.guild.id)
        self.assertEqual(len(backups), 2)
        self.assertEqual(
            [item["id"] for item in backups],
            [entries[1]["id"], entries[2]["id"]],
        )

    async def test_corrupt_store_fails_explicitly(self):
        self.path.write_text("{ invalid json", encoding="utf-8")
        with self.assertRaises(server_backup.BackupStoreError):
            await self.manager.list_backups(self.guild.id)

    async def test_automod_presets_round_trip(self):
        presets = discord.AutoModPresets(
            profanity=True,
            sexual_content=False,
            slurs=True,
        )
        restored = server_backup._deserialize_automod_presets(presets.value)
        self.assertEqual(restored.value, presets.value)

    async def test_restore_preserves_newer_roles_and_channels_by_default(self):
        guild = FakeRestoreGuild()
        backup = {
            "data": {
                "guild_id": str(guild.id),
                "roles": [],
                "channels": [],
                "member_roles": [],
                "channels_complete": True,
                "member_roles_complete": True,
            }
        }

        await self.manager.restore(guild, backup)

        self.assertFalse(guild.role.deleted)
        self.assertFalse(guild.channel.deleted)

    async def test_restore_deletes_newer_roles_and_channels_only_when_selected(self):
        guild = FakeRestoreGuild()
        backup = {
            "data": {
                "guild_id": str(guild.id),
                "roles": [],
                "channels": [],
                "member_roles": [],
                "channels_complete": True,
                "member_roles_complete": True,
            }
        }

        report = await self.manager.restore(guild, backup, delete_newer=True)

        self.assertTrue(guild.role.deleted)
        self.assertTrue(guild.channel.deleted)
        self.assertEqual(report["deleted"], {"channels": 1, "roles": 1})

    async def test_member_restore_preserves_managed_roles_and_resets_only_manageable(self):
        guild = FakeRestoreGuild()
        desired_role = FakeRole()
        desired_role.id = 987654323
        desired_role.name = "Snapshot role"
        desired_role.position = 2
        newer_role = FakeRole()
        newer_role.id = 987654324
        managed_role = FakeRole()
        managed_role.id = 987654325
        managed_role.managed = True
        member = FakeMember([newer_role, managed_role])
        snapshot = {
            "member_roles_complete": True,
            "member_roles": [
                {
                    "id": str(member.id),
                    "roles": [
                        {"id": str(desired_role.id), "name": desired_role.name}
                    ],
                }
            ],
        }
        report = {
            "updated": {},
            "skipped": {},
            "errors": [],
            "errors_omitted": 0,
            "warnings": [],
        }

        await self.manager._restore_member_roles(
            guild,
            snapshot,
            {str(desired_role.id): desired_role},
            {member.id: member},
            True,
            True,
            report,
        )

        self.assertCountEqual(
            member.roles, [desired_role, managed_role]
        )

    def entry_exists(self, entry_id):
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        return any(
            item.get("id") == entry_id
            for guild in stored["guilds"].values()
            for item in guild.get("backups", [])
        )


if __name__ == "__main__":
    unittest.main()
