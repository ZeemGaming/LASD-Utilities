---
name: Discord rate-limit visibility
description: How to observe live Discord REST limits in the bot without fighting discord.py's retry behavior.
---

Observe rate-limit response headers on discord.py's aiohttp session rather than relying only on the bot's wrapper functions. discord.py handles REST 429 responses and retries internally, so application-level wrappers may never see those exceptions.

**Why:** The bot uses Discord REST requests throughout many command and event handlers, not all through the existing retry wrappers. A single HTTP trace can observe `Retry-After`, `X-RateLimit-Reset-After`, `X-RateLimit-Reset`, remaining capacity, and global-limit signals across those requests.

**How to apply:** Attach an `aiohttp.TraceConfig` when constructing the discord.py client. Treat limits as observed state: Discord does not provide a reliable prediction for future rate limits before requests are made. Keep route labels sanitized and expire or bound cached route observations.
