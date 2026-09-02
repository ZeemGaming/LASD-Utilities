import os
import time
import discord
from discord import app_commands
from discord.ext import commands

# --- CONFIGURATION ---
# Set GUILD_ID to your server's ID for instant slash command registration.
# Set to None if you want global commands (can take up to 1 hour to sync across Discord).
GUILD_ID = 1538076681565241384

PREFIX = "On Shift | "

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Active shifts memory: { user_id: { "start_time": float, "original_nick": str or None } }
active_shifts = {}

# Saved shift history for leaderboard: { user_id: total_seconds_floated }
shift_leaderboard_data = {}


def format_duration(seconds: float) -> str:
    """Formats seconds into hours, minutes, and seconds."""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}h {minutes:02d}m {secs:02d}s"


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
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


# --- SLASH COMMANDS ---

@bot.tree.command(name="start-shift", description="Start your staff shift and update your server nickname.")
async def start_shift(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id in active_shifts:
        embed = discord.Embed(
            title="Shift Error",
            description="You are already on an active shift! Use `/end-shift` to finish your shift.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Store original display name
    original_nick = interaction.user.nick if interaction.user.nick else interaction.user.name

    # Record shift
    active_shifts[user_id] = {
        "start_time": time.time(),
        "original_nick": original_nick
    }

    # Update nickname
    new_nick = f"{PREFIX}{original_nick}"
    nick_updated = True
    try:
        # Truncate if nickname exceeds Discord's 32 character limit
        if len(new_nick) > 32:
            new_nick = new_nick[:32]
        await interaction.user.edit(nick=new_nick)
    except discord.Forbidden:
        nick_updated = False  # Bot lacks permissions or user is server owner/higher role

    embed = discord.Embed(
        title="Shift Started",
        description=f"**Staff Member:** {interaction.user.mention}\n**Start Time:** <t:{int(time.time())}:F>",
        color=discord.Color.green()
    )
    if not nick_updated:
        embed.set_footer(text="Warning: Couldn't change nickname (check bot role hierarchy/permissions).")
    else:
        embed.set_footer(text="ERLC Staff Management System")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="end-shift", description="End your staff shift and restore your nickname.")
async def end_shift(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in active_shifts:
        embed = discord.Embed(
            title="Shift Error",
            description="You do not have an active shift to end.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    shift_data = active_shifts.pop(user_id)
    start_time = shift_data["start_time"]
    original_nick = shift_data["original_nick"]
    end_time = time.time()
    duration = end_time - start_time

    # Save to leaderboard memory
    shift_leaderboard_data[user_id] = shift_leaderboard_data.get(user_id, 0.0) + duration

    # Restore original nickname
    nick_restored = True
    try:
        current_nick = interaction.user.nick or ""
        if current_nick.startswith(PREFIX):
            await interaction.user.edit(nick=original_nick)
    except discord.Forbidden:
        nick_restored = False

    embed = discord.Embed(
        title="Shift Summary",
        description=f"Shift ended for {interaction.user.mention}",
        color=discord.Color.blue()
    )
    embed.add_field(name="Start Time", value=f"<t:{int(start_time)}:F>", inline=False)
    embed.add_field(name="End Time", value=f"<t:{int(end_time)}:F>", inline=False)
    embed.add_field(name="Total Duration", value=f"`{format_duration(duration)}`", inline=True)
    embed.add_field(
        name="Cumulative Duty Time",
        value=f"`{format_duration(shift_leaderboard_data[user_id])}`",
        inline=True
    )

    if not nick_restored:
        embed.set_footer(text="Warning: Couldn't restore nickname automatically.")
    else:
        embed.set_footer(text="ERLC Staff Management System")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="shift-leaderboard", description="View total logged shift time across all staff.")
async def shift_leaderboard(interaction: discord.Interaction):
    if not shift_leaderboard_data:
        embed = discord.Embed(
            title="Shift Leaderboard",
            description="No shift data logged yet.",
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed)
        return

    # Sort leaderboard by total shift duration descending
    sorted_staff = sorted(shift_leaderboard_data.items(), key=lambda item: item[1], reverse=True)

    leaderboard_text = ""
    for index, (uid, total_secs) in enumerate(sorted_staff[:10], start=1):
        leaderboard_text += f"**{index}.** <@{uid}> — `{format_duration(total_secs)}`\n"

    embed = discord.Embed(
        title="Staff Shift Leaderboard",
        description=leaderboard_text,
        color=discord.Color.gold()
    )
    embed.set_footer(text="ERLC Staff Management System")

    await interaction.response.send_message(embed=embed)


# --- RUN BOT ---
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise ValueError("DISCORD_TOKEN environment variable not set in Replit Secrets.")

bot.run(token)