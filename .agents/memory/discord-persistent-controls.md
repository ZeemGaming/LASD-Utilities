---
name: Persistent Discord controls
description: Startup ordering for Discord buttons and other persistent component views.
---

Register persistent Discord component views at the start of `on_ready`, before awaited startup jobs or network operations. A bot can connect to the gateway while component callbacks remain unregistered if earlier startup work raises or stalls.

**Why:** A startup error before view registration left verification buttons timing out even though the bot appeared online. Registering views before awaited startup work resolved the issue, which the user then confirmed by testing.

**How to apply:** When adding persistent buttons, modals, or other component interactions, register their views before quota checks, message fetches, command sync, or other awaited startup work. Log registration so readiness is observable.