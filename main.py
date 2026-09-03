import os
import json
import re
import time
import random
import datetime
import asyncio
import tempfile
from zoneinfo import ZoneInfo
from aiohttp import ClientSession, ClientTimeout, web
import discord
from discord import app_commands
from discord.ext import commands, tasks

# --- CONFIGURATION ---
GUILD_ID = 1538076681565241384
QUOTA_LOG_CHANNEL_ID = 1544780775948419082
STRIKE_LOG_CHANNEL_ID = 1538076682664017944
ARREST_LOG_CHANNEL_ID = 1538076682441724001
CITE_LOG_CHANNEL_ID = 1538076682441724002
SHIFT_FARMING_ALERT_CHANNEL_ID = 1544201906908430436
VERIFICATION_CHANNEL_ID = 1545124321503215717
VERIFIED_ROLE_ID = 1545109313276878879
SHIFT_FARMING_THRESHOLD_SECONDS = 10 * 60 * 60
QUOTA_REQUIRED_SECONDS = 7200  # 2 Hours
WEB_PORT = int(os.getenv("PORT", "5000"))
SHIFT_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shifts.json")
VERIFICATION_DATA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "verification.json"
)

PREFIX = "On Shift | "

# User IDs allowed for admin commands
ALLOWED_USER_IDS = {
    1422647977880584324,  # @zeemonyt. (Secondary ID)
}

# Role IDs allowed to run admin commands
ALLOWED_ROLE_IDS = {
    1538076681628028957,
    1538076681632219216,
    1538076681632219218,
    1538076681632219219,
    1538076681632219220,
    1538076681632219221,
    1538076681632219222,
    1538076681632219223,
    1538076681632219224,
    1538076681640747130,
    1538076681640747131,
    1539413609388712006,
    1538076681640747132,
    1538076681640747133
}

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def health_handler(request: web.Request) -> web.Response:
    """Returns the plaintext health response for GET and HEAD requests."""
    return web.Response(
        text="Bot is alive!",
        content_type="text/plain"
    )


async def start_web_server() -> web.AppRunner:
    """Starts the health server on the same async loop as the Discord bot."""
    app = web.Application()
    app.router.add_get("/", health_handler, allow_head=False)
    app.router.add_head("/", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"Health website listening on port {WEB_PORT}.")
    return runner


# Active shifts memory: { user_id: { "start_time": float, "original_nick": str, "break_time": float, "on_break": bool, "break_start": float } }
active_shifts = {}

# Saved cumulative shift history: { user_id: total_seconds_floated }
shift_leaderboard_data = {}

# Last completed shift duration memory for clearing: { user_id: last_shift_seconds_floated }
last_shifts = {}

# User strikes database: { user_id: [ { "case_id": str, "moderator_id": int, "reason": str, "timestamp": int } ] }
user_strikes = {}

# Date of the most recent Saturday quota reset, persisted with shift data
last_quota_reset_date = None

# User IDs already warned about an active shift lasting over 10 hours
shift_farming_alerted = set()

# Persistent verification message and in-progress verification requests
verification_message_id = None
pending_verifications = {}
verification_views_registered = False


def save_shift_data() -> bool:
    """Persist all shift state atomically so unexpected shutdowns do not lose it."""
    data = {
        "active_shifts": {
            str(user_id): {
                "start_time": float(shift_data["start_time"]),
                "original_nick": shift_data["original_nick"],
                "break_time": float(shift_data["break_time"]),
                "on_break": bool(shift_data["on_break"]),
                "break_start": float(shift_data["break_start"])
            }
            for user_id, shift_data in active_shifts.items()
        },
        "shift_leaderboard_data": {
            str(user_id): float(total_seconds)
            for user_id, total_seconds in shift_leaderboard_data.items()
        },
        "last_shifts": {
            str(user_id): float(duration)
            for user_id, duration in last_shifts.items()
        },
        "shift_farming_alerted": [str(user_id) for user_id in shift_farming_alerted],
        "last_quota_reset_date": last_quota_reset_date
    }

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(SHIFT_DATA_PATH),
            prefix=".shifts-",
            suffix=".tmp",
            delete=False
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(data, temp_file, indent=2, sort_keys=True)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, SHIFT_DATA_PATH)
        return True
    except (OSError, TypeError, ValueError) as error:
        print(f"Error: Could not save shift data to {SHIFT_DATA_PATH}: {error}")
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


def load_shift_data() -> None:
    """Restore persisted shift state after a restart."""
    try:
        with open(SHIFT_DATA_PATH, "r", encoding="utf-8") as shift_file:
            data = json.load(shift_file)
    except FileNotFoundError:
        print(f"No saved shift data found at {SHIFT_DATA_PATH}; starting with empty shift state.")
        return
    except (OSError, json.JSONDecodeError) as error:
        print(f"Error: Could not load shift data from {SHIFT_DATA_PATH}: {error}")
        return

    if not isinstance(data, dict):
        print(f"Error: Saved shift data in {SHIFT_DATA_PATH} is not a JSON object.")
        return

    saved_active_shifts = data.get("active_shifts", {})
    if isinstance(saved_active_shifts, dict):
        for user_id, shift_data in saved_active_shifts.items():
            if not isinstance(shift_data, dict):
                continue
            try:
                parsed_user_id = int(user_id)
                original_nick = shift_data["original_nick"]
                if not isinstance(original_nick, str):
                    continue
                active_shifts[parsed_user_id] = {
                    "start_time": float(shift_data["start_time"]),
                    "original_nick": original_nick,
                    "break_time": float(shift_data["break_time"]),
                    "on_break": bool(shift_data["on_break"]),
                    "break_start": float(shift_data["break_start"])
                }
            except (KeyError, TypeError, ValueError):
                print(f"Warning: Ignoring invalid active shift record for user {user_id}.")

    saved_leaderboard = data.get("shift_leaderboard_data", {})
    if isinstance(saved_leaderboard, dict):
        for user_id, total_seconds in saved_leaderboard.items():
            try:
                shift_leaderboard_data[int(user_id)] = float(total_seconds)
            except (TypeError, ValueError):
                print(f"Warning: Ignoring invalid leaderboard record for user {user_id}.")

    saved_last_shifts = data.get("last_shifts", {})
    if isinstance(saved_last_shifts, dict):
        for user_id, duration in saved_last_shifts.items():
            try:
                last_shifts[int(user_id)] = float(duration)
            except (TypeError, ValueError):
                print(f"Warning: Ignoring invalid last-shift record for user {user_id}.")

    saved_alerted_users = data.get("shift_farming_alerted", [])
    if isinstance(saved_alerted_users, list):
        for user_id in saved_alerted_users:
            try:
                shift_farming_alerted.add(int(user_id))
            except (TypeError, ValueError):
                print(f"Warning: Ignoring invalid shift-farming alert record for user {user_id}.")

    saved_reset_date = data.get("last_quota_reset_date")
    if saved_reset_date is None or isinstance(saved_reset_date, str):
        global last_quota_reset_date
        last_quota_reset_date = saved_reset_date
    else:
        print("Warning: Ignoring invalid saved quota reset date.")

    print(
        f"Loaded saved shift data: {len(active_shifts)} active, "
        f"{len(shift_leaderboard_data)} leaderboard, {len(last_shifts)} last-shift records."
    )


