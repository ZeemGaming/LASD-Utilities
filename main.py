import os
import re
import time
import random
import datetime
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from discord.ext import commands, tasks

# --- CONFIGURATION ---
GUILD_ID = 1538076681565241384
QUOTA_LOG_CHANNEL_ID = 1544780775948419082
STRIKE_LOG_CHANNEL_ID = 1538076682664017944
ARREST_LOG_CHANNEL_ID = 1538076682441724001
CITE_LOG_CHANNEL_ID = 1538076682441724002
QUOTA_REQUIRED_SECONDS = 7200  # 2 Hours

PREFIX = "On Shift | "

# User IDs allowed for admin commands
ALLOWED_USER_IDS = {
    1111666992982827018,  # @zeemonyt.
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

# Active shifts memory: { user_id: { "start_time": float, "original_nick": str, "break_time": float, "on_break": bool, "break_start": float } }
active_shifts = {}

# Saved cumulative shift history: { user_id: total_seconds_floated }
shift_leaderboard_data = {}

# Last completed shift duration memory for clearing: { user_id: last_shift_seconds_floated }
last_shifts = {}

# User strikes database: { user_id: [ { "case_id": str, "moderator_id": int, "reason": str, "timestamp": int } ] }
user_strikes = {}

# Flag to prevent multiple reset triggers within the same minute
has_reset_this_week = False


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
    except discord.Forbidden:
        return False


async def restore_nick(member: discord.Member, original_nick: str) -> bool:
    """Restores the user's original nickname if it starts with the shift prefix."""
    try:
        current_nick = member.nick or ""
        if current_nick.startswith(PREFIX):
            await member.edit(nick=original_nick)
        return True
    except discord.Forbidden:
        return False


async def log_quota_failure(guild: discord.Guild, user: discord.Member, current_time: float):
    """Logs to the quota channel if user has not met the required 2 hours of shift time."""
    if current_time < QUOTA_REQUIRED_SECONDS:
        channel = guild.get_channel(QUOTA_LOG_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="⚠️ Quota Unfulfilled",
                description=(
                    f"**Staff Member:** {user.mention} (`{user.id}`)\n"
                    f"**Current Total Shift Time:** `{format_duration(current_time)}` / `02h 00m 00s`\n"
                    f"**Status:** Failed to meet 2-hour minimum requirement."
                ),
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                print(f"Error: Lacking permissions to send message in quota channel {QUOTA_LOG_CHANNEL_ID}")


# --- AUTOMATED WEEKLY RESET TASK (12 AM Saturday CST) ---
@tasks.loop(seconds=30)
async def weekly_quota_reset():
    global has_reset_this_week
    cst_zone = ZoneInfo("America/Chicago")
    now_cst = datetime.datetime.now(cst_zone)

    # Saturday is weekday 5. 12:00 AM corresponds to hour 0, minute 0.
    if now_cst.weekday() == 5 and now_cst.hour == 0 and now_cst.minute == 0:
        if not has_reset_this_week:
            has_reset_this_week = True
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
            print("Weekly shift leaderboard successfully reset at 12:00 AM Saturday CST.")
    else:
        has_reset_this_week = False


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if not weekly_quota_reset.is_running():
        weekly_quota_reset.start()

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


# --- SHIFT SLASH COMMANDS ---

@bot.tree.command(name="shift-start", description="Start your staff shift and update your server nickname.")
async def shift_start(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id in active_shifts:
        embed = discord.Embed(
            title="Shift Error",
            description="You are already on an active shift! Use `/shift-end` to finish your shift.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    original_nick = interaction.user.nick if interaction.user.nick else interaction.user.name

    active_shifts[user_id] = {
        "start_time": time.time(),
        "original_nick": original_nick,
        "break_time": 0.0,
        "on_break": False,
        "break_start": 0.0
    }

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

    await interaction.response.send_message(embed=embed)


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

    if interaction.guild:
        await log_quota_failure(interaction.guild, interaction.user, total_cumulative)


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

        await restore_nick(user, shift_data["original_nick"])

        embed = discord.Embed(
            title="Admin Override: Shift Ended",
            description=f"Shift for {user.mention} was forcefully ended by {interaction.user.mention}.\n**Logged Duration:** `{format_duration(active_duty_time)}`",
            color=discord.Color.orange()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

        if interaction.guild:
            await log_quota_failure(interaction.guild, user, total_cumulative)

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

        if interaction.guild:
            await log_quota_failure(interaction.guild, user, new_total)

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
            if interaction.guild:
                await log_quota_failure(interaction.guild, user, new_total)
        else:
            embed = discord.Embed(
                title="Admin Action Error",
                description=f"No active shift or recent shift record found for {user.mention}.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

    elif action == "clear_all":
        if target_id in active_shifts:
            shift_data = active_shifts.pop(target_id)
            await restore_nick(user, shift_data["original_nick"])

        shift_leaderboard_data.pop(target_id, None)
        last_shifts.pop(target_id, None)

        embed = discord.Embed(
            title="Admin Action: All Shift Data Cleared",
            description=f"Cleared all accumulated shift hours and history for {user.mention}.",
            color=discord.Color.dark_red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

        if interaction.guild:
            await log_quota_failure(interaction.guild, user, 0.0)


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
        "Image:"
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
                allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            await channel.send(
                content=content,
                allowed_mentions=discord.AllowedMentions.none()
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
        f"Arrester: {interaction.user.mention}"
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

bot.run(token)