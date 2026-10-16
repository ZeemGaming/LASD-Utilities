import os
import json
import re
import time
import random
import datetime
import asyncio
import tempfile
import io
from zoneinfo import ZoneInfo
from aiohttp import ClientSession, ClientTimeout, web
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.errors import HTTPException, NotFound, Forbidden


class RateLimitHandler:
    """Handles Discord API rate limits with exponential backoff and bucket tracking."""

    def __init__(self):
        self.buckets: dict[str, dict] = {}
        self.global_lock = asyncio.Lock()
        self.global_reset: float = 0

    def _get_bucket_key(self, method: str, path: str, major_params: dict = None) -> str:
        """Generate a bucket key for the rate limit bucket."""
        if major_params:
            param_str = "&".join(f"{k}={v}" for k, v in sorted(major_params.items()))
            return f"{method}:{path}:{param_str}"
        return f"{method}:{path}"

    async def _wait_for_bucket(self, bucket_key: str) -> None:
        """Wait if the bucket is rate limited."""
        async with self.global_lock:
            bucket = self.buckets.get(bucket_key)
            if not bucket:
                return

            now = time.time()
            if bucket.get("reset_at", 0) > now:
                wait_time = bucket["reset_at"] - now + 0.1  # Small buffer
                await asyncio.sleep(wait_time)

    def _update_bucket(self, bucket_key: str, headers: dict) -> None:
        """Update bucket state from response headers."""
        # Discord rate limit headers:
        # X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, X-RateLimit-Reset-After
        # X-RateLimit-Bucket, X-RateLimit-Scope
        bucket = self.buckets.setdefault(bucket_key, {})

        if "X-RateLimit-Remaining" in headers:
            bucket["remaining"] = int(headers["X-RateLimit-Remaining"])

        if "X-RateLimit-Reset-After" in headers:
            reset_after = float(headers["X-RateLimit-Reset-After"])
            bucket["reset_at"] = time.time() + reset_after
        elif "X-RateLimit-Reset" in headers:
            reset_ts = float(headers["X-RateLimit-Reset"])
            bucket["reset_at"] = reset_ts

        if "X-RateLimit-Bucket" in headers:
            bucket["bucket_id"] = headers["X-RateLimit-Bucket"]

        if "X-RateLimit-Global" in headers and headers["X-RateLimit-Global"] == "true":
            self.global_reset = time.time() + float(headers.get("X-RateLimit-Reset-After", 1))

    def _handle_429(self, bucket_key: str, headers: dict) -> float:
        """Calculate wait time from 429 response."""
        # 429 response includes Retry-After header (seconds)
        retry_after = float(headers.get("Retry-After", 1))
        # Add jitter to prevent thundering herd
        jitter = random.uniform(0, 0.5)
        total_wait = retry_after + jitter

        bucket = self.buckets.setdefault(bucket_key, {})
        bucket["reset_at"] = time.time() + total_wait
        bucket["remaining"] = 0

        return total_wait

    async def execute_with_retry(
        self,
        coro_factory,
        bucket_key: str,
        max_retries: int = 3,
        base_delay: float = 1.0
    ):
        """Execute a coroutine with rate limit handling and exponential backoff."""
        await self._wait_for_bucket(bucket_key)

        for attempt in range(max_retries):
            try:
                result = await coro_factory()
                return result
            except HTTPException as e:
                if e.status == 429:
                    # Rate limited - wait and retry
                    wait_time = self._handle_429(bucket_key, e.response.headers if e.response else {})
                    print(f"[RATE LIMIT] Hit 429 on {bucket_key}, waiting {wait_time:.2f}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_time)
                    continue
                elif e.status == 403:
                    # Forbidden - don't retry
                    raise
                elif e.status >= 500:
                    # Server error - retry with backoff
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    print(f"[RATE LIMIT] Server error {e.status} on {bucket_key}, retrying in {delay:.2f}s")
                    await asyncio.sleep(delay)
                    continue
                else:
                    # Other client errors - don't retry
                    raise
            except (NotFound, Forbidden):
                # Don't retry these
                raise
            except Exception as e:
                # Unexpected error - retry with backoff
                delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                print(f"[RATE LIMIT] Unexpected error on {bucket_key}: {e}, retrying in {delay:.2f}s")
                await asyncio.sleep(delay)

        # All retries exhausted
        raise HTTPException(None, f"Max retries ({max_retries}) exceeded for {bucket_key}")


# Global rate limit handler
rate_limiter = RateLimitHandler()


async def safe_send(channel, *args, **kwargs):
    """Send a message with rate limit handling."""
    bucket_key = f"POST:/channels/{channel.id}/messages"
    return await rate_limiter.execute_with_retry(
        lambda: channel.send(*args, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_edit(message, *args, **kwargs):
    """Edit a message with rate limit handling."""
    bucket_key = f"PATCH:/channels/{message.channel.id}/messages/{message.id}"
    return await rate_limiter.execute_with_retry(
        lambda: message.edit(*args, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_delete(message, *args, **kwargs):
    """Delete a message with rate limit handling."""
    bucket_key = f"DELETE:/channels/{message.channel.id}/messages/{message.id}"
    return await rate_limiter.execute_with_retry(
        lambda: message.delete(*args, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_add_reaction(message, emoji, *args, **kwargs):
    """Add reaction with rate limit handling."""
    bucket_key = f"PUT:/channels/{message.channel.id}/messages/{message.id}/reactions/{emoji}/@me"
    return await rate_limiter.execute_with_retry(
        lambda: message.add_reaction(emoji, *args, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_fetch_guild(bot, guild_id, *args, **kwargs):
    """Fetch guild with rate limit handling."""
    bucket_key = f"GET:/guilds/{guild_id}"
    return await rate_limiter.execute_with_retry(
        lambda: bot.fetch_guild(guild_id, *args, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_fetch_channel(guild, channel_id, *args, **kwargs):
    """Fetch channel with rate limit handling."""
    bucket_key = f"GET:/channels/{channel_id}"
    return await rate_limiter.execute_with_retry(
        lambda: guild.fetch_channel(channel_id, *args, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_fetch_member(guild, user_id, *args, **kwargs):
    """Fetch member with rate limit handling."""
    bucket_key = f"GET:/guilds/{guild.id}/members/{user_id}"
    return await rate_limiter.execute_with_retry(
        lambda: guild.fetch_member(user_id, *args, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_fetch_user(bot, user_id, *args, **kwargs):
    """Fetch user with rate limit handling."""
    bucket_key = f"GET:/users/{user_id}"
    return await rate_limiter.execute_with_retry(
        lambda: bot.fetch_user(user_id, *args, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_edit_member(member, *args, **kwargs):
    """Edit member (nickname, roles) with rate limit handling."""
    bucket_key = f"PATCH:/guilds/{member.guild.id}/members/{member.id}"
    return await rate_limiter.execute_with_retry(
        lambda: member.edit(*args, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_add_roles(member, *roles, **kwargs):
    """Add roles with rate limit handling."""
    bucket_key = f"PUT:/guilds/{member.guild.id}/members/{member.id}/roles"
    return await rate_limiter.execute_with_retry(
        lambda: member.add_roles(*roles, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_remove_roles(member, *roles, **kwargs):
    """Remove roles with rate limit handling."""
    bucket_key = f"DELETE:/guilds/{member.guild.id}/members/{member.id}/roles"
    return await rate_limiter.execute_with_retry(
        lambda: member.remove_roles(*roles, **kwargs),
        bucket_key,
        max_retries=3
    )


async def safe_send_dm(user, *args, **kwargs):
    """Send a DM with rate limit handling."""
    bucket_key = f"POST:/users/@me/channels"
    # DMs use a different endpoint - create DM channel first, then send
    async def _send_dm():
        dm_channel = user.dm_channel or await user.create_dm()
        return await dm_channel.send(*args, **kwargs)
    return await rate_limiter.execute_with_retry(
        _send_dm,
        bucket_key,
        max_retries=3
    )


async def safe_add_reaction_channel(channel, message_id, emoji, *args, **kwargs):
    """Add reaction via channel with rate limit handling."""
    bucket_key = f"PUT:/channels/{channel.id}/messages/{message_id}/reactions/{emoji}/@me"
    return await rate_limiter.execute_with_retry(
        lambda: channel.add_reaction(message_id, emoji, *args, **kwargs),
        bucket_key,
        max_retries=3
    )

# --- CONFIGURATION ---
GUILD_ID = 1538076681565241384
QUOTA_LOG_CHANNEL_ID = 1544780775948419082
STRIKE_LOG_CHANNEL_ID = 1538076682664017944
ARREST_LOG_CHANNEL_ID = 1538076682441724001
CITE_LOG_CHANNEL_ID = 1538076682441724002
VERIFICATION_CHANNEL_ID = 1545124075989504170
VERIFIED_ROLE_ID = 1545109313276878879
MARKET_CHANNEL_ID = 1549878344391069716
MARKET_LOG_CHANNEL_ID = 1550934250256343141
FAST_PASS_LOG_CHANNEL_ID = 1551079632621535232
UNVERIFIED_ROLE_ID = 1545126033433559140
REQUIRES_TRAINING_ROLE_ID = 1538076681607192664
RA_ROLE_ID = 1538076681607192663
TRAINING_REQUEST_CHANNEL_ID = 1538076682894843966
QUOTA_REQUIRED_SECONDS = 5400  # 1h 30m
WEB_PORT = int(os.getenv("PORT", "5000"))
VERIFICATION_DATA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "verification.json"
)
ADMIN_TOKENS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "admin.json"
)

PREFIX = "On Shift | "

# User IDs allowed for admin commands
ALLOWED_USER_IDS = {
    1422647977880584324,  # @zeemonyt. (Secondary ID)
}

WEB_PANEL_ROLE_ID = 1538076681586217031

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

MR_PLUS_ROLE_IDS = {
    1538076681628028956,
    1538076681628028955,
    1538076681628028957,
    1538076681632219216,
    1538076681640747130,
    1538076681640747131,
    1539413609388712006,
    1538076681640747132,
    1538076681640747133,
    1538076681640747135,
    1538076681640747136,
    1539431991080910899
} | ALLOWED_ROLE_IDS

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


WEBSITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LASD-Utilities-Website")


async def website_handler(request: web.Request) -> web.StreamResponse:
    """Serves the LASD Utilities website static files."""
    path = request.path
    if path == "/" or path == "":
        file_path = os.path.join(WEBSITE_DIR, "index.html")
    else:
        clean_path = path.lstrip("/")
        # Prevent directory traversal
        safe_path = os.path.normpath(clean_path)
        if safe_path.startswith(".."):
            return web.Response(text="Forbidden", status=403)
        file_path = os.path.join(WEBSITE_DIR, safe_path)

    if os.path.isfile(file_path):
        return web.FileResponse(file_path)

    html_path = file_path + ".html"
    if os.path.isfile(html_path):
        return web.FileResponse(html_path)

    not_found_path = os.path.join(WEBSITE_DIR, "404.html")
    if os.path.isfile(not_found_path):
        return web.FileResponse(not_found_path, status=404)

    return web.Response(text="Not Found", status=404)


async def health_handler(request: web.Request) -> web.Response:
    """Returns the plaintext health response for GET and HEAD requests."""
    return web.Response(
        text="Bot is alive!",
        content_type="text/plain"
    )


async def discord_callback_handler(request: web.Request) -> web.StreamResponse:
    code = request.query.get("code")
    if not code:
        return web.Response(text="Error: Missing authorization code.", status=400)

    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    if not client_id:
        callback_url_secret = os.getenv("DISCORD_CALLBACK_URL", "")
        if "client_id=" in callback_url_secret:
            try:
                import urllib.parse
                parsed_url = urllib.parse.urlparse(callback_url_secret)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                if "client_id" in query_params:
                    client_id = query_params["client_id"][0]
            except Exception:
                pass
    client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
    base_url = get_base_url(request)
    redirect_uri = f"{base_url}/callback"

    if not client_id or not client_secret or not redirect_uri:
        return web.Response(text="Error: OAuth2 configuration missing on server.", status=500)

    token_url = "https://discord.com/api/oauth2/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    timeout = ClientTimeout(total=10)
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.post(token_url, data=payload, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return web.Response(text=f"Error exchanging token: HTTP {resp.status} - {text}", status=400)
                token_data = await resp.json()
                access_token = token_data.get("access_token")

        if not access_token:
            return web.Response(text="Error: Failed to obtain access token from Discord.", status=400)

        async with ClientSession(timeout=timeout) as session:
            async with session.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
                if resp.status != 200:
                    return web.Response(text="Error fetching user profile.", status=400)
                user_data = await resp.json()
                user_id = int(user_data["id"])

            async with session.get(f"https://discord.com/api/users/@me/guilds/{GUILD_ID}/member", headers={"Authorization": f"Bearer {access_token}"}) as resp:
                if resp.status != 200:
                    return web.Response(text="Access Denied: You must be a member of the LASD Discord server.", status=403)
                member_data = await resp.json()
                user_roles = [int(r) for r in member_data.get("roles", [])]

    except Exception as e:
        import traceback
        traceback.print_exc()
        return web.Response(text=f"OAuth2 Error: {e}", status=500)

    is_authorized = False
    if user_id in ALLOWED_USER_IDS:
        is_authorized = True
    else:
        for role_id in user_roles:
            if role_id == WEB_PANEL_ROLE_ID:
                is_authorized = True
                break

    if not is_authorized:
        return web.Response(
            text="Access Denied: You do not have the required role (1538076681586217031) to access the admin panel.",
            status=403
        )

    # Generate a new secure token and remove any existing token for this user
    admin_tokens = load_admin_tokens()
    # Remove old token(s) for this user_id
    admin_tokens = {token: uid for token, uid in admin_tokens.items() if uid != user_id}
    # Generate new token
    new_token = generate_admin_token()
    admin_tokens[new_token] = user_id
    save_admin_tokens(admin_tokens)

    response = web.HTTPFound(location="/admin")
    # Use secure=True only when behind HTTPS (check X-Forwarded-Proto header)
    forwarded_proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    is_secure = forwarded_proto == "https"
    response.set_cookie("lasd_admin_auth", new_token, max_age=86400, secure=is_secure, httponly=True, samesite="Lax")
    return response


def get_base_url(request: web.Request) -> str:
    forwarded_host = request.headers.get("X-Forwarded-Host")
    forwarded_proto = request.headers.get("X-Forwarded-Proto")
    host = forwarded_host or request.host
    proto = forwarded_proto or request.scheme
    return f"{proto}://{host}"


def get_oauth_login_url(request: web.Request = None) -> str:
    import urllib.parse
    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    if not client_id:
        callback_url_secret = os.getenv("DISCORD_CALLBACK_URL", "")
        if "client_id=" in callback_url_secret:
            try:
                import urllib.parse
                parsed_url = urllib.parse.urlparse(callback_url_secret)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                if "client_id" in query_params:
                    client_id = query_params["client_id"][0]
            except Exception:
                pass

    redirect_uri = ""
    if request:
        base_url = get_base_url(request)
        redirect_uri = f"{base_url}/callback"
    else:
        redirect_uri = os.getenv("DISCORD_CALLBACK", "") or os.getenv("DISCORD_CALLBACK_URL", "")
        if redirect_uri.startswith("https://discord.com/api/oauth2/authorize"):
            redirect_uri = "https://lasd-utilities.onrender.com/callback"

    if client_id and redirect_uri:
        return f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}&response_type=code&scope=identify%20guilds.members.read"
    return ""


def get_auth_user_id(request: web.Request) -> int | None:
    auth_cookie = request.cookies.get("lasd_admin_auth")
    if auth_cookie:
        admin_tokens = load_admin_tokens()
        user_id = admin_tokens.get(auth_cookie)
        if user_id is not None:
            return user_id
    return None


SHIFTS_FILE = "shifts.json"
RECORDS_FILE = "records.json"
MERIT_POINTS_FILE = "Merit-Points.json"


def save_shift_data():
    """Saves shift and record data to JSON files (atomic writes)."""
    # Atomic write for shifts
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=".", prefix=".shifts-", suffix=".tmp", delete=False
        ) as temp_file:
            temp_path = temp_file.name
            json.dump({
                "active": {str(k): v for k, v in active_shifts.items()},
                "leaderboard": {str(k): v for k, v in shift_leaderboard_data.items()},
                "last": {str(k): v for k, v in last_shifts.items()}
            }, temp_file, indent=4)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, SHIFTS_FILE)
    except Exception as e:
        print(f"Error saving shifts: {e}")
        if temp_path:
            try: os.unlink(temp_path)
            except: pass

    # Atomic write for records
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=".", prefix=".records-", suffix=".tmp", delete=False
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(records_list, temp_file, indent=4)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, RECORDS_FILE)
    except Exception as e:
        print(f"Error saving records: {e}")
        if temp_path:
            try: os.unlink(temp_path)
            except: pass


def load_shift_data():
    """Loads shift and record data from JSON files."""
    global active_shifts, shift_leaderboard_data, last_shifts, records_list
    if os.path.exists(SHIFTS_FILE):
        try:
            with open(SHIFTS_FILE, "r") as f:
                data = json.load(f)
                active_shifts = {int(k): v for k, v in data.get("active", {}).items()}
                shift_leaderboard_data = {int(k): v for k, v in data.get("leaderboard", {}).items()}
                last_shifts = {int(k): v for k, v in data.get("last", {}).items()}
        except Exception as e:
            print(f"Error loading shift data: {e}")
    if os.path.exists(RECORDS_FILE):
        try:
            with open(RECORDS_FILE, "r") as f:
                records_list = [r for r in json.load(f) if r.get("plate") != "EXAMPLE"]
        except Exception as e:
            print(f"Error loading records: {e}")
    else:
        records_list = []
        save_shift_data()


def save_merit_points():
    """Saves merit points data to JSON file."""
    try:
        with open(MERIT_POINTS_FILE, "w") as f:
            json.dump({str(k): v for k, v in merit_points.items()}, f, indent=4)
    except Exception as e:
        print(f"Error saving merit points: {e}")


def load_merit_points():
    """Loads merit points data from JSON file."""
    global merit_points
    if os.path.exists(MERIT_POINTS_FILE):
        try:
            with open(MERIT_POINTS_FILE, "r") as f:
                data = json.load(f)
                merit_points = {int(k): v for k, v in data.items()}
        except Exception as e:
            print(f"Error loading merit points: {e}")
    else:
        merit_points = {}
        save_merit_points()


async def add_merit_points(user_id: int, amount: int, source: str = "Unknown", description: str = "") -> int:
    """Add merit points to a user. Returns new total."""
    global merit_points, merit_transactions
    async with merit_lock:
        merit_points[user_id] = merit_points.get(user_id, 0) + amount
        save_merit_points()

        # Log transaction
        if user_id not in merit_transactions:
            merit_transactions[user_id] = []
        merit_transactions[user_id].append({
            "amount": amount,
            "source": source,
            "description": description,
            "timestamp": int(time.time())
        })
        save_merit_transactions()
    return merit_points[user_id]


async def remove_merit_points(user_id: int, amount: int, source: str = "Admin", description: str = "") -> int:
    """Remove merit points from a user. Returns new total (min 0)."""
    global merit_points, merit_transactions
    async with merit_lock:
        current = merit_points.get(user_id, 0)
        merit_points[user_id] = max(0, current - amount)
        save_merit_points()

        # Log transaction
        if user_id not in merit_transactions:
            merit_transactions[user_id] = []
        merit_transactions[user_id].append({
            "amount": -amount,
            "source": source,
            "description": description,
            "timestamp": int(time.time())
        })
        save_merit_transactions()
    return merit_points[user_id]


def get_merit_points(user_id: int) -> int:
    """Get a user's merit points."""
    return merit_points.get(user_id, 0)


def get_merit_transactions(user_id: int) -> list:
    """Get a user's merit point transaction history."""
    return merit_transactions.get(user_id, [])


def save_merit_transactions():
    """Saves merit transactions data to JSON file."""
    try:
        with open(MERIT_POINTS_FILE + ".transactions", "w") as f:
            json.dump({str(k): v for k, v in merit_transactions.items()}, f, indent=4)
    except Exception as e:
        print(f"Error saving merit transactions: {e}")


def load_merit_transactions():
    """Loads merit transactions data from JSON file."""
    global merit_transactions
    transactions_file = MERIT_POINTS_FILE + ".transactions"
    if os.path.exists(transactions_file):
        try:
            with open(transactions_file, "r") as f:
                data = json.load(f)
                merit_transactions = {int(k): v for k, v in data.items()}
        except Exception as e:
            print(f"Error loading merit transactions: {e}")
    else:
        merit_transactions = {}
        save_merit_transactions()


async def check_user_web_panel_auth(request: web.Request) -> bool:
    auth_cookie = request.cookies.get("lasd_admin_auth")
    if not auth_cookie:
        return False

    # Validate token and get user_id
    admin_tokens = load_admin_tokens()
    user_id = admin_tokens.get(auth_cookie)
    if user_id is None:
        return False

    if user_id in ALLOWED_USER_IDS:
        return True

    if bot.is_ready():
        try:
            guild = bot.get_guild(GUILD_ID)
            if not guild:
                guild = await asyncio.wait_for(bot.fetch_guild(GUILD_ID), timeout=3.0)
            if guild:
                member = guild.get_member(user_id)
                if not member:
                    member = await asyncio.wait_for(guild.fetch_member(user_id), timeout=3.0)
                if member and any(r.id == WEB_PANEL_ROLE_ID for r in member.roles):
                    return True
        except Exception:
            pass
    return False


async def admin_handler(request: web.Request) -> web.StreamResponse:
    if not await check_user_web_panel_auth(request):
        return web.HTTPFound(location=os.getenv("DISCORD_CALLBACK_URL", ""))

    file_path = os.path.join(WEBSITE_DIR, "panel-frontend.html")
    if os.path.isfile(file_path):
        return web.FileResponse(file_path)

    return web.Response(text="Admin panel frontend not found.", status=404)


async def admin_hr_handler(request: web.Request) -> web.StreamResponse:
    if not await check_user_web_panel_auth(request):
        return web.HTTPFound(location=os.getenv("DISCORD_CALLBACK_URL", ""))

    file_path = os.path.join(WEBSITE_DIR, "panel-hr-frontend.html")
    if os.path.isfile(file_path):
        return web.FileResponse(file_path)

    return web.Response(text="HR panel frontend not found.", status=404)


async def api_user_handler(request: web.Request) -> web.Response:
    user_id = get_auth_user_id(request)
    if user_id is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    username = "LASD Deputy"
    avatar = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f464.png"
    roles = ["Verified Staff"]

    if bot.is_ready():
        try:
            guild = bot.get_guild(GUILD_ID)
            if not guild:
                try:
                    guild = await asyncio.wait_for(bot.fetch_guild(GUILD_ID), timeout=3.0)
                except Exception:
                    guild = None

            member = None
            if guild:
                member = guild.get_member(user_id)
                if not member:
                    try:
                        member = await asyncio.wait_for(guild.fetch_member(user_id), timeout=3.0)
                    except Exception:
                        member = None

            if member:
                username = member.display_name
                avatar = str(member.display_avatar.url)
                roles = [r.name for r in member.roles if r.name != "@everyone"]
            else:
                try:
                    user = await asyncio.wait_for(bot.fetch_user(user_id), timeout=3.0)
                    if user:
                        username = user.name
                        avatar = str(user.display_avatar.url)
                        roles = []
                except Exception:
                    pass
        except Exception:
            pass

    shift = active_shifts.get(user_id)
    shift_info = {
        "active": shift is not None,
        "on_break": shift["on_break"] if shift else False,
        "start_time": shift["start_time"] if shift else None,
        "break_time": shift["break_time"] if shift else 0.0,
        "break_start": shift["break_start"] if shift else 0.0
    }

    total_secs = shift_leaderboard_data.get(user_id, 0.0)
    if shift:
        current_break = (time.time() - shift["break_start"]) if shift["on_break"] else 0.0
        elapsed = time.time() - shift["start_time"] - shift["break_time"] - current_break
        total_secs += max(0.0, elapsed)

    quota_info = {
        "seconds": total_secs,
        "required": QUOTA_REQUIRED_SECONDS,
        "met": total_secs >= QUOTA_REQUIRED_SECONDS
    }

    merit_info = {
        "points": merit_points.get(user_id, 0)
    }

    return web.json_response({
        "id": user_id,
        "username": username,
        "avatar": avatar,
        "roles": roles,
        "shift": shift_info,
        "quota": quota_info,
        "merit": merit_info
    })

async def api_shift_start_handler(request: web.Request) -> web.Response:
    user_id = get_auth_user_id(request)
    if user_id is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    async with shift_lock:
        if user_id in active_shifts:
            return web.json_response({"error": "Shift already active"}, status=400)

        guild = bot.get_guild(GUILD_ID)
        if not guild:
            try:
                guild = await bot.fetch_guild(GUILD_ID)
            except Exception as e:
                print(f"Error fetching guild {GUILD_ID}: {e}")

        member = None
        if guild:
            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception as e:
                    print(f"Error fetching member {user_id} in guild: {e}")

        if not member:
            return web.json_response({"error": "Member not found in staff server (ensure bot is running and user is in server)"}, status=400)

        raw_nick = member.nick if member.nick else member.name
        original_nick = raw_nick[len(PREFIX):] if raw_nick.startswith(PREFIX) else raw_nick

        active_shifts[user_id] = {
            "start_time": time.time(),
            "original_nick": original_nick,
            "break_time": 0.0,
            "on_break": False,
            "break_start": 0.0
        }
        save_shift_data()
    await apply_on_shift_nick(member, original_nick)
    return web.json_response({"success": True, "message": "Shift started successfully."})

async def api_shift_break_handler(request: web.Request) -> web.Response:
    user_id = get_auth_user_id(request)
    if user_id is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    async with shift_lock:
        if user_id not in active_shifts:
            return web.json_response({"error": "No active shift"}, status=400)

        shift_data = active_shifts[user_id]
        if not shift_data["on_break"]:
            shift_data["on_break"] = True
            shift_data["break_start"] = time.time()
            status = "break_started"
        else:
            break_duration = time.time() - shift_data["break_start"]
            shift_data["break_time"] += break_duration
            shift_data["on_break"] = False
            shift_data["break_start"] = 0.0
            status = "break_ended"

        save_shift_data()
        return web.json_response({"success": True, "status": status})

async def api_shift_end_handler(request: web.Request) -> web.Response:
    user_id = get_auth_user_id(request)
    if user_id is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    async with shift_lock:
        if user_id not in active_shifts:
            return web.json_response({"error": "No active shift"}, status=400)

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

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        try:
            guild = await bot.fetch_guild(GUILD_ID)
        except Exception:
            pass

    member = None
    if guild:
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                pass

    if member:
        await restore_nick(member, shift_data["original_nick"])

    return web.json_response({"success": True, "duration": active_duty_time})

async def api_arrest_handler(request: web.Request) -> web.Response:
    user_id = get_auth_user_id(request)
    if user_id is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    suspect = ""
    reason = ""
    plate = ""
    court_date = "Not scheduled"
    files = []

    if request.content_type == "multipart/form-data":
        reader = await request.multipart()
        async for field in reader:
            if field.name in ["user", "suspect"]:
                suspect = await field.text()
            elif field.name in ["reason", "charges", "details"]:
                val = await field.text()
                if reason:
                    reason += f" | {val}"
                else:
                    reason = val
            elif field.name == "plate":
                plate = await field.text()
            elif field.name == "court_date":
                court_date = await field.text() or "Not scheduled"
            elif field.name in ["image", "image1", "image2"]:
                filename = field.filename or "evidence.png"
                file_bytes = await field.read()
                if file_bytes:
                    files.append(discord.File(io.BytesIO(file_bytes), filename=filename))
    else:
        try:
            data = await request.json()
            suspect = data.get("user") or data.get("suspect", "")
            charges = data.get("charges", "")
            details = data.get("details", "")
            notes = data.get("notes", "")
            reason = f"{charges} - {details}"
            if notes:
                reason += f" (Notes: {notes})"
            plate = data.get("plate", "")
            court_date = data.get("court_date", "Not scheduled")
        except Exception:
            pass

    if not suspect or not reason:
        return web.json_response({"error": "Missing required fields"}, status=400)

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        try:
            guild = await bot.fetch_guild(GUILD_ID)
        except Exception:
            pass

    channel = guild.get_channel(ARREST_LOG_CHANNEL_ID) if guild else None
    if not channel and guild:
        try:
            channel = await guild.fetch_channel(ARREST_LOG_CHANNEL_ID)
        except Exception:
            pass

    member = guild.get_member(user_id) if guild else None
    if not member and guild:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            pass

    officer_mention = member.mention if member else f"<@{user_id}>"

    plate_display = plate.strip().upper()[:10] if plate else "N/A"

    content = (
        f"User: {suspect}\n"
        f"Reason for arrest: {reason}\n"
        f"Court date: {court_date}\n"
        f"Plate: {plate_display}\n"
        f"Arresting Officer: {officer_mention}\n"
        f"Image:"
    )

    if not channel:
        return web.json_response({"error": "Arrest channel not found"}, status=500)

    try:
        if files:
            await safe_send(channel, content=content, files=files, allowed_mentions=discord.AllowedMentions(users=[member] if member else []))
        else:
            await safe_send(channel, content=content, allowed_mentions=discord.AllowedMentions(users=[member] if member else []))
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    # Add to records list so it updates utilities panel / records
    new_record = {
        "id": int(time.time() * 1000),
        "type": "arrest",
        "plate": plate_display,
        "vehicle": "Unknown",
        "subject": suspect,
        "secondary": "Arrest Record",
        "description": reason,
        "timestamp": time.time() * 1000
    }
    async with shift_lock:
        records_list.insert(0, new_record)
        save_shift_data()

    # Award $10 merit points for arrest
    async with merit_lock:
        add_merit_points(user_id, 10, source="Arrest", description="Arrest log submitted via web panel")

    return web.json_response({"success": True, "merit_earned": 10})

async def api_citation_handler(request: web.Request) -> web.Response:
    user_id = get_auth_user_id(request)
    if user_id is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    suspect = ""
    reason = ""
    court_date = "Not scheduled"
    plate = ""
    discord_file = None

    if request.content_type == "multipart/form-data":
        reader = await request.multipart()
        async for field in reader:
            if field.name in ["user", "suspect"]:
                suspect = await field.text()
            elif field.name in ["reason", "violation", "fine"]:
                val = await field.text()
                if reason:
                    reason += f" | {val}"
                else:
                    reason = val
            elif field.name == "court_date":
                court_date = await field.text() or "Not scheduled"
            elif field.name == "plate":
                plate = await field.text()
            elif field.name == "image":
                filename = field.filename or "evidence.png"
                file_bytes = await field.read()
                if file_bytes:
                    discord_file = discord.File(io.BytesIO(file_bytes), filename=filename)
    else:
        try:
            data = await request.json()
            suspect = data.get("user") or data.get("suspect", "")
            violation = data.get("violation", "")
            fine = data.get("fine", "")
            notes = data.get("notes", "")
            reason = f"{violation} - {fine}"
            if notes:
                reason += f" (Notes: {notes})"
            court_date = data.get("court_date", "Not scheduled")
            plate = data.get("plate", "")
        except Exception:
            pass

    if not suspect or not reason:
        return web.json_response({"error": "Missing required fields"}, status=400)

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        try:
            guild = await bot.fetch_guild(GUILD_ID)
        except Exception:
            pass

    channel = guild.get_channel(CITE_LOG_CHANNEL_ID) if guild else None
    if not channel and guild:
        try:
            channel = await guild.fetch_channel(CITE_LOG_CHANNEL_ID)
        except Exception:
            pass

    member = guild.get_member(user_id) if guild else None
    if not member and guild:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            pass

    officer_mention = member.mention if member else f"<@{user_id}>"

    plate_display = plate.strip().upper()[:10] if plate else "N/A"

    content = (
        f"User: {suspect}\n"
        f"Reason: {reason}\n"
        f"Court date: {court_date}\n"
        f"Plate: {plate_display}\n"
        f"Submitted by: {officer_mention}\n"
        f"Image:"
    )

    if not channel:
        return web.json_response({"error": "Citation channel not found"}, status=500)

    try:
        if discord_file:
            await safe_send(channel, content=content, file=discord_file, allowed_mentions=discord.AllowedMentions(users=[member] if member else []))
        else:
            await safe_send(channel, content=content, allowed_mentions=discord.AllowedMentions(users=[member] if member else []))
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    # Add to records list so it updates utilities panel / records
    new_record = {
        "id": int(time.time() * 1000),
        "type": "citation",
        "plate": plate_display,
        "vehicle": "Unknown",
        "subject": suspect,
        "secondary": "Citation Record",
        "description": f"{reason} (Court: {court_date})",
        "timestamp": time.time() * 1000
    }
    async with shift_lock:
        records_list.insert(0, new_record)
        save_shift_data()

    return web.json_response({"success": True})


async def api_units_handler(request: web.Request) -> web.Response:
    admin_tokens = load_admin_tokens()
    auth_cookie = request.cookies.get("lasd_admin_auth")
    if not auth_cookie or auth_cookie not in admin_tokens:
        return web.json_response({"error": "Unauthorized"}, status=401)

    guild = bot.get_guild(GUILD_ID)
    units = []

    for user_id, shift_data in active_shifts.items():
        member = guild.get_member(user_id) if guild else None
        if not member and guild:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None

        name = member.display_name if member else f"User {user_id}"
        rank = "Deputy"
        if member and member.roles:
            valid_roles = [r for r in member.roles if r.name != "@everyone"]
            if valid_roles:
                valid_roles.sort(key=lambda r: r.position, reverse=True)
                rank = valid_roles[0].name

        units.append({
            "user_id": user_id,
            "unit_number": shift_data.get("unit_number", f"1-A-{user_id % 100}"),
            "name": name,
            "location": shift_data.get("location", "Patrol Area 1"),
            "rank": rank,
            "status": shift_data.get("cad_status", "10-7")
        })

    return web.json_response({"units": units})


async def api_unit_status_handler(request: web.Request) -> web.Response:
    user_id = get_auth_user_id(request)
    if user_id is None:
        return web.json_response({"error": "Unauthorized"}, status=401)
    admin_tokens = load_admin_tokens()
    auth_cookie = request.cookies.get("lasd_admin_auth")
    if not auth_cookie or auth_cookie not in admin_tokens:
        return web.json_response({"error": "Unauthorized"}, status=401)

    async with shift_lock:
        if user_id not in active_shifts:
            guild = bot.get_guild(GUILD_ID)
            member = guild.get_member(user_id) if guild else None
            if not member and guild:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    member = None
            raw_nick = member.nick if member and member.nick else (member.name if member else "Deputy")
            original_nick = raw_nick[len(PREFIX):] if raw_nick.startswith(PREFIX) else raw_nick
            active_shifts[user_id] = {
                "start_time": time.time(),
                "original_nick": original_nick,
                "break_time": 0.0,
                "on_break": False,
                "break_start": 0.0,
                "cad_status": "10-7",
                "location": "Patrol Area 1",
                "unit_number": f"1-A-{user_id % 100}"
            }
            save_shift_data()

        try:
            data = await request.json()
            new_status = data.get("status")
            new_location = data.get("location")
            unit_number = data.get("unit_number")

            if new_status:
                active_shifts[user_id]["cad_status"] = new_status
            if new_location:
                active_shifts[user_id]["location"] = new_location
            if unit_number:
                active_shifts[user_id]["unit_number"] = unit_number

            save_shift_data()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)


async def api_records_handler(request: web.Request) -> web.Response:
    admin_tokens = load_admin_tokens()
    auth_cookie = request.cookies.get("lasd_admin_auth")
    if not auth_cookie or auth_cookie not in admin_tokens:
        return web.json_response({"error": "Unauthorized"}, status=401)
    return web.json_response({"records": records_list})


async def api_records_post_handler(request: web.Request) -> web.Response:
    admin_tokens = load_admin_tokens()
    auth_cookie = request.cookies.get("lasd_admin_auth")
    if not auth_cookie or auth_cookie not in admin_tokens:
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
        new_record = {
            "id": int(time.time() * 1000),
            "type": data.get("type", "note"),
            "plate": data.get("plate", "UNKNOWN").upper()[:10],
            "vehicle": data.get("vehicle", "Unknown"),
            "subject": data.get("subject", "Unknown"),
            "secondary": data.get("secondary", ""),
            "description": data.get("description", ""),
            "timestamp": time.time() * 1000
        }
        async with shift_lock:
            records_list.insert(0, new_record)
            save_shift_data()
        return web.json_response({"success": True, "record": new_record})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def api_records_delete_handler(request: web.Request) -> web.Response:
    admin_tokens = load_admin_tokens()
    auth_cookie = request.cookies.get("lasd_admin_auth")
    if not auth_cookie or auth_cookie not in admin_tokens:
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        record_id = int(request.query.get('id', 0))
        async with shift_lock:
            global records_list
            records_list = [r for r in records_list if r.get('id') != record_id]
            save_shift_data()
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def start_web_server() -> web.AppRunner:
    """Starts the website and health server on the same async loop as the Discord bot."""
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/status", health_handler)
    app.router.add_get("/callback", discord_callback_handler)
    app.router.add_get("/callback/", discord_callback_handler)
    app.router.add_get("/admin", admin_handler)
    app.router.add_get("/admin/", admin_handler)
    app.router.add_get("/admin/HR", admin_hr_handler)
    app.router.add_get("/admin/HR/", admin_hr_handler)
    app.router.add_get("/api/user", api_user_handler)
    app.router.add_post("/api/shift/start", api_shift_start_handler)
    app.router.add_post("/api/shift/break", api_shift_break_handler)
    app.router.add_post("/api/shift/end", api_shift_end_handler)
    app.router.add_post("/api/arrest", api_arrest_handler)
    app.router.add_post("/api/citation", api_citation_handler)
    app.router.add_get("/api/units", api_units_handler)
    app.router.add_post("/api/units/status", api_unit_status_handler)
    app.router.add_get("/api/records", api_records_handler)
    app.router.add_post("/api/records", api_records_post_handler)
    app.router.add_delete("/api/records", api_records_delete_handler)
    app.router.add_get("/{tail:.*}", website_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"LASD Utilities website and health server listening on port {WEB_PORT}.")
    return runner


# Active shifts memory: { user_id: { "start_time": float, "original_nick": str, "break_time": float, "on_break": bool, "break_start": float } }
active_shifts = {}

# Saved cumulative shift history: { user_id: total_seconds_floated }
shift_leaderboard_data = {}

# Logs: [ { "id": int, "type": str, "plate": str, "vehicle": str, "subject": str, "secondary": str, "description": str, "timestamp": float } ]
records_list = []

# Last completed shift duration memory for clearing: { user_id: last_shift_seconds_floated }
last_shifts = {}

# User strikes database: { user_id: [ { "case_id": str, "moderator_id": int, "reason": str, "timestamp": int } ] }
user_strikes = {}

# Merit points currency: { user_id: int }
merit_points = {}

# Merit points transaction history: { user_id: [ { "amount": int, "source": str, "description": str, "timestamp": int } ] }
merit_transactions = {}

# Date of the most recent Saturday quota reset, persisted with shift data
last_quota_reset_date = None

# User IDs already warned about an active shift lasting over 10 hours
shift_farming_alerted = set()

# Persistent verification message and in-progress verification requests
verification_message_id = None
pending_verifications = {}
verification_views_registered = False

# Market/Shop message
market_message_id = None
market_views_registered = False

# Steal cooldowns: { user_id: timestamp of last steal attempt }
steal_cooldowns = {}

# Locks for thread-safe access to shared state
shift_lock = asyncio.Lock()
merit_lock = asyncio.Lock()
strikes_lock = asyncio.Lock()
records_lock = asyncio.Lock()
verification_lock = asyncio.Lock()
steal_lock = asyncio.Lock()


def save_verification_data() -> bool:
    """Persist the verification message ID, last quota reset date, and any in-progress requests atomically."""
    data = {
        "verification_message_id": verification_message_id,
        "last_quota_reset_date": last_quota_reset_date,
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
    """Restore the verification message ID, last quota reset date, and pending requests after a restart."""
    global verification_message_id, last_quota_reset_date

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

    saved_reset_date = data.get("last_quota_reset_date")
    if saved_reset_date is None or isinstance(saved_reset_date, str):
        last_quota_reset_date = saved_reset_date
    else:
        print("Warning: Ignoring invalid saved quota reset date.")

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


def parse_money_string(money_str: str) -> int:
    """Parses money strings like '5', '20', '1k', '10k', '1m', '1b', '1t' (k=thousand, m=million, b=billion, t=trillion)."""
    if not money_str:
        return 0
    money_str = money_str.lower().strip()

    multipliers = {
        'k': 1_000,
        'm': 1_000_000,
        'b': 1_000_000_000,
        't': 1_000_000_000_000
    }

    # Check for suffix
    for suffix, multiplier in multipliers.items():
        if money_str.endswith(suffix):
            try:
                num = float(money_str[:-1])
                return int(num * multiplier)
            except ValueError:
                return 0

    # Plain number
    try:
        return int(float(money_str))
    except ValueError:
        return 0


def has_web_panel_role(interaction: discord.Interaction) -> bool:
    """Checks if the user has the required role (1538076681586217031) or is in ALLOWED_USER_IDS."""
    if interaction.user.id in ALLOWED_USER_IDS:
        return True

    if isinstance(interaction.user, discord.Member):
        return any(role.id == WEB_PANEL_ROLE_ID for role in interaction.user.roles)

    return False


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
    """Applies the 'On Shift | ' prefix to a user's nickname safely."""
    clean_nick = original_nick
    if clean_nick.startswith(PREFIX):
        clean_nick = clean_nick[len(PREFIX):]

    new_nick = f"{PREFIX}{clean_nick}"
    if len(new_nick) > 32:
        allowed_len = 32 - len(PREFIX)
        new_nick = f"{PREFIX}{clean_nick[:allowed_len]}"
    try:
        await safe_edit_member(member, nick=new_nick, reason="Started staff shift")
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Error: Could not apply shift nickname for {member} ({member.id}): {e}")
        return False


async def restore_nick(member: discord.Member, original_nick: str) -> bool:
    """Restores the user's original nickname, stripping the shift prefix."""
    try:
        current_nick = member.nick or ""
        clean_original = original_nick
        if clean_original and clean_original.startswith(PREFIX):
            clean_original = clean_original[len(PREFIX):]

        target_nick = clean_original if clean_original != member.name else None

        if current_nick.startswith(PREFIX) or member.nick is not None:
            await safe_edit_member(member, nick=target_nick, reason="Ended staff shift")
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Error: Could not restore nickname for {member} ({member.id}): {e}")
        return False


async def check_for_shift_farming():
    """Automatically end and report active shifts exceeding 10 hours."""
    async with shift_lock:
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
                await safe_send(
                    warning_channel,
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
                await safe_send(
                    log_channel,
                    embed=log_embed,
                    allowed_mentions=discord.AllowedMentions.none()
                )
                warning_sent = True
            except (discord.Forbidden, discord.HTTPException):
                print(f"Error: Could not log possible shift farming for user {user_id}.")

        if warning_sent or (not warning_channel and not log_channel):
            shift_farming_alerted.add(user_id)
            save_shift_data()


async def run_quota_reset(
    guild: discord.Guild | None = None,
    force: bool = False
) -> bool:
    """Run the weekly quota reset logic. Returns True if reset was performed."""
    global last_quota_reset_date
    cst_zone = ZoneInfo("America/Chicago")
    now_cst = datetime.datetime.now(cst_zone)

    # Determine the most recent Saturday at midnight CST
    days_since_saturday = (now_cst.weekday() - 5) % 7
    last_saturday = now_cst - datetime.timedelta(days=days_since_saturday)
    last_saturday_midnight = last_saturday.replace(hour=0, minute=0, second=0, microsecond=0)
    reset_date = last_saturday_midnight.date().isoformat()

    # If not forced, only reset if we're past the reset time and haven't reset yet
    if not force:
        if now_cst < last_saturday_midnight:
            return False  # Not yet time for this week's reset
        if last_quota_reset_date == reset_date:
            return False  # Already reset for this week

    last_quota_reset_date = reset_date
    save_verification_data()

    target_guild = guild or bot.get_guild(GUILD_ID)
    if target_guild:
        channel = target_guild.get_channel(QUOTA_LOG_CHANNEL_ID)
        if channel:
            # Get all members who have quota-eligible roles
            eligible_members = []
            for member in target_guild.members:
                if not member.bot and any(role.id in QUOTA_ELIGIBLE_ROLE_IDS for role in member.roles):
                    eligible_members.append(member)

            failed_users = []
            passed_users = []
            for member in eligible_members:
                total_secs = shift_leaderboard_data.get(member.id, 0.0)
                if total_secs < QUOTA_REQUIRED_SECONDS:
                    failed_users.append(f"• {member.mention} (`{member.id}`) — `{format_duration(total_secs)}` / `02h 00m 00s`")
                else:
                    passed_users.append(member.id)

            # Award $20 merit points to users who met quota
            async with merit_lock:
                for uid in passed_users:
                    await add_merit_points(uid, 20, source="Quota Bonus", description="Weekly shift quota met (≥1h30m)")

            report_text = "**Weekly Quota Summary Report (Saturday Reset - CST)**\n\n"
            if failed_users:
                report_text += "The following staff members did not meet the 2-hour minimum shift quota this week:\n\n" + "\n".join(failed_users)
            else:
                report_text += "All active staff members successfully met their 2-hour quota this week! 🎉"

            if passed_users:
                report_text += "\n\n**Quota Bonus:** The following users earned **$20 Պ** for meeting quota:\n" + "\n".join(f"• <@{uid}>" for uid in passed_users)

            embed = discord.Embed(
                title="📅 End of Week Quota Report",
                description=report_text,
                color=discord.Color.red() if failed_users else discord.Color.green()
            )
            embed.set_footer(text="Made by zeemonyt")
            try:
                await safe_send(channel, embed=embed)
            except discord.Forbidden:
                print("Error: Could not send weekly reset report to quota log channel.")

    async with shift_lock:
        shift_leaderboard_data.clear()
        last_shifts.clear()
        save_shift_data()
    print(f"Weekly shift leaderboard successfully reset for {reset_date} (force={force}).")
    return True


# --- AUTOMATED WEEKLY RESET TASK (12 AM Saturday CST) ---
@tasks.loop(seconds=30)
async def weekly_quota_reset():
    await check_for_shift_farming()
    await run_quota_reset()


VERIFICATION_WORDS = [
    "amber", "apple", "brave", "candy", "cedar", "cloud", "coral", "daisy",
    "eagle", "ember", "frost", "grape", "green", "happy", "ivory", "lemon",
    "maple", "ocean", "olive", "pearl", "piano", "pixel", "pluto", "raven",
    "river", "robin", "sable", "solar", "stone", "sunny", "tiger", "violet"
]


def generate_verification_code() -> str:
    """Generates four short random words for a Roblox bio verification code."""
    return " ".join(random.sample(VERIFICATION_WORDS, 4))


def generate_admin_token() -> str:
    """Generates a secure 15-30 character alphanumeric token for admin authentication."""
    length = random.randint(15, 30)
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choice(chars) for _ in range(length))


def load_admin_tokens() -> dict:
    """Loads admin tokens from JSON file."""
    try:
        with open(ADMIN_TOKENS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("tokens", {})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_admin_tokens(tokens: dict) -> bool:
    """Saves admin tokens to JSON file atomically."""
    data = {"tokens": tokens}
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(ADMIN_TOKENS_PATH),
            prefix=".admin-",
            suffix=".tmp",
            delete=False
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(data, temp_file, indent=2, sort_keys=True)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, ADMIN_TOKENS_PATH)
        return True
    except (OSError, TypeError, ValueError) as error:
        print(f"Error: Could not save admin tokens to {ADMIN_TOKENS_PATH}: {error}")
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


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
        async with verification_lock:
            pending_verifications[interaction.user.id] = request
            save_verification_data()

        verification_view = VerificationCodeView()
        try:
            await safe_send_dm(
                interaction.user,
                embed=verification_embed(request),
                view=verification_view,
                allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't DM you. Here are your verification instructions; "
                "this private button panel expires after 15 minutes. Enable server DMs "
                "and click **Verify now** again if it expires.",
                embed=verification_embed(request),
                view=VerificationCodeView(),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none()
            )
            return
        except discord.HTTPException as error:
            print(
                f"Error: Could not send verification DM to user "
                f"{interaction.user.id}: HTTP {error.status}."
            )
            await interaction.followup.send(
                "I couldn't send your verification instructions. Please try again or "
                "enable server DMs and click **Verify now** again.",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            "I sent your Roblox verification code and persistent Verify buttons in DMs. "
            "The buttons remain available while you update your bio.",
            ephemeral=True
        )


class VerificationStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item
    ) -> None:
        print(f"Error in verification start button: {error}")
        message = "The verification button encountered an error. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=bool(interaction.guild)
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=bool(interaction.guild)
                )
        except discord.HTTPException as response_error:
            print(f"Could not report verification button error: {response_error}")

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
    def __init__(self, user_id: int | None = None):
        super().__init__(timeout=None)
        self.user_id = user_id

        verify_custom_id = (
            f"verification:verify:{user_id}"
            if user_id is not None
            else "verification:verify"
        )
        verify_button = discord.ui.Button(
            label="Verify",
            style=discord.ButtonStyle.success,
            custom_id=verify_custom_id
        )
        verify_button.callback = self.verify_code
        self.add_item(verify_button)

        regenerate_custom_id = (
            f"verification:regenerate:{user_id}"
            if user_id is not None
            else "verification:regenerate"
        )
        regenerate_button = discord.ui.Button(
            label="Words tagged role",
            style=discord.ButtonStyle.secondary,
            custom_id=regenerate_custom_id
        )
        regenerate_button.callback = self.regenerate_code
        self.add_item(regenerate_button)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item
    ) -> None:
        print(f"Error in verification code button: {error}")
        message = "The verification button encountered an error. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=bool(interaction.guild)
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=bool(interaction.guild)
                )
        except discord.HTTPException as response_error:
            print(f"Could not report verification button error: {response_error}")

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if self.user_id is None or interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "This verification request belongs to another user.",
            ephemeral=bool(interaction.guild)
        )
        return False

    async def regenerate_code(self, interaction: discord.Interaction):
        if not await self._check_owner(interaction):
            return

        user_id = self.user_id if self.user_id is not None else interaction.user.id
        await interaction.response.defer()

        async with verification_lock:
            request = pending_verifications.get(user_id)
            if not request:
                await interaction.followup.send(
                    "This verification request has expired. Click **Verify now** to start again.",
                    ephemeral=bool(interaction.guild)
                )
                return

            request["code"] = generate_verification_code()
            request["created_at"] = int(time.time())
            save_verification_data()
        await interaction.edit_original_response(
            embed=verification_embed(request),
            view=VerificationCodeView(self.user_id)
        )

    async def verify_code(self, interaction: discord.Interaction):
        if not await self._check_owner(interaction):
            return

        await interaction.response.defer()
        user_id = self.user_id if self.user_id is not None else interaction.user.id

        async with verification_lock:
            request = pending_verifications.get(user_id)
            if not request:
                await interaction.followup.send(
                    "This verification request has expired. Click **Verify now** to start again.",
                    ephemeral=bool(interaction.guild)
                )
                return

        try:
            roblox_profile = await fetch_roblox_profile(request["roblox_user_id"])
        except Exception as error:
            print(
                f"Error: Roblox bio lookup failed for user "
                f"{request['roblox_username']}: {error}"
            )
            await interaction.followup.send(
                "I couldn't check your Roblox bio right now. Please try again in a moment.",
                ephemeral=bool(interaction.guild)
            )
            return

        if request["code"].casefold() not in roblox_profile["description"].casefold():
            await interaction.followup.send(
                "The verification code was not found in your Roblox bio. "
                "Add it exactly, save your bio, and try again.",
                ephemeral=bool(interaction.guild)
            )
            return

        guild = interaction.guild or bot.get_guild(request["guild_id"])
        if not guild:
            await interaction.followup.send(
                "I couldn't find the staff server. Please try again later.",
                ephemeral=bool(interaction.guild)
            )
            return

        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None

        role = guild.get_role(VERIFIED_ROLE_ID)
        if not member or not role:
            await interaction.followup.send(
                "I couldn't find your server member record or the verification role. "
                "Please contact an administrator.",
                ephemeral=bool(interaction.guild)
            )
            return

        try:
            await safe_add_roles(member, role, reason="Roblox bio verification completed")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I couldn't assign the verification role. Please contact an administrator.",
                ephemeral=bool(interaction.guild)
            )
            return

        nickname_updated = True
        try:
            await safe_edit_member(
                member,
                nick=request["roblox_username"],
                reason="Set nickname to verified Roblox username"
            )
        except (discord.Forbidden, discord.HTTPException):
            nickname_updated = False
            print(
                f"Warning: Could not set Discord nickname for verified user "
                f"{user_id}."
            )

        async with verification_lock:
            pending_verifications.pop(user_id, None)
            save_verification_data()
        result_message = (
            f"Verification complete. You received the <@&{VERIFIED_ROLE_ID}> role "
            f"and your nickname was set to **{request['roblox_username']}**."
        )
        if not nickname_updated:
            result_message += (
                "\n\nI couldn't change your nickname because of Discord permissions."
            )
        await interaction.followup.send(
            result_message,
            ephemeral=bool(interaction.guild),
            allowed_mentions=discord.AllowedMentions.none()
        )


def register_verification_views() -> None:
    """Registers persistent button handlers after the bot connects."""
    global verification_views_registered
    if verification_views_registered:
        return

    bot.add_view(VerificationStartView())
    bot.add_view(VerificationCodeView())
    for user_id in pending_verifications:
        bot.add_view(VerificationCodeView(user_id))
    verification_views_registered = True


# --- MARKET / SHOP SYSTEM ---

# Role IDs for automatic role assignment on purchase
MARKET_ROLE_IDS = {
    "undercover_privilege": 1550683794841804800,      # 1 week
    "high_speed_pass": 1550683906888695889,           # 1 week
    "honorary_command_staff": 1550685863212617758,    # permanent
    "vip_chat_color": 1550684278378201259,            # permanent
}

# Duration in seconds (0 = permanent)
MARKET_ROLE_DURATIONS = {
    "undercover_privilege": 7 * 24 * 60 * 60,   # 1 week
    "high_speed_pass": 7 * 24 * 60 * 60,        # 1 week
    "honorary_command_staff": 0,                # permanent
    "vip_chat_color": 0,                        # permanent
}

MARKET_ITEMS = [
    {
        "id": "undercover_privilege",
        "name": "Undercover Privilege",
        "price": 150,
        "description": "Since LRs are strictly locked to the basic livery, buying this pass gives an LR or MR temporary permission (for 1 week) to use the Undercover livery.",
        "role_id": 1550683794841804800,
        "duration": "1 week"
    },
    {
        "id": "high_speed_pass",
        "name": "High Speed Vehicle Pass",
        "price": 100,
        "description": "This grants an LR or MR permission (for one week) to pull out the high speed vehicles (like sports cars) for their patrol shift even if those cars are normally locked behind higher ranks or specific traffic subdivisions.",
        "role_id": 1550683906888695889,
        "duration": "1 week"
    },
    {
        "id": "fast_pass",
        "name": "Fast Pass",
        "price": 200,
        "description": "This is the perfect bridge for people outside of LASD who have accumulated credits. Buying this allows a civilian to instantly skip the training waitlist, jump straight into the next scheduled academy, and fast-track their way to becoming an official Deputy.",
        "role_id": None,
        "duration": "N/A"
    },
    {
        "id": "honorary_command_staff",
        "name": "Honorary Command Staff Rank (Permanent)",
        "price": 500,
        "description": "A Discord role called the Sheriff's Advisory Council. It sits high on the member list, giving veteran players or highly active outsiders massive status without giving them actual HR powers. (You may be consulted though!)",
        "role_id": 1550685863212617758,
        "duration": "Permanent"
    },
    {
        "id": "vip_chat_color",
        "name": "VIP Chat Color (Yellow Role)",
        "price": 50,
        "description": "Gives the user a custom, distinct yellow role on the Discord active users list. It's clean, and looks great on the sidebar.",
        "role_id": 1550684278378201259,
        "duration": "Permanent"
    }
]

market_message_id = None
market_views_registered = False


def market_embed() -> discord.Embed:
    """Builds the market/shop embed."""
    embed = discord.Embed(
        title="🛒 LASD Merit Points Market",
        description=(
            "Buy items here using your Merit Points (Պ).\n\n"
            "**How to Buy:** Click a button below to purchase an item. "
            "You'll be prompted to confirm your purchase."
        ),
        color=discord.Color.gold()
    )

    for item in MARKET_ITEMS:
        duration = item.get('duration', 'N/A')
        embed.add_field(
            name=f"{item['name']} — **${item['price']} Պ** {'(' + duration + ')' if duration != 'N/A' else ''}",
            value=item['description'],
            inline=False
        )

    embed.set_footer(text="Made by zeemonyt • LASD Utilities")
    return embed


class MarketBuyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        for item in MARKET_ITEMS:
            duration = item.get('duration', 'N/A')
            label = f"Buy {item['name']} — ${item['price']} Պ"
            if duration != "N/A":
                label += f" ({duration})"
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"market:buy:{item['id']}"
            )
            button.callback = self.create_buy_callback(item)
            self.add_item(button)

    def create_buy_callback(self, item):
        async def callback(interaction: discord.Interaction):
            # Check user's merit points
            user_points = get_merit_points(interaction.user.id)

            if user_points < item['price']:
                await interaction.response.send_message(
                    f"❌ You don't have enough Merit Points! You have **{user_points:,} Պ**, but **{item['name']}** costs **{item['price']:,} Պ**.",
                    ephemeral=True
                )
                return

            # Create confirmation view
            confirm_view = MarketConfirmView(item)
            duration = item.get('duration', 'N/A')
            embed = discord.Embed(
                title=f"Confirm Purchase: {item['name']}",
                description=(
                    f"**Item:** {item['name']}\n"
                    f"**Price:** {item['price']:,} Պ\n"
                    f"**Duration:** {duration}\n"
                    f"**Your Balance:** {user_points:,} Պ\n"
                    f"**Remaining After Purchase:** {user_points - item['price']:,} Պ\n\n"
                    f"{item['description']}\n\n"
                    "Are you sure you want to purchase this item?"
                ),
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed, view=confirm_view, ephemeral=True)
        return callback


class MarketConfirmView(discord.ui.View):
    def __init__(self, item: dict):
        super().__init__(timeout=60)
        self.item = item

        confirm_button = discord.ui.Button(
            label="✅ Confirm Purchase",
            style=discord.ButtonStyle.success,
            custom_id=f"market:confirm:{item['id']}"
        )
        confirm_button.callback = self.confirm_purchase
        self.add_item(confirm_button)

        cancel_button = discord.ui.Button(
            label="❌ Cancel",
            style=discord.ButtonStyle.danger,
            custom_id=f"market:cancel:{item['id']}"
        )
        cancel_button.callback = self.cancel_purchase
        self.add_item(cancel_button)

    async def confirm_purchase(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        user_points = get_merit_points(user_id)

        if user_points < self.item['price']:
            await interaction.response.edit_message(
                content=f"❌ You don't have enough Merit Points! You have **{user_points:,} Պ**, but this costs **{self.item['price']:,} Պ**.",
                embed=None,
                view=None
            )
            return

        # Deduct points
        new_balance = await remove_merit_points(user_id, self.item['price'], source="Market Purchase", description=f"Purchased: {self.item['name']}")

        # Assign role if applicable
        role_assigned = False
        role_info = ""
        guild = interaction.guild
        if guild and self.item.get('role_id'):
            role = guild.get_role(self.item['role_id'])
            member = guild.get_member(user_id)
            if role and member:
                try:
                    await safe_add_roles(member, role, reason=f"Market purchase: {self.item['name']}")
                    role_assigned = True

                    duration = self.item.get('duration', 'N/A')
                    if duration and duration != "N/A" and duration != "Permanent":
                        role_info = f"\n**Role:** {role.mention} (expires in {duration})"
                        # Schedule role removal
                        duration_seconds = MARKET_ROLE_DURATIONS.get(self.item['id'], 0)
                        if duration_seconds > 0:
                            asyncio.create_task(remove_role_after_duration(guild, member, role, duration_seconds))
                    else:
                        role_info = f"\n**Role:** {role.mention} (permanent)"
                except discord.Forbidden:
                    role_info = "\n⚠️ Could not assign role (missing permissions)"
                except discord.HTTPException as e:
                    role_info = f"\n⚠️ Could not assign role: {e}"

        # Send confirmation
        embed = discord.Embed(
            title="✅ Purchase Successful!",
            description=(
                f"You bought **{self.item['name']}** for **{self.item['price']:,} Պ**!\n\n"
                f"**New Balance:** {new_balance:,} Պ{role_info}\n\n"
                f"**Item Details:**\n{self.item['description']}"
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text="Made by zeemonyt • LASD Utilities")
        await interaction.response.edit_message(embed=embed, view=None)

        # Log to market log channel
        if guild:
            log_channel = guild.get_channel(MARKET_LOG_CHANNEL_ID)
            if log_channel:
                log_embed = discord.Embed(
                    title="🛒 Market Purchase",
                    description=(
                        f"**Buyer:** {interaction.user.mention} (`{user_id}`)\n"
                        f"**Item:** {self.item['name']}\n"
                        f"**Price:** {self.item['price']:,}Պ\n"
                        f"**New Balance:** {new_balance:,}Պ\n"
                        f"**Role Assigned:** {'✅ Yes' if role_assigned else '❌ No'}"
                    ),
                    color=discord.Color.gold()
                )
                log_embed.set_footer(text="Made by zeemonyt")
                try:
                    await log_channel.send(embed=log_embed)
                except discord.Forbidden:
                    pass

            # Also log Fast Pass to dedicated channel
            if self.item['id'] == 'fast_pass':
                fast_pass_log_channel = guild.get_channel(FAST_PASS_LOG_CHANNEL_ID)
                if fast_pass_log_channel:
                    fast_pass_embed = discord.Embed(
                        title="⚡ Fast Pass Purchase",
                        description=(
                            f"**Buyer:** {interaction.user.mention} (`{user_id}`)\n"
                            f"**Price:** {self.item['price']:,}Պ\n"
                            f"**New Balance:** {new_balance:,}Պ\n\n"
                            f"**Action Required:** Manual fulfillment needed for Fast Pass (skip training waitlist)"
                        ),
                        color=discord.Color.blue()
                    )
                    fast_pass_embed.set_footer(text="Made by zeemonyt")
                    try:
                        await fast_pass_log_channel.send(embed=fast_pass_embed)
                    except discord.Forbidden:
                        pass

    async def cancel_purchase(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="❌ Purchase cancelled.",
            embed=None,
            view=None
        )


async def remove_role_after_duration(guild: discord.Guild, member: discord.Member, role: discord.Role, duration_seconds: int):
    """Removes a role from a member after the specified duration."""
    try:
        await asyncio.sleep(duration_seconds)
        # Re-fetch member in case they left/rejoined
        try:
            member = guild.get_member(member.id) or await safe_fetch_member(guild, member.id)
            if member and role in member.roles:
                await safe_remove_roles(member, role, reason=f"Market purchase expired: {role.name}")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    except asyncio.CancelledError:
        pass


def register_market_views() -> None:
    """Registers persistent market button handlers after the bot connects."""
    global market_views_registered
    if market_views_registered:
        return

    bot.add_view(MarketBuyView())
    market_views_registered = True


async def ensure_market_message() -> None:
    """Ensures the market message is sent only once."""
    global market_message_id

    channel = bot.get_channel(MARKET_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(MARKET_CHANNEL_ID)
        except discord.NotFound:
            print(f"Error: Market channel {MARKET_CHANNEL_ID} was not found.")
            return
        except discord.Forbidden:
            print(f"Error: Discord denied access to market channel {MARKET_CHANNEL_ID}.")
            return
        except discord.HTTPException as error:
            print(f"Error: Could not access market channel {MARKET_CHANNEL_ID} (HTTP {error.status}).")
            return

    # Check if message already exists (we could persist the ID, but for now just send new)
    try:
        message = await safe_send(
            channel,
            embed=market_embed(),
            view=MarketBuyView(),
            allowed_mentions=discord.AllowedMentions.none()
        )
        market_message_id = message.id
        print(f"Created market message {message.id} in channel {MARKET_CHANNEL_ID}.")
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Error: Could not send the market message: {error}")
        return


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
        message = await safe_send(
            channel,
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
async def on_connect():
    print(f"[BOT LOG] Connected to Discord Gateway successfully!")

@bot.event
async def on_ready():
    load_shift_data()
    load_merit_points()
    load_merit_transactions()
    print(f"[BOT LOG] Logged in as {bot.user} (ID: {bot.user.id}), ready={bot.is_ready()}, guilds={[g.name for g in bot.guilds]}")

    register_verification_views()
    register_market_views()
    print(
        f"[BOT LOG] Registered persistent verification controls "
        f"for {len(pending_verifications)} pending request(s)."
    )

    if not weekly_quota_reset.is_running():
        weekly_quota_reset.start()

    # Check for missed quota reset on startup
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await run_quota_reset(guild=guild)

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"[BOT LOG] Synced {len(synced)} command(s) to guild {GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"[BOT LOG] Synced {len(synced)} global command(s).")
    except Exception as e:
        print(f"[BOT LOG] Error syncing slash commands: {e}")

    await ensure_verification_message()
    await ensure_market_message()


@bot.event
async def on_member_join(member):
    role = member.guild.get_role(1545126033433559140)
    if role:
        try:
            await safe_add_roles(member, role)
            print(f"Assigned @Unverified to {member.name}, has just joined.")
        except discord.Forbidden:
            print(
                "Failed to assign role: Check that the bot has 'Manage Roles' "
                "and its highest role is ABOVE the @Unverified role in Server Settings."
            )
        except discord.HTTPException as e:
            print(f"Failed to assign role due to an HTTP error: {e}")


# --- AUTOMOD SYSTEM ---
AUTOMOD_LOG_CHANNEL_ID = 1545600605853065257

HR_SHR_ROLE_IDS = {
    1538076681640747130, # Captain
    1538076681640747131, # Chief Deputy
    1539413609388712006, # Chief
    1538076681640747132, # Undersheriff
    1538076681640747133, # Sheriff
    1538076681640747135, # High Ranking Team
    1538076681640747136, # Overseer
    1539431991080910899  # GLARP Ownership
}

class AutoModRule:
    def __init__(self, section_name: str, message_template: str, patterns: list):
        self.section_name = section_name
        self.message_template = message_template
        self.compiled_patterns = []
        for pat in patterns:
            pat = pat.strip()
            if not pat:
                continue
            # Escape special regex chars, but treat * as wildcard
            if '*' in pat:
                # Split by * and escape each part, then join with .*
                parts = pat.split('*')
                escaped_parts = [re.escape(p) for p in parts]
                escaped = '.*'.join(escaped_parts)
                # Match at word boundaries or string boundaries, allowing punctuation
                regex_str = rf"(?:^|(?<=[\s\W])){escaped}(?:(?=[\s\W])|$)"
            else:
                escaped = re.escape(pat)
                # Match whole words, allowing punctuation around them
                regex_str = rf"(?:^|(?<=[\s\W])){escaped}(?:(?=[\s\W])|$)"
            try:
                self.compiled_patterns.append((pat, re.compile(regex_str, re.IGNORECASE)))
            except Exception:
                pass

def load_blocked_words():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocked_words.json")
    rules = []
    if not os.path.exists(path):
        return rules

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    current_section = "General"
    current_message = ""
    current_patterns = []

    for line in lines:
        line_stripped = line.strip()
        if line_stripped.startswith("##--") or line_stripped == "--:-":
            if current_patterns:
                rules.append(AutoModRule(current_section, current_message, current_patterns))
                current_patterns = []
            current_message = ""
            current_section = "General"
            continue
        if line_stripped.startswith("##-#"):
            current_message = line_stripped[4:].strip()
            continue
        if line_stripped.startswith("##"):
            if "Advertising" in line_stripped:
                current_section = "Advertising, Links & Exploits"
            elif "Severe" in line_stripped or "Threats" in line_stripped:
                current_section = "Severe Violations (Threats, Harm & Doxxing)"
            elif "Toxicity" in line_stripped:
                current_section = "Toxicity, Flaming & Disrespect"
            elif "Roleplay" in line_stripped or "meta" in line_stripped.lower():
                current_section = "Roleplay Rulebreaks, Slang & Meta-Complaints"
            elif "Profanity" in line_stripped or "Bypass" in line_stripped:
                current_section = "Profanity & Filter Bypasses"
            elif "Swearing" in line_stripped:
                current_section = "Swearing and Profanity, or otherwise Rule Breaking"
            continue

        if line_stripped and not line_stripped.startswith("#"):
            current_patterns.append(line_stripped)

    if current_patterns:
        rules.append(AutoModRule(current_section, current_message, current_patterns))
    return rules

AUTOMOD_RULES = load_blocked_words()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return

    # Check HR + SHR exemption
    if isinstance(message.author, discord.Member):
        user_roles = {r.id for r in message.author.roles}
        if user_roles.intersection(HR_SHR_ROLE_IDS) or message.author.id in ALLOWED_USER_IDS:
            await bot.process_commands(message)
            return

    content = message.content
    matched_rule = None
    matched_word = None

    for rule in AUTOMOD_RULES:
        for pat, regex in rule.compiled_patterns:
            if regex.search(content):
                matched_rule = rule
                matched_word = pat
                break
        if matched_rule:
            break

    if matched_rule:
        user_id = message.author.id
        now = int(time.time())

        try:
            await safe_delete(message)
        except discord.HTTPException:
            pass

        if user_id not in user_strikes:
            user_strikes[user_id] = []

        case_id = str(random.randint(100000000000000000, 999999999999999999))
        strike_record = {
            "case_id": case_id,
            "moderator_id": bot.user.id,
            "reason": f"AutoMod: Triggered rule [{matched_rule.section_name}] (Matched: `{matched_word}`)",
            "timestamp": now
        }
        user_strikes[user_id].append(strike_record)
        infraction_count = len(user_strikes[user_id])

        if infraction_count == 1:
            action_str = "Muted."
        elif infraction_count == 2:
            action_str = "Muted & Warned."
        else:
            action_str = "Kicked."

        dm_msg = matched_rule.message_template
        if not dm_msg:
            dm_msg = f"Your message violated our server rules in section **{matched_rule.section_name}**."

        full_dm = f"{dm_msg} **{action_str}**"
        try:
            await safe_send_dm(message.author, full_dm)
        except discord.HTTPException:
            pass

        if infraction_count >= 3:
            try:
                if isinstance(message.author, discord.Member):
                    await message.author.kick(reason=f"AutoMod threshold reached: {matched_rule.section_name}")
            except discord.HTTPException:
                pass

        log_channel = message.guild.get_channel(AUTOMOD_LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(
                title="🛡️ | AutoMod Infraction Log",
                description=(
                    f"📋 **Automated Moderation Action**\n\n"
                    f"**Violation Details**\n"
                    f"> **Case ID:** `{case_id}`\n"
                    f"> **User:** {message.author.mention} | `{user_id}`\n"
                    f"> **Channel:** {message.channel.mention}\n"
                    f"> **Section:** {matched_rule.section_name}\n"
                    f"> **Matched Pattern:** `{matched_word}`\n"
                    f"> **Action Taken:** `{action_str}`\n\n"
                    f"**Message Content:**\n"
                    f"```text\n{content[:1500]}\n```\n"
                    f"• 🕒 **Time:** <t:{now}:f>\n"
                    f"• 📜 Total infractions: `{infraction_count}`"
                ),
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            try:
                await safe_send(log_channel, embed=embed)
            except discord.HTTPException:
                pass

        return

    await bot.process_commands(message)


# --- SHIFT SLASH COMMANDS ---

@bot.tree.command(name="shift-start", description="Start your staff shift and update your server nickname.")
async def shift_start(interaction: discord.Interaction):
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use shift commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
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

    async with shift_lock:
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

        raw_nick = interaction.user.nick if interaction.user.nick else interaction.user.name
        original_nick = raw_nick[len(PREFIX):] if raw_nick.startswith(PREFIX) else raw_nick

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
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use shift commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    user_id = interaction.user.id

    async with shift_lock:
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
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use shift commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    user_id = interaction.user.id

    async with shift_lock:
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
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use shift commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
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


@bot.tree.command(name="shift-active", description="View currently active staff on shift.")
async def shift_active(interaction: discord.Interaction):
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use shift commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if not active_shifts:
        embed = discord.Embed(
            title="🟢 Active Shifts",
            description="No staff members currently on shift.",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)
        return

    guild = interaction.guild
    active_text = ""
    for user_id, shift_data in active_shifts.items():
        member = guild.get_member(user_id) if guild else None
        name = member.display_name if member else f"User {user_id}"
        status = "☕ On Break" if shift_data["on_break"] else "🟢 On Shift"
        elapsed = time.time() - shift_data["start_time"] - shift_data["break_time"]
        if shift_data["on_break"]:
            elapsed -= (time.time() - shift_data["break_start"])
        elapsed = max(0, elapsed)
        active_text += f"• **{name}** — {status} (`{format_duration(elapsed)}`)\n"

    embed = discord.Embed(
        title="🟢 Active Shifts",
        description=active_text,
        color=discord.Color.green()
    )
    embed.set_footer(text="Made by zeemonyt")
    await interaction.response.send_message(embed=embed)


class ShiftStatusView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="Start Shift", style=discord.ButtonStyle.success, custom_id="shift:start", emoji="🟢")
    async def start_shift(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your shift panel!", ephemeral=True)
            return
        await self.handle_shift_start(interaction)

    @discord.ui.button(label="Take Break", style=discord.ButtonStyle.secondary, custom_id="shift:break", emoji="☕")
    async def toggle_break(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your shift panel!", ephemeral=True)
            return
        await self.handle_break(interaction)

    @discord.ui.button(label="End Shift", style=discord.ButtonStyle.danger, custom_id="shift:end", emoji="🔴")
    async def end_shift(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your shift panel!", ephemeral=True)
            return
        await self.handle_end_shift(interaction)

    async def handle_shift_start(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        async with shift_lock:
            if user_id in active_shifts:
                await interaction.response.send_message("You already have an active shift!", ephemeral=True)
                return

            raw_nick = interaction.user.nick if interaction.user.nick else interaction.user.name
            original_nick = raw_nick[len(PREFIX):] if raw_nick.startswith(PREFIX) else raw_nick

            active_shifts[user_id] = {
                "start_time": time.time(),
                "original_nick": original_nick,
                "break_time": 0.0,
                "on_break": False,
                "break_start": 0.0
            }
            save_shift_data()
        await apply_on_shift_nick(interaction.user, original_nick)

        embed = discord.Embed(
            title="🟢 Shift Started",
            description=f"Shift started at <t:{int(time.time())}:F>",
            color=discord.Color.green()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.edit_message(embed=embed, view=None)

    async def handle_break(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        async with shift_lock:
            if user_id not in active_shifts:
                await interaction.response.send_message("You don't have an active shift!", ephemeral=True)
                return

            shift_data = active_shifts[user_id]
            if not shift_data["on_break"]:
                shift_data["on_break"] = True
                shift_data["break_start"] = time.time()
                status = "Break started"
            else:
                break_duration = time.time() - shift_data["break_start"]
                shift_data["break_time"] += break_duration
                shift_data["on_break"] = False
                shift_data["break_start"] = 0.0
                status = f"Break ended ({format_duration(break_duration)})"

            save_shift_data()
        embed = discord.Embed(
            title=f"☕ {status}",
            description=f"Break status updated at <t:{int(time.time())}:F>",
            color=discord.Color.gold() if not shift_data["on_break"] else discord.Color.orange()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.edit_message(embed=embed, view=None)

    async def handle_end_shift(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        async with shift_lock:
            if user_id not in active_shifts:
                await interaction.response.send_message("You don't have an active shift!", ephemeral=True)
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

        await restore_nick(interaction.user, shift_data["original_nick"])

        embed = discord.Embed(
            title="🔴 Shift Ended",
            description=f"**Active Duty:** `{format_duration(active_duty_time)}`\n**Total Cumulative:** `{format_duration(total_cumulative)}`",
            color=discord.Color.blue()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.edit_message(embed=embed, view=None)


@bot.tree.command(name="shift", description="View and manage your shift status.")
async def shift_command(interaction: discord.Interaction):
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use shift commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    user_id = interaction.user.id
    shift_data = active_shifts.get(user_id)

    if shift_data:
        elapsed = time.time() - shift_data["start_time"] - shift_data["break_time"]
        if shift_data["on_break"]:
            elapsed -= (time.time() - shift_data["break_start"])
        elapsed = max(0, elapsed)

        if shift_data["on_break"]:
            status = "☕ On Break"
            color = discord.Color.orange()
        else:
            status = "🟢 On Shift"
            color = discord.Color.green()

        embed = discord.Embed(
            title="Your Shift Status",
            description=f"**Status:** {status}\n**Duration:** `{format_duration(elapsed)}`\n**Started:** <t:{int(shift_data['start_time'])}:F>",
            color=color
        )
    else:
        embed = discord.Embed(
            title="Your Shift Status",
            description="**Status:** 🔴 Off Shift\n\nClick **Start Shift** to begin your shift.",
            color=discord.Color.red()
        )

    embed.set_footer(text="Made by zeemonyt")
    await interaction.response.send_message(embed=embed, view=ShiftStatusView(user_id), ephemeral=True)


@bot.tree.command(name="shift-choose", description="All-in-one shift management command.")
@app_commands.describe(
    option="Select an action",
    user="Optional: Target user (for admin actions)"
)
@app_commands.choices(option=[
    app_commands.Choice(name="Start Shift", value="start"),
    app_commands.Choice(name="End Shift", value="end"),
    app_commands.Choice(name="Take Break", value="break"),
    app_commands.Choice(name="Active Shifts", value="active"),
    app_commands.Choice(name="Leaderboard", value="leaderboard"),
    app_commands.Choice(name="See Status", value="see"),
    app_commands.Choice(name="My Status", value="status")
])
async def shift_choose(
    interaction: discord.Interaction,
    option: app_commands.Choice[str],
    user: discord.Member = None
):
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use shift commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    action = option.value
    target = user or interaction.user
    target_id = target.id

    if action == "start":
        async with shift_lock:
            if target_id in active_shifts:
                await interaction.response.send_message(f"{target.mention} already has an active shift!", ephemeral=True)
                return
            raw_nick = target.nick if target.nick else target.name
            original_nick = raw_nick[len(PREFIX):] if raw_nick.startswith(PREFIX) else raw_nick
            active_shifts[target_id] = {
                "start_time": time.time(),
                "original_nick": original_nick,
                "break_time": 0.0,
                "on_break": False,
                "break_start": 0.0
            }
            save_shift_data()
        await apply_on_shift_nick(target, original_nick)
        await interaction.response.send_message(f"🟢 Started shift for {target.mention}", ephemeral=(target != interaction.user))

    elif action == "end":
        async with shift_lock:
            if target_id not in active_shifts:
                await interaction.response.send_message(f"{target.mention} doesn't have an active shift!", ephemeral=True)
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
        await restore_nick(target, shift_data["original_nick"])
        await interaction.response.send_message(f"🔴 Ended shift for {target.mention} — `{format_duration(active_duty_time)}` logged", ephemeral=(target != interaction.user))

    elif action == "break":
        async with shift_lock:
            if target_id not in active_shifts:
                await interaction.response.send_message(f"{target.mention} doesn't have an active shift!", ephemeral=True)
                return
            shift_data = active_shifts[target_id]
            if not shift_data["on_break"]:
                shift_data["on_break"] = True
                shift_data["break_start"] = time.time()
                status = "Break started"
            else:
                break_duration = time.time() - shift_data["break_start"]
                shift_data["break_time"] += break_duration
                shift_data["on_break"] = False
                shift_data["break_start"] = 0.0
                status = f"Break ended ({format_duration(break_duration)})"
            save_shift_data()
        await interaction.response.send_message(f"☕ {status} for {target.mention}", ephemeral=(target != interaction.user))

    elif action == "active":
        if not active_shifts:
            await interaction.response.send_message("No staff members currently on shift.", ephemeral=True)
            return
        guild = interaction.guild
        active_text = ""
        for uid, sdata in active_shifts.items():
            member = guild.get_member(uid) if guild else None
            name = member.display_name if member else f"User {uid}"
            st = "☕ On Break" if sdata["on_break"] else "🟢 On Shift"
            elapsed = time.time() - sdata["start_time"] - sdata["break_time"]
            if sdata["on_break"]:
                elapsed -= (time.time() - sdata["break_start"])
            elapsed = max(0, elapsed)
            active_text += f"• **{name}** — {st} (`{format_duration(elapsed)}`)\n"
        embed = discord.Embed(title="🟢 Active Shifts", description=active_text, color=discord.Color.green())
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    elif action == "leaderboard":
        if not shift_leaderboard_data:
            await interaction.response.send_message("No shift data logged yet.", ephemeral=True)
            return
        sorted_staff = sorted(shift_leaderboard_data.items(), key=lambda x: x[1], reverse=True)
        lb_text = ""
        for i, (uid, secs) in enumerate(sorted_staff[:10], 1):
            icon = "✔" if secs >= QUOTA_REQUIRED_SECONDS else "❌"
            lb_text += f"**{i}.** <@{uid}> — `{format_duration(secs)}` {icon}\n"
        embed = discord.Embed(title="Staff Shift Leaderboard", description=lb_text, color=discord.Color.gold())
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    elif action == "see":
        shift_data = active_shifts.get(target_id)
        if shift_data:
            elapsed = time.time() - shift_data["start_time"] - shift_data["break_time"]
            if shift_data["on_break"]:
                elapsed -= (time.time() - shift_data["break_start"])
            elapsed = max(0, elapsed)
            status = "☕ On Break" if shift_data["on_break"] else "🟢 On Shift"
            embed = discord.Embed(
                title=f"{target.display_name}'s Shift Status",
                description=f"**Status:** {status}\n**Duration:** `{format_duration(elapsed)}`\n**Started:** <t:{int(shift_data['start_time'])}:F>",
                color=discord.Color.orange() if shift_data["on_break"] else discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title=f"{target.display_name}'s Shift Status",
                description="**Status:** 🔴 Off Shift",
                color=discord.Color.red()
            )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    elif action == "status":
        shift_data = active_shifts.get(target_id)
        if shift_data:
            elapsed = time.time() - shift_data["start_time"] - shift_data["break_time"]
            if shift_data["on_break"]:
                elapsed -= (time.time() - shift_data["break_start"])
            elapsed = max(0, elapsed)
            status = "☕ On Break" if shift_data["on_break"] else "🟢 On Shift"
            embed = discord.Embed(
                title=f"{target.display_name}'s Shift Status",
                description=f"**Status:** {status}\n**Duration:** `{format_duration(elapsed)}`\n**Started:** <t:{int(shift_data['start_time'])}:F>",
                color=discord.Color.orange() if shift_data["on_break"] else discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title=f"{target.display_name}'s Shift Status",
                description="**Status:** 🔴 Off Shift",
                color=discord.Color.red()
            )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="money-leaderboard", description="View the merit points leaderboard.")
async def money_leaderboard(interaction: discord.Interaction):
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use this command.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if not merit_points:
        embed = discord.Embed(
            title="Merit Points Leaderboard",
            description="No merit points data yet.",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)
        return

    sorted_staff = sorted(merit_points.items(), key=lambda item: item[1], reverse=True)

    leaderboard_text = ""
    for index, (uid, points) in enumerate(sorted_staff[:15], start=1):
        leaderboard_text += f"**{index}.** <@{uid}> — `{points:,}` Պ\n"

    # Find requester's rank
    requester_id = interaction.user.id
    requester_rank = None
    requester_points = merit_points.get(requester_id, 0)
    for index, (uid, _) in enumerate(sorted_staff, start=1):
        if uid == requester_id:
            requester_rank = index
            break

    embed = discord.Embed(
        title="Merit Points Leaderboard",
        description=leaderboard_text,
        color=discord.Color.gold()
    )

    if requester_rank:
        embed.add_field(
            name="Your Rank",
            value=f"**#{requester_rank}** — `{requester_points:,}` Պ",
            inline=False
        )
    else:
        embed.add_field(
            name="Your Rank",
            value=f"Unranked — `0` Պ",
            inline=False
        )

    embed.set_footer(text="Made by zeemonyt")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="money", description="View your merit points balance and transaction history.")
async def money_command(interaction: discord.Interaction):
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use this command.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    user_id = interaction.user.id
    balance = merit_points.get(user_id, 0)
    transactions = get_merit_transactions(user_id)

    # Group transactions by source
    source_totals = {}
    for tx in transactions:
        src = tx.get("source", "Unknown")
        if src not in source_totals:
            source_totals[src] = 0
        source_totals[src] += tx["amount"]

    # Build embed
    embed = discord.Embed(
        title="💰 Your Merit Points",
        description=f"**Current Balance:** `{balance:,} Պ`",
        color=discord.Color.gold()
    )

    if source_totals:
        source_text = ""
        for source, total in sorted(source_totals.items(), key=lambda x: x[1], reverse=True):
            source_text += f"• **{source}:** `{total:+,} Պ`\n"
        embed.add_field(name="Earnings by Source", value=source_text, inline=False)

    # Show recent transactions (last 10)
    if transactions:
        recent_tx = transactions[-10:]
        tx_text = ""
        for tx in reversed(recent_tx):
            amount = tx["amount"]
            source = tx.get("source", "Unknown")
            desc = tx.get("description", "")
            timestamp = tx.get("timestamp", 0)
            time_str = f"<t:{timestamp}:R>"
            sign = "+" if amount > 0 else ""
            tx_text += f"`{sign}{amount:,} Պ` — **{source}** ({desc}) {time_str}\n"
        embed.add_field(name="Recent Transactions", value=tx_text, inline=False)
    else:
        embed.add_field(name="Recent Transactions", value="No transactions yet.", inline=False)

    embed.set_footer(text="Made by zeemonyt")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="money-items", description="View all available market items (ephemeral).")
async def money_items_command(interaction: discord.Interaction):
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use this command.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    market_url = "https://discord.com/channels/1538076681565241384/1549878344391069716"
    await interaction.response.send_message(
        f"🛒 **Market Items:** View all available items here: {market_url}",
        ephemeral=True
    )


# Win chance percentages for each steal amount (1-50)
STEAL_WIN_CHANCES = {
    1: 20.00, 2: 19.65, 3: 19.31, 4: 18.96, 5: 18.61,
    6: 18.27, 7: 17.92, 8: 17.57, 9: 17.22, 10: 16.88,
    11: 16.53, 12: 16.18, 13: 15.84, 14: 15.49, 15: 15.14,
    16: 14.80, 17: 14.45, 18: 14.10, 19: 13.76, 20: 13.41,
    21: 13.06, 22: 12.71, 23: 12.37, 24: 12.02, 25: 11.67,
    26: 11.33, 27: 10.98, 28: 10.63, 29: 10.29, 30: 9.94,
    31: 9.59, 32: 9.24, 33: 8.90, 34: 8.55, 35: 8.20,
    36: 7.86, 37: 7.51, 38: 7.16, 39: 6.82, 40: 6.47,
    41: 6.12, 42: 5.78, 43: 5.43, 44: 5.08, 45: 4.73,
    46: 4.39, 47: 4.04, 48: 3.69, 49: 3.35, 50: 3.00
}


@bot.tree.command(name="money-steal", description="Attempt to steal merit points from another user (1-50 MP).")
@app_commands.describe(
    target="The user to steal from",
    amount="Amount to steal (1-50 MP)"
)
async def money_steal(
    interaction: discord.Interaction,
    target: discord.Member,
    amount: app_commands.Range[int, 1, 50]
):
    if not has_web_panel_role(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use this command.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if target.id == interaction.user.id:
        embed = discord.Embed(
            title="Invalid Target",
            description="You cannot steal from yourself!",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if target.bot:
        embed = discord.Embed(
            title="Invalid Target",
            description="You cannot steal from bots!",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Check 24-hour cooldown
    now = time.time()
    async with steal_lock:
        last_attempt = steal_cooldowns.get(user_id, 0)
        cooldown_seconds = 24 * 60 * 60  # 24 hours
        if now - last_attempt < cooldown_seconds:
            remaining = cooldown_seconds - (now - last_attempt)
            hours = int(remaining // 3600)
            minutes = int((remaining % 3600) // 60)
            embed = discord.Embed(
                title="⏳ Cooldown Active",
                description=f"You must wait **{hours}h {minutes}m** before attempting another steal.",
                color=discord.Color.orange()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Update cooldown
        steal_cooldowns[user_id] = now

    user_id = interaction.user.id
    target_id = target.id

    user_balance = merit_points.get(user_id, 0)
    target_balance = merit_points.get(target_id, 0)

    if target_balance < amount:
        embed = discord.Embed(
            title="Insufficient Funds",
            description=f"{target.mention} only has **{target_balance:,} Պ**. You cannot steal more than they have.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Get win chance
    win_chance = STEAL_WIN_CHANCES.get(amount, 3.00)
    roll = random.random() * 100

    async with merit_lock:
        user_balance = merit_points.get(user_id, 0)
        target_balance = merit_points.get(target_id, 0)

        if target_balance < amount:
            embed = discord.Embed(
                title="Insufficient Funds",
                description=f"{target.mention} only has **{target_balance:,} Պ**. You cannot steal more than they have.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if roll <= win_chance:
            # SUCCESS - steal the money
            merit_points[user_id] = user_balance + amount
            merit_points[target_id] = target_balance - amount
            save_merit_points()

            # Log transactions
            for uid, amt, src, desc in [
                (user_id, amount, "Steal", f"Stole from {target.display_name}"),
                (target_id, -amount, "Stolen", f"Stolen by {interaction.user.display_name}")
            ]:
                if uid not in merit_transactions:
                    merit_transactions[uid] = []
                merit_transactions[uid].append({
                    "amount": amt,
                    "source": src,
                    "description": desc,
                    "timestamp": int(time.time())
                })
            save_merit_transactions()

            embed = discord.Embed(
                title="💰 Steal Successful!",
                description=(
                    f"You successfully stole **{amount:,} Պ** from {target.mention}!\n\n"
                    f"**Your New Balance:** {merit_points[user_id]:,} Պ\n"
                    f"**{target.display_name}'s New Balance:** {merit_points[target_id]:,} Պ\n\n"
                    f"*Win chance was {win_chance:.2f}%*"
                ),
                color=discord.Color.green()
            )
        else:
            # FAIL - lose half the amount (or all if less)
            penalty = max(1, amount // 2)
            actual_penalty = min(penalty, user_balance)
            merit_points[user_id] = user_balance - actual_penalty
            save_merit_points()

            if actual_penalty > 0:
                if user_id not in merit_transactions:
                    merit_transactions[user_id] = []
                merit_transactions[user_id].append({
                    "amount": -actual_penalty,
                    "source": "Steal Failed",
                    "description": f"Failed to steal from {target.display_name}",
                    "timestamp": int(time.time())
                })
                save_merit_transactions()

            embed = discord.Embed(
                title="💸 Steal Failed!",
                description=(
                    f"You failed to steal from {target.mention} and lost **{actual_penalty:,} Պ** as penalty!\n\n"
                    f"**Your New Balance:** {merit_points[user_id]:,} Պ\n"
                    f"**{target.display_name}'s Balance:** {target_balance:,} Պ (unchanged)\n\n"
                    f"*Win chance was {win_chance:.2f}%*"
                ),
                color=discord.Color.red()
            )

    embed.set_footer(text="Made by zeemonyt")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="market", description="Send the market/shop message to the market channel.")
@app_commands.describe(
    channel="Optional: Channel to send the market message to (defaults to configured market channel)"
)
async def market_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None
):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Permission Denied",
            description="You do not have permission to use this command.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    target_channel = channel or bot.get_channel(MARKET_CHANNEL_ID)
    if not target_channel:
        try:
            target_channel = await bot.fetch_channel(MARKET_CHANNEL_ID)
        except Exception:
            embed = discord.Embed(
                title="Error",
                description="Could not find the market channel.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

    try:
        message = await safe_send(
            target_channel,
            embed=market_embed(),
            view=MarketBuyView(),
            allowed_mentions=discord.AllowedMentions.none()
        )
        global market_message_id
        market_message_id = message.id
        embed = discord.Embed(
            title="✅ Market Message Sent",
            description=f"Market message sent to {target_channel.mention}.",
            color=discord.Color.green()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        embed = discord.Embed(
            title="Error",
            description=f"Failed to send market message: {e}",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="money-admin", description="Admin override commands for managing merit points.")
@app_commands.describe(
    user="The target staff member",
    option="Select an administrative action",
    amount="Optional: Amount to add/remove (e.g. 5, 20, 1k, 10k, 1m, 1b, 1t)"
)
@app_commands.choices(option=[
    app_commands.Choice(name="Add Money", value="add"),
    app_commands.Choice(name="Remove Money", value="remove"),
    app_commands.Choice(name="Clear All Money", value="clear_all"),
    app_commands.Choice(name="Set Money", value="set")
])
async def money_admin(
    interaction: discord.Interaction,
    user: discord.Member,
    option: app_commands.Choice[str],
    amount: str = None
):
    if not is_admin(interaction):
        embed = discord.Embed(
            title="Permission Denied",
            description="You do not have permission to use this command.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    target_id = user.id
    action = option.value

    if action in ["add", "remove", "set"]:
        if not amount:
            embed = discord.Embed(
                title="Error",
                description="Please provide an amount (e.g. `5`, `20`, `1k`, `10k`, `1m`, `1b`, `1t`).",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        parsed_amount = parse_money_string(amount)
        if parsed_amount <= 0:
            embed = discord.Embed(
                title="Error",
                description="Invalid amount format. Use formats like `5`, `20`, `1k`, `10k`, `1m`, `1b`, `1t`.",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

    if action == "add":
        new_total = await add_merit_points(target_id, parsed_amount, source="Admin", description=f"Added by {interaction.user.display_name}")

        embed = discord.Embed(
            title="Admin Action: Money Added",
            description=(
                f"Added **{parsed_amount:,} Պ** to {user.mention}'s balance.\n"
                f"**New Balance:** {new_total:,} Պ"
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

    elif action == "remove":
        new_total = await remove_merit_points(target_id, parsed_amount, source="Admin", description=f"Removed by {interaction.user.display_name}")

        embed = discord.Embed(
            title="Admin Action: Money Removed",
            description=(
                f"Removed **{parsed_amount:,} Պ** from {user.mention}'s balance.\n"
                f"**New Balance:** {new_total:,} Պ"
            ),
            color=discord.Color.dark_red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

    elif action == "set":
        current = merit_points.get(target_id, 0)
        diff = parsed_amount - current
        merit_points[target_id] = parsed_amount
        save_merit_points()

        # Log transaction
        if target_id not in merit_transactions:
            merit_transactions[target_id] = []
        merit_transactions[target_id].append({
            "amount": diff,
            "source": "Admin",
            "description": f"Set to {parsed_amount} by {interaction.user.display_name}",
            "timestamp": int(time.time())
        })
        save_merit_transactions()

        embed = discord.Embed(
            title="Admin Action: Money Set",
            description=(
                f"Set {user.mention}'s balance to **{parsed_amount:,} Պ**.\n"
                f"**Previous Balance:** {current:,} Պ"
            ),
            color=discord.Color.blue()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed)

    elif action == "clear_all":
        current = merit_points.get(target_id, 0)
        if current > 0:
            merit_points.pop(target_id, None)
            save_merit_points()

            # Log transaction
            if target_id not in merit_transactions:
                merit_transactions[target_id] = []
            merit_transactions[target_id].append({
                "amount": -current,
                "source": "Admin",
                "description": f"Cleared by {interaction.user.display_name}",
                "timestamp": int(time.time())
            })
            save_merit_transactions()

        embed = discord.Embed(
            title="Admin Action: All Money Cleared",
            description=f"Cleared all merit points for {user.mention}.",
            color=discord.Color.dark_red()
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
    app_commands.Choice(name="Clear All Shift Time", value="clear_all"),
    app_commands.Choice(name="Force Quota Reset (Global)", value="force_quota_reset")
])
async def shift_admin(
    interaction: discord.Interaction,
    user: discord.Member,
    option: app_commands.Choice[str],
    time_input: str = None
):
    if not has_web_panel_role(interaction) and not is_admin(interaction):
        embed = discord.Embed(
            title="Access Denied",
            description="You do not have the required role (1538076681586217031) to use admin shift commands.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    target_id = user.id
    action = option.value

    if action == "start_shift":
        async with shift_lock:
            if target_id in active_shifts:
                embed = discord.Embed(
                    title="Admin Override Error",
                    description=f"{user.mention} is already on an active shift.",
                    color=discord.Color.red()
                )
                embed.set_footer(text="Made by zeemonyt")
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            raw_nick = user.nick if user.nick else user.name
            original_nick = raw_nick[len(PREFIX):] if raw_nick.startswith(PREFIX) else raw_nick
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
        async with shift_lock:
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

        async with shift_lock:
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

        async with shift_lock:
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
        async with shift_lock:
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
        async with shift_lock:
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

    elif action == "force_quota_reset":
        await interaction.response.defer(ephemeral=True)
        performed = await run_quota_reset(guild=interaction.guild, force=True)
        if performed:
            embed = discord.Embed(
                title="✅ Quota Reset Forced",
                description="Weekly quota reset has been manually triggered. Leaderboards cleared, quota bonuses awarded, and report sent to log channel.",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="ℹ️ Quota Reset Not Needed",
                description="The quota reset was already performed for this week, or it's not yet Saturday midnight CST. Use this command only if you need to force an immediate reset.",
                color=discord.Color.blue()
            )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.followup.send(embed=embed, ephemeral=True)


# Activity Check Configuration
ACTIVITY_CHECK_CHANNEL_ID = 1539668156153602218

# Rank hierarchy from highest to lowest (for demotion)
# Based on QuickRanks.txt ordering
RANK_HIERARCHY = [
    # SHR
    (1539431991080910899, "GLARP Ownership"),
    (1538076681640747136, "Overseer"),
    (1538076681640747135, "High Ranking Team"),
    (1538076681640747128, "Department Head"),
    # HR
    (1538076681640747133, "Sheriff"),
    (1538076681640747132, "Undersheriff"),
    (1539413609388712006, "Chief"),
    (1538076681640747131, "Chief Deputy"),
    (1550735626460995636, "Assistant Chief"),
    (1550735900596506644, "Captain"),
    (1538076681640747130, "Deputy Captain"),
    # MR
    (1550736797385101424, "Commander"),
    (1550737209466818570, "Staff Lieutenant"),
    (1538076681632219216, "Lieutenant"),
    (1550737321991606362, "First Lieutenant"),
    (1550736797695479840, "Major"),
    (1550737452056973332, "Sergeant Major"),
    (1538076681628028957, "Sergeant"),
    (1538076681628028955, "Master Deputy"),
    (1550738579016253461, "Senior Supervisor"),
    (1538076681628028956, "Supervisor"),
    (1550738587278909531, "Senior Field Training Officer"),
    # LR
    (1550738620669894727, "Field Training Officer"),
    (1550738623534596156, "Deputy First Class"),
    (1538076681628028953, "Senior Deputy"),
    (1538076681628028952, "Deputy"),
    (1550738635706601482, "Junior Deputy"),
]

# Role IDs that are required to meet weekly shift quota
# Includes Web Panel role (verified staff) + all ranked roles from hierarchy
QUOTA_ELIGIBLE_ROLE_IDS = {WEB_PANEL_ROLE_ID} | {role_id for role_id, _ in RANK_HIERARCHY}

# Activity check storage
activity_check_data = {
    "message_id": None,
    "start_time": None,
    "channel_id": ACTIVITY_CHECK_CHANNEL_ID,
}


def get_member_highest_rank(member: discord.Member) -> tuple[int, str] | None:
    """Returns (role_id, role_name) of the member's highest rank from the hierarchy."""
    member_role_ids = {role.id for role in member.roles}
    for role_id, role_name in RANK_HIERARCHY:
        if role_id in member_role_ids:
            return (role_id, role_name)
    return None


def get_next_lower_rank(current_role_id: int) -> tuple[int, str] | None:
    """Returns the next lower rank in the hierarchy, or None if at bottom."""
    for i, (role_id, role_name) in enumerate(RANK_HIERARCHY):
        if role_id == current_role_id:
            if i + 1 < len(RANK_HIERARCHY):
                return RANK_HIERARCHY[i + 1]
            return None  # At bottom of hierarchy
    return None


# --- PURGE COMMANDS ---

def parse_duration_string(duration_str: str) -> int:
    """Parses duration strings like '12h', '15m 2s', '30m', '1h 30m' into seconds."""
    if not duration_str or duration_str.strip() == "0s":
        return 0
    duration_str = duration_str.lower().strip()

    total_seconds = 0
    pattern = re.compile(r'(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?')
    match = pattern.fullmatch(duration_str)
    if match:
        hours = int(match.group(1)) if match.group(1) else 0
        minutes = int(match.group(2)) if match.group(2) else 0
        seconds = int(match.group(3)) if match.group(3) else 0
        total_seconds = (hours * 3600) + (minutes * 60) + seconds
    return total_seconds


def is_hr_plus(interaction: discord.Interaction) -> bool:
    """Checks if the user is HR+ (has HR/SHR roles from QuickRanks.txt)."""
    if interaction.user.id in ALLOWED_USER_IDS:
        return True
    if isinstance(interaction.user, discord.Member):
        user_role_ids = {role.id for role in interaction.user.roles}
        # HR+ roles from QuickRanks.txt (HR and SHR ranks)
        hr_shr_roles = {
            # SHR
            1539431991080910899,  # GLARP Ownership
            1538076681640747136,  # Overseer
            1538076681640747135,  # High Ranking Team
            1538076681640747133,  # Sheriff
            1538076681640747132,  # Undersheriff
            1539413609388712006,  # Chief
            1538076681640747131,  # Chief Deputy
            1550735626460995636,  # Assistant Chief
            1550735900596506644,  # Captain
            1538076681640747130,  # Deputy Captain
            # HR
            1550736797385101424,  # Commander
            1550737209466818570,  # Staff Lieutenant
            1538076681632219216,  # Lieutenant
            1550737321991606362,  # First Lieutenant
            1550736797695479840,  # Major
            1550737452056973332,  # Sergeant Major
            1538076681628028957,  # Sergeant
            1538076681628028955,  # Master Deputy
            1550738579016253461,  # Senior Supervisor
            1538076681628028956,  # Supervisor
            1550738587278909531,  # Senior Field Training Officer
        }
        return bool(user_role_ids.intersection(hr_shr_roles))
    return False


@bot.tree.command(name="unverified-purge", description="HR+: Purge unverified members with optional DM delay.")
@app_commands.describe(
    purge_delay="Time before purge (e.g. 1h, 30m, 1h 30m). 0s = now",
    dm_delay="Time before sending DM warning (e.g. 1h, 30m). 0s = now"
)
async def unverified_purge(
    interaction: discord.Interaction,
    purge_delay: str = "0s",
    dm_delay: str = "0s"
):
    if not is_hr_plus(interaction):
        embed = discord.Embed(
            title="Permission Denied",
            description="HR+ only command.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Guild not found.", ephemeral=True)
        return

    unverified_role = guild.get_role(UNVERIFIED_ROLE_ID)
    verified_role = guild.get_role(VERIFIED_ROLE_ID)

    if not unverified_role:
        await interaction.response.send_message("Unverified role not found.", ephemeral=True)
        return

    # Get members with unverified role but NOT verified role
    targets = [m for m in unverified_role.members if not verified_role or verified_role not in m.roles]

    if not targets:
        await interaction.response.send_message("No unverified members to purge (excluding those with verified role).", ephemeral=True)
        return

    purge_seconds = parse_duration_string(purge_delay)
    dm_seconds = parse_duration_string(dm_delay)
    purge_time = time.time() + purge_seconds
    dm_time = time.time() + dm_seconds

    dm_message = (
        "Hello, this is the GLARP I LASD bot! This is a reminder that you will be kicked from LASD "
        "as you are not verified. Verify in "
        "https://discord.com/channels/1538076681565241384/1545124075989504170"
    )

    embed = discord.Embed(
        title="🧹 Unverified Purge Scheduled",
        description=(
            f"**Target Count:** {len(targets)} members\n"
            f"**Purge In:** {'Now' if purge_seconds == 0 else f'<t:{int(purge_time)}:R>'}\n"
            f"**DM Warning In:** {'Now' if dm_seconds == 0 else f'<t:{int(dm_time)}:R>'}\n\n"
            "Members with the verified role are excluded from this purge."
        ),
        color=discord.Color.orange()
    )
    embed.set_footer(text=f"Initiated by {interaction.user.display_name} • Made by zeemonyt")
    await interaction.response.send_message(embed=embed)

    # Schedule DM sending
    if dm_seconds > 0:
        async def send_dms():
            await asyncio.sleep(dm_seconds)
            for member in targets:
                try:
                    await safe_send_dm(member, dm_message)
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass
        asyncio.create_task(send_dms())
    else:
        for member in targets:
            try:
                await safe_send_dm(member, dm_message)
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

    # Schedule purge
    async def do_purge():
        await asyncio.sleep(purge_seconds)
        for member in targets:
            try:
                await member.kick(reason="Unverified purge - failed to verify in time")
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

        # Log to strike channel
        log_channel = guild.get_channel(STRIKE_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title="🧹 Unverified Purge Executed",
                description=f"**Executed by:** {interaction.user.mention}\n**Purged:** {len(targets)} members",
                color=discord.Color.red()
            )
            log_embed.set_footer(text="Made by zeemonyt")
            try:
                await safe_send(log_channel, embed=log_embed)
            except discord.Forbidden:
                pass

    asyncio.create_task(do_purge())


@bot.tree.command(name="untrained-purge", description="HR+: Purge untrained members (Requires Training / RA) with DM warning.")
@app_commands.describe(
    purge_delay="Time before purge (e.g. 12h, 15m 2s). 0s = now",
    target_group="Which group to purge",
    dm_delay="Time before sending DM warning. 0s = now"
)
@app_commands.choices(target_group=[
    app_commands.Choice(name="Requires Training", value="training"),
    app_commands.Choice(name="RA (Ride Along)", value="ra"),
    app_commands.Choice(name="Both", value="both")
])
async def untrained_purge(
    interaction: discord.Interaction,
    purge_delay: str = "0s",
    target_group: app_commands.Choice[str] = "both",
    dm_delay: str = "0s"
):
    if not is_hr_plus(interaction):
        embed = discord.Embed(
            title="Permission Denied",
            description="HR+ only command.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Guild not found.", ephemeral=True)
        return

    training_role = guild.get_role(REQUIRES_TRAINING_ROLE_ID)
    ra_role = guild.get_role(RA_ROLE_ID)
    verified_role = guild.get_role(VERIFIED_ROLE_ID)

    targets = []
    group_name = ""

    if target_group.value in ["training", "both"] and training_role:
        targets.extend([m for m in training_role.members if not verified_role or verified_role not in m.roles])
        group_name += "Requires Training, "
    if target_group.value in ["ra", "both"] and ra_role:
        targets.extend([m for m in ra_role.members if not verified_role or verified_role not in m.roles])
        group_name += "RA, "

    # Deduplicate
    seen = set()
    unique_targets = []
    for m in targets:
        if m.id not in seen:
            seen.add(m.id)
            unique_targets.append(m)
    targets = unique_targets
    group_name = group_name.rstrip(", ")

    if not targets:
        await interaction.response.send_message(f"No members found in {group_name} (excluding those with verified role).", ephemeral=True)
        return

    purge_seconds = parse_duration_string(purge_delay)
    dm_seconds = parse_duration_string(dm_delay)
    purge_time = time.time() + purge_seconds
    dm_time = time.time() + dm_seconds

    # Format the time until purge for the DM message
    if purge_seconds > 0:
        time_until_purge = format_duration(purge_seconds)
    else:
        time_until_purge = "immediately"

    dm_message = (
        "Hello, this is the GLARP I LASD bot! This is a reminder that you will be kicked from LASD "
        f"as you are not trained currently. You have {time_until_purge} to get trained. "
        "Request a training in "
        "https://discord.com/channels/1538076681565241384/1538076682894843966"
    )

    embed = discord.Embed(
        title="🎓 Untrained Purge Scheduled",
        description=(
            f"**Target Group:** {group_name}\n"
            f"**Target Count:** {len(targets)} members\n"
            f"**Purge In:** {'Now' if purge_seconds == 0 else f'<t:{int(purge_time)}:R>'}\n"
            f"**DM Warning In:** {'Now' if dm_seconds == 0 else f'<t:{int(dm_time)}:R>'}\n\n"
            "Members with the verified role are excluded from this purge."
        ),
        color=discord.Color.orange()
    )
    embed.set_footer(text=f"Initiated by {interaction.user.display_name} • Made by zeemonyt")
    await interaction.response.send_message(embed=embed)

    # Schedule DM sending
    if dm_seconds > 0:
        async def send_dms():
            await asyncio.sleep(dm_seconds)
            for member in targets:
                try:
                    await safe_send_dm(member, dm_message)
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass
        asyncio.create_task(send_dms())
    else:
        for member in targets:
            try:
                await safe_send_dm(member, dm_message)
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

    # Schedule purge
    async def do_purge():
        await asyncio.sleep(purge_seconds)
        for member in targets:
            try:
                await member.kick(reason=f"Untrained purge ({group_name}) - failed to complete training in time")
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

        # Log to strike channel
        log_channel = guild.get_channel(STRIKE_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title="🎓 Untrained Purge Executed",
                description=f"**Executed by:** {interaction.user.mention}\n**Group:** {group_name}\n**Purged:** {len(targets)} members",
                color=discord.Color.red()
            )
            log_embed.set_footer(text="Made by zeemonyt")
            try:
                await safe_send(log_channel, embed=log_embed)
            except discord.Forbidden:
                pass

    asyncio.create_task(do_purge())


# --- ACTIVITY CHECK COMMAND ---

async def run_activity_check(guild: discord.Guild, message_id: int, initiated_by: int):
    """Runs the activity check after 24 hours - demotes members who didn't react."""
    await asyncio.sleep(24 * 60 * 60)  # 24 hours

    channel = guild.get_channel(ACTIVITY_CHECK_CHANNEL_ID)
    if not channel:
        print(f"Activity check: Channel {ACTIVITY_CHECK_CHANNEL_ID} not found")
        return

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        print(f"Activity check: Message {message_id} not found")
        return
    except discord.Forbidden:
        print(f"Activity check: No permission to fetch message")
        return
    except discord.HTTPException as e:
        print(f"Activity check: HTTP error fetching message: {e}")
        return

    # Get the checkmark reaction
    checkmark_reaction = None
    for reaction in message.reactions:
        if str(reaction.emoji) == "✅":
            checkmark_reaction = reaction
            break

    if not checkmark_reaction:
        print("Activity check: No checkmark reaction found on message")
        return

    # Get all users who reacted with checkmark
    reacted_users = set()
    try:
        async for user in checkmark_reaction.users():
            reacted_users.add(user.id)
    except discord.Forbidden:
        print("Activity check: No permission to fetch reaction users")
        return
    except discord.HTTPException as e:
        print(f"Activity check: HTTP error fetching reaction users: {e}")
        return

    # Get all members who can view the channel (have View Channel permission)
    # We'll check all members in the guild who have the permission
    eligible_members = []
    for member in guild.members:
        if member.bot:
            continue
        # Check if member has permission to view the channel
        perms = channel.permissions_for(member)
        if perms.view_channel:
            eligible_members.append(member)

    # Find members who didn't react
    non_reacted = [m for m in eligible_members if m.id not in reacted_users]

    # Demote non-reacted members
    demoted_count = 0
    demoted_details = []

    for member in non_reacted:
        highest_rank = get_member_highest_rank(member)
        if not highest_rank:
            # No rank to demote
            continue

        current_role_id, current_role_name = highest_rank
        next_rank = get_next_lower_rank(current_role_id)

        current_role = guild.get_role(current_role_id)
        if not current_role:
            continue

        try:
            # Remove current rank
            await safe_remove_roles(member, current_role, reason="Activity check: Failed to react within 24 hours")

            # Add next lower rank if exists
            if next_rank:
                next_role_id, next_role_name = next_rank
                next_role = guild.get_role(next_role_id)
                if next_role:
                    await safe_add_roles(member, next_role, reason="Activity check: Demoted to next lower rank")
                    demoted_details.append(f"• {member.mention}: {current_role_name} → {next_role_name}")
                else:
                    demoted_details.append(f"• {member.mention}: {current_role_name} → (removed, next role not found)")
            else:
                demoted_details.append(f"• {member.mention}: {current_role_name} → (removed, at bottom of hierarchy)")

            demoted_count += 1

        except discord.Forbidden:
            demoted_details.append(f"• {member.mention}: Failed to demote (missing permissions)")
        except discord.HTTPException as e:
            demoted_details.append(f"• {member.mention}: Failed to demote ({e})")

    # Log results
    log_channel = guild.get_channel(STRIKE_LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(
            title="⏰ Activity Check Complete",
            description=(
                f"**Channel:** {channel.mention}\n"
                f"**Message:** [Jump to message]({message.jump_url})\n"
                f"**Eligible Members:** {len(eligible_members)}\n"
                f"**Reacted:** {len(reacted_users)}\n"
                f"**Demoted:** {demoted_count}"
            ),
            color=discord.Color.red() if demoted_count > 0 else discord.Color.green()
        )
        if demoted_details:
            embed.add_field(name="Demotion Details", value="\n".join(demoted_details[:20]), inline=False)
            if len(demoted_details) > 20:
                embed.add_field(name="", value=f"... and {len(demoted_details) - 20} more", inline=False)
        embed.set_footer(text=f"Initiated by <@{initiated_by}> • Made by zeemonyt")
        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            pass

    # Clear activity check data
    activity_check_data["message_id"] = None
    activity_check_data["start_time"] = None


@bot.tree.command(name="activity-check", description="HR+: Start an activity check in the activity channel.")
async def activity_check(interaction: discord.Interaction):
    if not is_hr_plus(interaction):
        embed = discord.Embed(
            title="Permission Denied",
            description="HR+ only command.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Check if there's already an active activity check
    if activity_check_data["message_id"]:
        embed = discord.Embed(
            title="Activity Check Already Active",
            description="There is already an active activity check. Wait for it to complete or use the admin panel to cancel.",
            color=discord.Color.orange()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    channel = interaction.guild.get_channel(ACTIVITY_CHECK_CHANNEL_ID)
    if not channel:
        try:
            channel = await interaction.guild.fetch_channel(ACTIVITY_CHECK_CHANNEL_ID)
        except Exception:
            embed = discord.Embed(
                title="Error",
                description=f"Could not find the activity check channel (`{ACTIVITY_CHECK_CHANNEL_ID}`).",
                color=discord.Color.red()
            )
            embed.set_footer(text="Made by zeemonyt")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

    # Send the activity check message
    try:
        message = await safe_send(
            channel,
            content="<@&1538076681586217031>",
            embed=discord.Embed(
                title="Activity Check!",
                description="Please react with ✅ if you see this message. \nIf you are detected not reacting, you will be demoted.",
                color=discord.Color.blurple()
            ).set_author(name="LASD Activity Check").set_footer(text="Made by zeemonyt."),
            allowed_mentions=discord.AllowedMentions(roles=True)
        )
    except discord.Forbidden:
        embed = discord.Embed(
            title="Error",
            description="Bot is missing permissions to send messages in the activity check channel.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    except discord.HTTPException as e:
        embed = discord.Embed(
            title="Error",
            description=f"Failed to send activity check message: {e}",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Add checkmark reaction
    try:
        await safe_add_reaction(message, "✅")
    except discord.HTTPException as e:
        print(f"Failed to add reaction: {e}")

    # Store activity check data
    activity_check_data["message_id"] = message.id
    activity_check_data["start_time"] = time.time()

    # Schedule the 24-hour check
    asyncio.create_task(run_activity_check(interaction.guild, message.id, interaction.user.id))

    embed = discord.Embed(
        title="✅ Activity Check Started",
        description=(
            f"Activity check message sent to {channel.mention}.\n"
            f"**Message ID:** `{message.id}`\n"
            f"**Check completes in:** 24 hours (<t:{int(time.time() + 86400)}:R>)\n\n"
            f"Members with access to {channel.mention} must react with ✅ within 24 hours "
            f"or they will be demoted one rank."
        ),
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Initiated by {interaction.user.display_name} • Made by zeemonyt")
    await interaction.response.send_message(embed=embed)


# --- STRIKE / INFRACTION COMMAND ---

async def log_strike_event(guild: discord.Guild, embed: discord.Embed):
    """Logs strike actions to the dedicated #strikes channel."""
    if guild:
        channel = guild.get_channel(STRIKE_LOG_CHANNEL_ID)
        if channel:
            try:
                await safe_send(channel, embed=embed)
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
    plate="Optional license plate (max 10 chars)",
    image="Optional proof image"
)
async def cite(
    interaction: discord.Interaction,
    user: str,
    reason: str,
    court_date: str = None,
    plate: str = None,
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
        f"Plate: {plate.upper()[:10] if plate else 'N/A'}\n"
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
            await safe_send(
                channel,
                content=content,
                file=file,
                allowed_mentions=discord.AllowedMentions(users=[interaction.user])
            )
        else:
            await safe_send(
                channel,
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

    if plate:
        plate_clean = plate.strip().upper()[:10]
        records_list.insert(0, {
            "id": int(time.time() * 1000),
            "type": "citation",
            "plate": plate_clean,
            "vehicle": "Unknown Vehicle",
            "subject": user,
            "secondary": reason[:50],
            "description": f"Citation issued to {user}: {reason} (Court: {court_date or 'None'})",
            "timestamp": time.time() * 1000
        })
        save_shift_data()

    embed = discord.Embed(
        title="Citation Successfully Logged",
        description=f"Logged citation for Roblox user **{user}** in <#{CITE_LOG_CHANNEL_ID}>.",
        color=discord.Color.green()
    )
    embed.set_footer(text="Made by zeemonyt")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- ARREST COMMAND ---

@bot.tree.command(name="arrest", description="Log a Roblox user arrest with charges, fine/jail time, notes, and evidence.")
@app_commands.describe(
    user="Roblox username of the arrested subject",
    charges="Charges / Penal Code (e.g. PC 241 - Assault on Peace Officer)",
    details="Fine Amount ($) & Jail Time (e.g. $5,000 / 30 Minutes)",
    notes="Optional officer notes or summary",
    plate="Optional license plate (max 10 chars)",
    image="Primary proof image attachment",
    image1="Optional second proof image",
    image2="Optional third proof image"
)
async def arrest(
    interaction: discord.Interaction,
    user: str,
    charges: str,
    details: str,
    notes: str = "",
    plate: str = None,
    image: discord.Attachment = None,
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

    reason_str = f"{charges} - {details}"
    if notes:
        reason_str += f" (Notes: {notes})"

    content = (
        f"User: {user}\n"
        f"Reason for arrest: {reason_str}\n"
        f"Plate: {plate.upper()[:10] if plate else 'N/A'}\n"
        f"Image:\n"
        f"Arresting Officer: {interaction.user.mention}"
    )

    files = []
    try:
        if image:
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
        if files:
            await safe_send(channel, content=content, files=files, allowed_mentions=discord.AllowedMentions(users=[interaction.user]))
        else:
            await safe_send(channel, content=content, allowed_mentions=discord.AllowedMentions(users=[interaction.user]))
    except discord.Forbidden:
        embed = discord.Embed(
            title="Arrest Log Error",
            description="Bot is missing permissions to send messages or files in the arrest channel.",
            color=discord.Color.red()
        )
        embed.set_footer(text="Made by zeemonyt")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if plate:
        plate_clean = plate.strip().upper()[:10]
        records_list.insert(0, {
            "id": int(time.time() * 1000),
            "type": "arrest",
            "plate": plate_clean,
            "vehicle": "Unknown Vehicle",
            "subject": user,
            "secondary": charges[:50],
            "description": f"Arrest logged for {user}: {charges} - {details}. Notes: {notes}",
            "timestamp": time.time() * 1000
        })
        save_shift_data()

    # Award $10 merit points for arrest
    await add_merit_points(interaction.user.id, 10, source="Arrest", description="Arrest log submitted via /arrest command")

    embed = discord.Embed(
        title="Arrest Successfully Logged",
        description=f"Logged arrest for Roblox user **{user}** in <#{ARREST_LOG_CHANNEL_ID}>.\n**+10 Պ** earned!",
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