load_shift_data()
if not os.path.exists(SHIFT_DATA_PATH):
    save_shift_data()


def save_verification_data() -> bool:
    """Persist the verification message ID and any in-progress requests atomically."""
    data = {
        "verification_message_id": verification_message_id,
        "pending_verifications": {
            str(user_id): {
                "roblox_username": request["roblox_username"],
                "roblox_user_id": int(request["roblox_user_id"]),
                "code": request["code"],
                "guild_id": int(request["guild_id"]),
                "created_at": int(request["created_at"])
            }
            for user_id, request in pending_verifications.items()
        }
    }

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(VERIFICATION_DATA_PATH),
            prefix=".verification-",
            suffix=".tmp",
            delete=False
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(data, temp_file, indent=2, sort_keys=True)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, VERIFICATION_DATA_PATH)
        return True
    except (OSError, TypeError, ValueError) as error:
        print(f"Error: Could not save verification data to {VERIFICATION_DATA_PATH}: {error}")
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


def load_verification_data() -> None:
    """Restore the verification message ID and pending requests after a restart."""
    global verification_message_id

    try:
        with open(VERIFICATION_DATA_PATH, "r", encoding="utf-8") as verification_file:
            data = json.load(verification_file)
    except FileNotFoundError:
        print(
            f"No saved verification data found at {VERIFICATION_DATA_PATH}; "
            "the verification message will be created once."
        )
        return
    except (OSError, json.JSONDecodeError) as error:
        print(f"Error: Could not load verification data from {VERIFICATION_DATA_PATH}: {error}")
        return

    if not isinstance(data, dict):
        print(f"Error: Saved verification data in {VERIFICATION_DATA_PATH} is not a JSON object.")
        return

    saved_message_id = data.get("verification_message_id")
    if saved_message_id is not None:
        try:
            verification_message_id = int(saved_message_id)
        except (TypeError, ValueError):
            print("Warning: Ignoring invalid saved verification message ID.")

    saved_requests = data.get("pending_verifications", {})
    if isinstance(saved_requests, dict):
        for user_id, request in saved_requests.items():
            if not isinstance(request, dict):
                continue
            try:
                pending_verifications[int(user_id)] = {
                    "roblox_username": str(request["roblox_username"]),
                    "roblox_user_id": int(request["roblox_user_id"]),
                    "code": str(request["code"]),
                    "guild_id": int(request["guild_id"]),
                    "created_at": int(request["created_at"])
                }
            except (KeyError, TypeError, ValueError):
                print(f"Warning: Ignoring invalid verification request for user {user_id}.")

    print(
        f"Loaded verification data: message ID {'present' if verification_message_id else 'missing'}, "
        f"{len(pending_verifications)} pending request(s)."
    )


load_verification_data()


def format_duration(seconds: float) -> str:
    """Formats seconds into hours, minutes, and seconds."""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}h {minutes:02d}m {secs:02d}s"


def parse_time_string(time_str: str) -> float:
    """Parses time strings like '1h 30m', '2h', '45m', '300s', or raw numbers as minutes."""
    if not time_str:
        return 0.0
    time_str = time_str.lower().strip()

    if time_str.isdigit():
        return float(time_str) * 60.0

    total_seconds = 0.0
    pattern = re.compile(r'(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?')
    match = pattern.fullmatch(time_str)
    if match:
        hours = int(match.group(1)) if match.group(1) else 0
        minutes = int(match.group(2)) if match.group(2) else 0
        seconds = int(match.group(3)) if match.group(3) else 0
        total_seconds = (hours * 3600) + (minutes * 60) + seconds

    return total_seconds


def is_admin(interaction: discord.Interaction) -> bool:
    """Checks if the user is authorized to run admin commands."""
    if interaction.user.id in ALLOWED_USER_IDS:
        return True

    if isinstance(interaction.user, discord.Member):
        user_role_ids = {role.id for role in interaction.user.roles}
        if user_role_ids.intersection(ALLOWED_ROLE_IDS):
            return True

    return False


async def apply_on_shift_nick(member: discord.Member, original_nick: str) -> bool:
    """Applies the 'On Shift | ' prefix to a user's nickname."""
    new_nick = f"{PREFIX}{original_nick}"
    if len(new_nick) > 32:
        new_nick = new_nick[:32]
    try:
        await member.edit(nick=new_nick)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def restore_nick(member: discord.Member, original_nick: str) -> bool:
    """Restores the user's original nickname if it starts with the shift prefix."""
    try:
        current_nick = member.nick or ""
        if current_nick.startswith(PREFIX):
            await member.edit(nick=original_nick)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def check_for_shift_farming():
    """Automatically end and report active shifts exceeding 10 hours."""
    for user_id in list(shift_farming_alerted):
        if user_id not in active_shifts:
            shift_farming_alerted.discard(user_id)

    warning_channel = bot.get_channel(SHIFT_FARMING_ALERT_CHANNEL_ID)
    log_channel = bot.get_channel(STRIKE_LOG_CHANNEL_ID)

    for user_id, shift_data in list(active_shifts.items()):
        elapsed_seconds = time.time() - shift_data["start_time"]
        if elapsed_seconds <= SHIFT_FARMING_THRESHOLD_SECONDS or user_id in shift_farming_alerted:
            continue

        end_time = time.time()
        total_break_time = shift_data["break_time"]
        if shift_data["on_break"]:
            total_break_time += end_time - shift_data["break_start"]

        active_duty_time = max(
            0.0,
            end_time - shift_data["start_time"] - total_break_time
        )
        last_shifts[user_id] = active_duty_time
        shift_leaderboard_data[user_id] = (
            shift_leaderboard_data.get(user_id, 0.0) + active_duty_time
        )
        active_shifts.pop(user_id, None)
        save_shift_data()

        guild = bot.get_guild(GUILD_ID)
        member = guild.get_member(user_id) if guild else None
        if member:
            await restore_nick(member, shift_data["original_nick"])

        warning_sent = False
        if warning_channel:
            try:
                await warning_channel.send(
                    f"<@{user_id}> Your shift was automatically ended after exceeding 10 hours "
                    "because it may have been shift farming. If this was a legitimate shift, "
                    "you must start a new shift with `/shift-start`."
                )
                warning_sent = True
            except (discord.Forbidden, discord.HTTPException):
                print(f"Error: Could not send shift-farming warning for user {user_id}.")

        if log_channel:
            log_embed = discord.Embed(
                title="Possible Shift Farming Detected",
                description=(
                    f"Possible shift farmer/farming detected for <@{user_id}>.\n"
                    f"**Shift Duration Before Auto-End:** `{format_duration(elapsed_seconds)}`\n"
                    f"**Recorded Active Duty Time:** `{format_duration(active_duty_time)}`\n"
                    "The shift was automatically ended. The staff member must start a new "
                    "shift if this was legitimate."
                ),
                color=discord.Color.orange()
            )
            log_embed.set_footer(text="Made by zeemonyt")
            try:
                await log_channel.send(
                    embed=log_embed,
                    allowed_mentions=discord.AllowedMentions.none()
                )
                warning_sent = True
            except (discord.Forbidden, discord.HTTPException):
                print(f"Error: Could not log possible shift farming for user {user_id}.")

        if warning_sent or (not warning_channel and not log_channel):
            shift_farming_alerted.add(user_id)
            save_shift_data()


# --- AUTOMATED WEEKLY RESET TASK (12 AM Saturday CST) ---
@tasks.loop(seconds=30)
async def weekly_quota_reset():
    global last_quota_reset_date
    cst_zone = ZoneInfo("America/Chicago")
    now_cst = datetime.datetime.now(cst_zone)

    await check_for_shift_farming()

    # Saturday is weekday 5. The persisted date also handles downtime during 12:00 AM.
    if now_cst.weekday() == 5:
        reset_date = now_cst.date().isoformat()
        if last_quota_reset_date != reset_date:
            last_quota_reset_date = reset_date
            guild = bot.get_guild(GUILD_ID)
            if guild:
                channel = guild.get_channel(QUOTA_LOG_CHANNEL_ID)
                if channel:
                    failed_users = []
                    for uid, total_secs in shift_leaderboard_data.items():
                        if total_secs < QUOTA_REQUIRED_SECONDS:
                            failed_users.append(f"• <@{uid}> (`{uid}`) — `{format_duration(total_secs)}` / `02h 00m 00s`")

                    report_text = "**Weekly Quota Summary Report (Saturday Reset - CST)**\n\n"
                    if failed_users:
                        report_text += "The following staff members did not meet the 2-hour minimum shift quota this week:\n\n" + "\n".join(failed_users)
                    else:
                        report_text += "All active staff members successfully met their 2-hour quota this week! 🎉"

                    embed = discord.Embed(
                        title="📅 End of Week Quota Report",
                        description=report_text,
                        color=discord.Color.red() if failed_users else discord.Color.green()
                    )
                    embed.set_footer(text="Made by zeemonyt")
                    try:
                        await channel.send(embed=embed)
                    except discord.Forbidden:
                        print("Error: Could not send weekly reset report to quota log channel.")

            shift_leaderboard_data.clear()
            last_shifts.clear()
            save_shift_data()
            print("Weekly shift leaderboard successfully reset for Saturday CST.")


VERIFICATION_WORDS = [
    "amber", "apple", "brave", "candy", "cedar", "cloud", "coral", "daisy",
    "eagle", "ember", "frost", "grape", "green", "happy", "ivory", "lemon",
    "maple", "ocean", "olive", "pearl", "piano", "pixel", "pluto", "raven",
    "river", "robin", "sable", "solar", "stone", "sunny", "tiger", "violet"
]


def generate_verification_code() -> str:
    """Generates four short random words for a Roblox bio verification code."""
    return " ".join(random.sample(VERIFICATION_WORDS, 4))


def verification_embed(request: dict) -> discord.Embed:
    """Builds the current verification instructions for a user."""
    embed = discord.Embed(
        title="Roblox Verification",
        description=(
            f"Put this exact code in your Roblox bio:\n\n"
            f"**`{request['code']}`**\n\n"
            "After saving your bio, click **Verify** below. "
            "Use **Words tagged role** to generate a new code."
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(name="Roblox Username", value=request["roblox_username"], inline=True)
    embed.set_footer(text="The code is checked against your public Roblox bio.")
    return embed


async def fetch_roblox_profile(user_id: int) -> dict:
    """Fetches a Roblox user's current public profile."""
    url = f"https://users.roblox.com/v1/users/{user_id}"
    timeout = ClientTimeout(total=10)
    async with ClientSession(timeout=timeout) as session:
        async with session.get(
            url,
            headers={"User-Agent": "LASD Utilities Roblox verification"}
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Roblox profile lookup returned HTTP {response.status}.")
            profile = await response.json()

    return {
        "id": int(profile["id"]),
        "name": str(profile.get("name", "")),
        "description": str(profile.get("description") or "")
    }


async def find_roblox_user(username: str) -> dict | None:
    """Resolves a Roblox username and returns its public profile."""
    url = "https://users.roblox.com/v1/usernames/users"
    timeout = ClientTimeout(total=10)
    async with ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            json={"usernames": [username], "excludeBannedUsers": False},
            headers={"User-Agent": "LASD Utilities Roblox verification"}
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Roblox username lookup returned HTTP {response.status}.")
            results = (await response.json()).get("data", [])

    if not results:
        return None

    matching_user = next(
        (
            result for result in results
            if str(result.get("name", "")).casefold() == username.casefold()
        ),
        results[0]
    )
    return await fetch_roblox_profile(int(matching_user["id"]))


class RobloxUsernameModal(discord.ui.Modal, title="Roblox Verification"):
    roblox_username = discord.ui.TextInput(
        label="Roblox username",
        placeholder="Enter your Roblox username",
        min_length=3,
        max_length=20,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "Verification must be started in the staff server.",
                ephemeral=True
            )
            return

        username = str(self.roblox_username).strip()
        await interaction.response.defer(ephemeral=True)

        try:
            roblox_profile = await find_roblox_user(username)
        except Exception as error:
            print(f"Error: Roblox lookup failed for username {username}: {error}")
            await interaction.followup.send(
                "I couldn't reach Roblox right now. Please try again in a moment.",
                ephemeral=True
            )
            return

        if not roblox_profile:
            await interaction.followup.send(
                f"I couldn't find a Roblox account named **{username}**. "
                "Check the spelling and try again.",
                ephemeral=True
            )
            return

        request = {
            "roblox_username": roblox_profile["name"],
            "roblox_user_id": roblox_profile["id"],
            "code": generate_verification_code(),
            "guild_id": interaction.guild.id,
            "created_at": int(time.time())
        }
        pending_verifications[interaction.user.id] = request
        save_verification_data()

        await interaction.followup.send(
            embed=verification_embed(request),
            view=VerificationCodeView(interaction.user.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none()
        )


class VerificationStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify now",
        style=discord.ButtonStyle.primary,
        custom_id="verification:start"
    )
    async def verify_now(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Verification must be started in the staff server.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(RobloxUsernameModal())


class VerificationCodeView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

        verify_button = discord.ui.Button(
            label="Verify",
            style=discord.ButtonStyle.success,
            custom_id=f"verification:verify:{user_id}"
        )
        verify_button.callback = self.verify_code
        self.add_item(verify_button)

        regenerate_button = discord.ui.Button(
            label="Words tagged role",
            style=discord.ButtonStyle.secondary,
            custom_id=f"verification:regenerate:{user_id}"
        )
        regenerate_button.callback = self.regenerate_code
        self.add_item(regenerate_button)

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "This verification request belongs to another user.",
            ephemeral=True
        )
        return False

    async def regenerate_code(self, interaction: discord.Interaction):
        if not await self._check_owner(interaction):
            return

        request = pending_verifications.get(self.user_id)
        if not request:
            await interaction.response.send_message(
                "This verification request has expired. Click **Verify now** to start again.",
                ephemeral=True
            )
            return

        request["code"] = generate_verification_code()
        request["created_at"] = int(time.time())
        save_verification_data()
        await interaction.response.edit_message(
            embed=verification_embed(request),
            view=VerificationCodeView(self.user_id)
        )

    async def verify_code(self, interaction: discord.Interaction):
        if not await self._check_owner(interaction):
            return

        request = pending_verifications.get(self.user_id)
        if not request:
            await interaction.response.send_message(
                "This verification request has expired. Click **Verify now** to start again.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            roblox_profile = await fetch_roblox_profile(request["roblox_user_id"])
        except Exception as error:
            print(
                f"Error: Roblox bio lookup failed for user "
                f"{request['roblox_username']}: {error}"
            )
            await interaction.followup.send(
                "I couldn't check your Roblox bio right now. Please try again in a moment.",
                ephemeral=True
            )
            return

        if request["code"].casefold() not in roblox_profile["description"].casefold():
            await interaction.followup.send(
                "The verification code was not found in your Roblox bio. "
                "Add it exactly, save your bio, and try again.",
                ephemeral=True
            )
            return

        guild = interaction.guild or bot.get_guild(request["guild_id"])
        if not guild:
            await interaction.followup.send(
                "I couldn't find the staff server. Please try again later.",
                ephemeral=True
            )
            return

        member = guild.get_member(self.user_id)
        if not member:
            try:
                member = await guild.fetch_member(self.user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None

        role = guild.get_role(VERIFIED_ROLE_ID)
        if not member or not role:
            await interaction.followup.send(
                "I couldn't find your server member record or the verification role. "
                "Please contact an administrator.",
                ephemeral=True
            )
            return

        try:
            await member.add_roles(role, reason="Roblox bio verification completed")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I couldn't assign the verification role. Please contact an administrator.",
                ephemeral=True
            )
            return

        pending_verifications.pop(self.user_id, None)
        save_verification_data()
        await interaction.followup.send(
            f"Verification complete. You received the <@&{VERIFIED_ROLE_ID}> role.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none()
        )


def register_verification_views() -> None:
    """Registers persistent button handlers after the bot connects."""
    global verification_views_registered
    if verification_views_registered:
        return

    bot.add_view(VerificationStartView())
    for user_id in pending_verifications:
        bot.add_view(VerificationCodeView(user_id))
    verification_views_registered = True


async def ensure_verification_message() -> None:
    """Ensures the public verification message is sent only once."""
    global verification_message_id

    channel = bot.get_channel(VERIFICATION_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(VERIFICATION_CHANNEL_ID)
        except discord.NotFound:
            print(
                f"Error: Verification channel {VERIFICATION_CHANNEL_ID} was not found "
                "or is not visible to this bot."
            )
            return
        except discord.Forbidden:
            print(
                f"Error: Discord denied access to verification channel "
                f"{VERIFICATION_CHANNEL_ID}; check View Channel permission."
            )
            return
        except discord.HTTPException as error:
            print(
                f"Error: Discord API could not access verification channel "
                f"{VERIFICATION_CHANNEL_ID} (HTTP {error.status})."
            )
            return

    if verification_message_id:
        try:
            await channel.fetch_message(verification_message_id)
            return
        except discord.NotFound:
            print("Saved verification message was not found; creating a replacement.")
            verification_message_id = None
            save_verification_data()
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Error: Could not check the saved verification message: {error}")
            return

    try:
        message = await channel.send(
            "Backup verification system",
            view=VerificationStartView(),
            allowed_mentions=discord.AllowedMentions.none()
        )
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Error: Could not send the verification message: {error}")
        return

    verification_message_id = message.id
    save_verification_data()
    print(
        f"Created one-time verification message {message.id} "
        f"in channel {VERIFICATION_CHANNEL_ID}."
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if not weekly_quota_reset.is_running():
        weekly_quota_reset.start()

    register_verification_views()

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} command(s) to guild {GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global command(s).")
    except Exception as e:
        print(f"Error syncing slash commands: {e}")

    await ensure_verification_message()


# --- SHIFT SLASH COMMANDS ---

@bot.tree.command(name="shift-start", description="Start your staff shift and update your server nickname.")
async def shift_start(interaction: discord.Interaction):
    user_id = interaction.user.id

    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        embed = discord.Embed(
            title="Shift Error",
            description="Shift commands can only be used inside the staff server.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if user_id in active_shifts:
        embed = discord.Embed(
            title="Shift Error",
            description="You are already on an active shift! Use `/shift-end` to finish your shift.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.defer()

    original_nick = interaction.user.nick if interaction.user.nick else interaction.user.name

    active_shifts[user_id] = {
        "start_time": time.time(),
        "original_nick": original_nick,
        "break_time": 0.0,
        "on_break": False,
        "break_start": 0.0
    }
    save_shift_data()

    nick_updated = await apply_on_shift_nick(interaction.user, original_nick)

    embed = discord.Embed(
        title="Shift Started",
        description=f"**Staff Member:** {interaction.user.mention}\n**Start Time:** <t:{int(time.time())}:F>",
        color=discord.Color.green()
    )
    if not nick_updated:
        embed.set_footer(text="Warning: Couldn't change nickname (check bot role hierarchy/permissions).")
    else:
        embed.set_footer(text="Made by zeemonyt")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="shift-break", description="Pause or resume your active shift for a break.")
async def shift_break(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in active_shifts:
        embed = discord.Embed(
            title="Shift Error",
            description="You do not have an active shift to pause or resume.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    shift_data = active_shifts[user_id]

    if not shift_data["on_break"]:
        shift_data["on_break"] = True
        shift_data["break_start"] = time.time()
        save_shift_data()

        embed = discord.Embed(
            title="Shift On Break",
            description=f"**Staff Member:** {interaction.user.mention}\nBreak started at <t:{int(time.time())}:R>.",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)
    else:
        break_duration = time.time() - shift_data["break_start"]
        shift_data["break_time"] += break_duration
        shift_data["on_break"] = False
        shift_data["break_start"] = 0.0
        save_shift_data()

        embed = discord.Embed(
            title="Shift Resumed",
            description=f"**Staff Member:** {interaction.user.mention}\nWelcome back! Break duration: `{format_duration(break_duration)}`.",
            color=discord.Color.green()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="shift-end", description="End your staff shift and restore your nickname.")
async def shift_end(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in active_shifts:
        embed = discord.Embed(
            title="Shift Error",
            description="You do not have an active shift to end.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    shift_data = active_shifts.pop(user_id)
    end_time = time.time()

    total_break_time = shift_data["break_time"]
    if shift_data["on_break"]:
        total_break_time += (end_time - shift_data["break_start"])

    total_time = end_time - shift_data["start_time"]
    active_duty_time = max(0.0, total_time - total_break_time)

    last_shifts[user_id] = active_duty_time
    total_cumulative = shift_leaderboard_data.get(user_id, 0.0) + active_duty_time
    shift_leaderboard_data[user_id] = total_cumulative
    save_shift_data()

    nick_restored = await restore_nick(interaction.user, shift_data["original_nick"])

    embed = discord.Embed(
        title="Shift Summary",
        description=f"Shift ended for {interaction.user.mention}",
        color=discord.Color.blue()
    )
    embed.add_field(name="Start Time", value=f"<t:{int(shift_data['start_time'])}:F>", inline=False)
    embed.add_field(name="End Time", value=f"<t:{int(end_time)}:F>", inline=False)
    embed.add_field(name="Active Duty Duration", value=f"`{format_duration(active_duty_time)}`", inline=True)
    embed.add_field(name="Break Duration", value=f"`{format_duration(total_break_time)}`", inline=True)
    embed.add_field(
        name="Cumulative Duty Time",
        value=f"`{format_duration(total_cumulative)}`",
        inline=False
    )

    if not nick_restored:
        embed.set_footer(text="Warning: Couldn't restore nickname automatically.")
    else:
        embed.set_footer(text="Made by zeemonyt")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="shift-leaderboard", description="View total logged shift time across all staff.")
async def shift_leaderboard(interaction: discord.Interaction):
    if not shift_leaderboard_data:
        embed = discord.Embed(
            title="Shift Leaderboard",
            description="No shift data logged yet.",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)
        return

    sorted_staff = sorted(shift_leaderboard_data.items(), key=lambda item: item[1], reverse=True)

    leaderboard_text = ""
    for index, (uid, total_secs) in enumerate(sorted_staff[:10], start=1):
        status_icon = "✔" if total_secs >= QUOTA_REQUIRED_SECONDS else "❌"
        leaderboard_text += f"**{index}.** <@{uid}> — `{format_duration(total_secs)}` {status_icon}\n"

    embed = discord.Embed(
        title="Staff Shift Leaderboard",
        description=leaderboard_text,
        color=discord.Color.gold()
    )
    embed.set_footer(text="Made by zeemonyt")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="shift-admin", description="Admin override commands for managing staff shifts.")
@app_commands.describe(
    user="The target staff member",
    option="Select an administrative action",
    time_input="Optional: Duration for Add/Remove Time (e.g. 1h 30m, 30m, 2h)"
)
@app_commands.choices(option=[
    app_commands.Choice(name="Start Shift", value="start_shift"),
    app_commands.Choice(name="End Shift", value="end_shift"),
    app_commands.Choice(name="Add Shift Time", value="add_time"),
    app_commands.Choice(name="Remove Shift Time", value="remove_time"),
    app_commands.Choice(name="Clear Current/Last Shift Time", value="clear_last"),
    app_commands.Choice(name="Clear All Shift Time", value="clear_all")
])
async def shift_admin(
    interaction: discord.Interaction,
    user: discord.Member,
    option: app_commands.Choice[str],
    time_input: str = None
):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Permission Denied",
            description="You do not have permission to use admin shift commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    target_id = user.id
    action = option.value

    if action == "start_shift":
        if target_id in active_shifts:
            embed = discord.Embed(
                title="Admin Override Error",
                description=f"{user.mention} is already on an active shift.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        original_nick = user.nick if user.nick else user.name
        active_shifts[target_id] = {
            "start_time": time.time(),
            "original_nick": original_nick,
            "break_time": 0.0,
            "on_break": False,
            "break_start": 0.0
        }
        save_shift_data()
        await apply_on_shift_nick(user, original_nick)

        embed = discord.Embed(
            title="Admin Override: Shift Started",
            description=f"An active shift was forcefully started for {user.mention} by {interaction.user.mention}.",
            color=discord.Color.orange()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

    elif action == "end_shift":
        if target_id not in active_shifts:
            embed = discord.Embed(
                title="Admin Override Error",
                description=f"{user.mention} does not have an active shift.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        shift_data = active_shifts.pop(target_id)
        end_time = time.time()

        total_break_time = shift_data["break_time"]
        if shift_data["on_break"]:
            total_break_time += (end_time - shift_data["break_start"])

        total_time = end_time - shift_data["start_time"]
        active_duty_time = max(0.0, total_time - total_break_time)

        last_shifts[target_id] = active_duty_time
        total_cumulative = shift_leaderboard_data.get(target_id, 0.0) + active_duty_time
        shift_leaderboard_data[target_id] = total_cumulative
        save_shift_data()

        await restore_nick(user, shift_data["original_nick"])

        embed = discord.Embed(
            title="Admin Override: Shift Ended",
            description=f"Shift for {user.mention} was forcefully ended by {interaction.user.mention}.\n**Logged Duration:** `{format_duration(active_duty_time)}`",
            color=discord.Color.orange()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

    elif action == "add_time":
        if not time_input:
            embed = discord.Embed(
                title="Admin Action Error",
                description="Please provide a valid time duration to add (e.g. `1h 30m`, `30m`, `2h`).",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        seconds_to_add = parse_time_string(time_input)
        if seconds_to_add <= 0:
            embed = discord.Embed(
                title="Admin Action Error",
                description="Invalid time format provided. Use formats like `1h 30m`, `45m`, or `2h`.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        current_time = shift_leaderboard_data.get(target_id, 0.0)
        new_total = current_time + seconds_to_add
        shift_leaderboard_data[target_id] = new_total
        save_shift_data()

        embed = discord.Embed(
            title="Admin Action: Time Added",
            description=(
                f"Added `{format_duration(seconds_to_add)}` to {user.mention}'s shift history.\n"
                f"**New Total Shift Time:** `{format_duration(new_total)}`"
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

    elif action == "remove_time":
        if not time_input:
            embed = discord.Embed(
                title="Admin Action Error",
                description="Please provide a valid time duration to remove (e.g. `1h 30m`, `30m`, `2h`).",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        seconds_to_remove = parse_time_string(time_input)
        if seconds_to_remove <= 0:
            embed = discord.Embed(
                title="Admin Action Error",
                description="Invalid time format provided. Use formats like `1h 30m`, `45m`, or `2h`.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        current_time = shift_leaderboard_data.get(target_id, 0.0)
        new_total = max(0.0, current_time - seconds_to_remove)
        shift_leaderboard_data[target_id] = new_total
        save_shift_data()

        embed = discord.Embed(
            title="Admin Action: Time Removed",
            description=(
                f"Deducted `{format_duration(seconds_to_remove)}` from {user.mention}'s shift history.\n"
                f"**New Total Shift Time:** `{format_duration(new_total)}`"
            ),
            color=discord.Color.dark_red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

    elif action == "clear_last":
        if target_id in active_shifts:
            shift_data = active_shifts.pop(target_id)
            await restore_nick(user, shift_data["original_nick"])
            embed = discord.Embed(
                title="Admin Action: Active Shift Canceled",
                description=f"Canceled active shift for {user.mention}. No duty time was logged.",
                color=discord.Color.dark_red()
            )
        elif target_id in last_shifts:
            last_duration = last_shifts.pop(target_id)
            new_total = max(0.0, shift_leaderboard_data.get(target_id, 0.0) - last_duration)
            shift_leaderboard_data[target_id] = new_total
            embed = discord.Embed(
                title="Admin Action: Last Shift Cleared",
                description=f"Removed last shift duration (`{format_duration(last_duration)}`) from {user.mention}'s leaderboard profile.",
                color=discord.Color.dark_red()
            )
        else:
            embed = discord.Embed(
                title="Admin Action Error",
                description=f"No active shift or recent shift record found for {user.mention}.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        save_shift_data()
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

    elif action == "clear_all":
        if target_id in active_shifts:
            shift_data = active_shifts.pop(target_id)
            await restore_nick(user, shift_data["original_nick"])

        shift_leaderboard_data.pop(target_id, None)
        last_shifts.pop(target_id, None)
        save_shift_data()

        embed = discord.Embed(
            title="Admin Action: All Shift Data Cleared",
            description=f"Cleared all accumulated shift hours and history for {user.mention}.",
            color=discord.Color.dark_red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)


# --- STRIKE / INFRACTION COMMAND ---

async def log_strike_event(guild: discord.Guild, embed: discord.Embed):
    """Logs strike actions to the dedicated #strikes channel."""
    if guild:
        channel = guild.get_channel(STRIKE_LOG_CHANNEL_ID)
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                print(f"Error: Lacking permissions to send message in strike channel {STRIKE_LOG_CHANNEL_ID}")


@bot.tree.command(name="strike", description="Manage staff infractions and strikes.")
@app_commands.describe(
    option="Select strike action",
    user="Target staff member",
    reason="Optional: Reason for the strike (or Case ID when editing/removing)",
    case_id="Optional: Specific Case ID (required if editing/removing specific strike)"
)
@app_commands.choices(option=[
    app_commands.Choice(name="Add", value="add"),
    app_commands.Choice(name="Remove", value="remove"),
    app_commands.Choice(name="Edit", value="edit")
])
async def strike(
    interaction: discord.Interaction,
    option: app_commands.Choice[str],
    user: discord.Member,
    reason: str = "No reason provided.",
    case_id: str = None
):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Permission Denied",
            description="You do not have permission to use moderation commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    action = option.value
    target_id = user.id
    now = int(time.time())

    if target_id not in user_strikes:
        user_strikes[target_id] = []

    if action == "add":
        new_case_id = str(random.randint(100000000000000000, 999999999999999999))
        strike_record = {
            "case_id": new_case_id,
            "moderator_id": interaction.user.id,
            "reason": reason,
            "timestamp": now
        }
        user_strikes[target_id].append(strike_record)
        current_infractions = len(user_strikes[target_id])

        embed = discord.Embed(
            title="❗️ | Infraction Log",
            description=(
                "📋 **New Moderation Case**\n\n"
                "A new moderation case has been created and applied successfully.\n\n"
                "**Case Information**\n"
                f"> **Case ID:** `{new_case_id}`\n"
                f"> **User:** {user.mention} | `{target_id}`\n"
                f"> **Moderator:** {interaction.user.mention} | `{interaction.user.id}`\n"
                "> **Action:** Infraction\n"
                f"> **Reason:** {reason}\n"
                "> **Duration:** N/A\n\n"
                "**Additional Information**\n"
                f"• 🕒 **Created:** <t:{now}:f>\n"
                f"• 📜 {user.mention} **current infractions:** `{current_infractions}`\n"
                "• 📚 This case has been added to the user's moderation history.\n"
                "• 🔒 All moderation actions are logged for accountability and staff review."
            ),
            color=discord.Color.from_rgb(47, 49, 54)
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)
        await log_strike_event(interaction.guild, embed)

    elif action == "remove":
        if not user_strikes[target_id]:
            embed = discord.Embed(
                title="Strike Error",
                description=f"{user.mention} has no active infractions to remove.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        removed_case = None
        if case_id:
            for s in user_strikes[target_id]:
                if s["case_id"] == case_id:
                    removed_case = s
                    user_strikes[target_id].remove(s)
                    break
        else:
            removed_case = user_strikes[target_id].pop()

        if not removed_case:
            embed = discord.Embed(
                title="Strike Error",
                description=f"Case ID `{case_id}` not found for {user.mention}.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        current_infractions = len(user_strikes[target_id])
        target_case_id = removed_case["case_id"]

        embed = discord.Embed(
            title="❗️ | Infraction Log",
            description=(
                "📋 **Moderation Case Removed**\n\n"
                "A moderation case has been removed successfully.\n\n"
                "**Case Information**\n"
                f"> **Case ID:** `{target_case_id}`\n"
                f"> **User:** {user.mention} | `{target_id}`\n"
                f"> **Moderator:** {interaction.user.mention} | `{interaction.user.id}`\n"
                "> **Action:** Infraction Removal\n"
                f"> **Reason:** {reason}\n"
                "> **Duration:** N/A\n\n"
                "**Additional Information**\n"
                f"• 🕒 **Action Time:** <t:{now}:f>\n"
                f"• 📜 {user.mention} **current infractions:** `{current_infractions}`\n"
                "• 📚 Case history has been updated.\n"
                "• 🔒 All moderation actions are logged for accountability and staff review."
            ),
            color=discord.Color.from_rgb(47, 49, 54)
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)
        await log_strike_event(interaction.guild, embed)

    elif action == "edit":
        if not user_strikes[target_id]:
            embed = discord.Embed(
                title="Strike Error",
                description=f"{user.mention} has no active infractions to edit.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        target_case = None
        if case_id:
            for s in user_strikes[target_id]:
                if s["case_id"] == case_id:
                    target_case = s
                    break
        else:
            target_case = user_strikes[target_id][-1]

        if not target_case:
            embed = discord.Embed(
                title="Strike Error",
                description=f"Case ID `{case_id}` not found for {user.mention}.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        target_case["reason"] = reason
        current_infractions = len(user_strikes[target_id])
        target_case_id = target_case["case_id"]

        embed = discord.Embed(
            title="❗️ | Infraction Log",
            description=(
                "📋 **Moderation Case Updated**\n\n"
                "A moderation case reason has been updated successfully.\n\n"
                "**Case Information**\n"
                f"> **Case ID:** `{target_case_id}`\n"
                f"> **User:** {user.mention} | `{target_id}`\n"
                f"> **Moderator:** {interaction.user.mention} | `{interaction.user.id}`\n"
                "> **Action:** Infraction Edit\n"
                f"> **New Reason:** {reason}\n"
                "> **Duration:** N/A\n\n"
                "**Additional Information**\n"
                f"• 🕒 **Updated:** <t:{now}:f>\n"
                f"• 📜 {user.mention} **current infractions:** `{current_infractions}`\n"
                "• 📚 This case update has been recorded in the moderation history.\n"
                "• 🔒 All moderation actions are logged for accountability and staff review."
            ),
            color=discord.Color.from_rgb(47, 49, 54)
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)
        await log_strike_event(interaction.guild, embed)


@bot.tree.command(name="strike-list", description="View all active infractions for a staff member.")
@app_commands.describe(user="Target staff member")
async def strike_list(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Permission Denied",
            description="You do not have permission to use moderation commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    target_id = user.id
    strikes = user_strikes.get(target_id, [])

    if not strikes:
        embed = discord.Embed(
            title="❗️ | Infraction History",
            description=f"{user.mention} has no active infractions on record.",
            color=discord.Color.from_rgb(47, 49, 54)
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)
        return

    history_text = f"**Total Infractions:** `{len(strikes)}` for {user.mention}\n\n"
    for case in strikes:
        history_text += (
            f"• **Case ID:** `{case['case_id']}`\n"
            f"  > **Moderator:** <@{case['moderator_id']}>\n"
            f"  > **Reason:** {case['reason']}\n"
            f"  > **Date:** <t:{case['timestamp']}:f>\n\n"
        )

    embed = discord.Embed(
        title="❗️ | Infraction History",
        description=history_text,
        color=discord.Color.from_rgb(47, 49, 54)
    )
    embed.set_footer(text="Made by zeemonyt")
    await interaction.response.send_message(embed=embed)


# --- CITATION COMMAND ---

@bot.tree.command(name="cite", description="Issue a citation to a Roblox user.")
@app_commands.describe(
    user="Roblox username of the cited subject",
    reason="Reason for the citation",
    court_date="Optional court date",
    image="Optional proof image"
)
async def cite(
    interaction: discord.Interaction,
    user: str,
    reason: str,
    court_date: str = None,
    image: discord.Attachment = None
):
    channel = interaction.guild.get_channel(CITE_LOG_CHANNEL_ID) if interaction.guild else None
    if not channel:
        embed = discord.Embed(
            title="Citation Error",
            description=f"Could not find the target citation channel (`{CITE_LOG_CHANNEL_ID}`).",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    content = (
        f"User: {user}\n"
        f"Reason: {reason}\n"
        f"Court date: {court_date or 'Not scheduled'}\n"
        "Image:\n"
        f"Submitted by: {interaction.user.mention}"
    )

    file = None
    if image:
        try:
            file = await image.to_file()
        except Exception as e:
            embed = discord.Embed(
                title="Citation Error",
                description=f"Failed to process the attached image: {e}",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

    try:
        if file:
            await channel.send(
                content=content,
                file=file,
                allowed_mentions=discord.AllowedMentions(users=[interaction.user])
            )
        else:
            await channel.send(
                content=content,
                allowed_mentions=discord.AllowedMentions(users=[interaction.user])
            )
    except discord.Forbidden:
        embed = discord.Embed(
            title="Citation Error",
            description=f"Bot is missing permissions to send the citation or image in <#{CITE_LOG_CHANNEL_ID}>.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="Citation Successfully Logged",
        description=f"Logged citation for Roblox user **{user}** in <#{CITE_LOG_CHANNEL_ID}>.",
        color=discord.Color.green()
    )
    embed.set_footer(text="Made by zeemonyt")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- ARREST COMMAND ---

@bot.tree.command(name="arrest", description="Log a Roblox user arrest with evidence images.")
@app_commands.describe(
    user="Roblox username of the arrested subject",
    reason="Reason for the arrest",
    image="Primary proof image attachment",
    image1="Optional second proof image",
    image2="Optional third proof image"
)
async def arrest(
    interaction: discord.Interaction,
    user: str,
    reason: str,
    image: discord.Attachment,
    image1: discord.Attachment = None,
    image2: discord.Attachment = None,
):
    channel = interaction.guild.get_channel(ARREST_LOG_CHANNEL_ID) if interaction.guild else None
    if not channel:
        embed = discord.Embed(
            title="Arrest Log Error",
            description=f"Could not find the target arrest log channel (`{ARREST_LOG_CHANNEL_ID}`).",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    content = (
        f"User: {user}\n"
        f"Reason for arrest: {reason}\n"
        f"Image:\n"
        f"Arresting Officer: {interaction.user.mention}"
    )

    files = []
    try:
        files.append(await image.to_file())
        if image1:
            files.append(await image1.to_file())
        if image2:
            files.append(await image2.to_file())
    except Exception as e:
        embed = discord.Embed(
            title="Arrest Log Error",
            description=f"Failed to process attached images: {e}",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    try:
        await channel.send(content=content, files=files)
    except discord.Forbidden:
        embed = discord.Embed(
            title="Arrest Log Error",
            description="Bot is missing permissions to send messages or files in the arrest channel.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="Arrest Successfully Logged",
        description=f"Logged arrest for Roblox user **{user}** in <#{ARREST_LOG_CHANNEL_ID}>.",
        color=discord.Color.green()
    )
    embed.set_footer(text="Made by zeemonyt")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- RUN BOT ---
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise ValueError("DISCORD_TOKEN environment variable not set in Replit Secrets.")

async def run_application():
    web_runner = await start_web_server()
    try:
        await bot.start(token)
    finally:
        await web_runner.cleanup()


asyncio.run(run_application())
