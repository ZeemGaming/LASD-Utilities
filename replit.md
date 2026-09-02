# Running the project

This project is a Python Discord bot with an HTTP health endpoint.

## Run

Use the configured `Run` workflow, which executes:

```text
python main.py
```

The health endpoint listens on port `5000` and returns `Bot is alive!`.

## Required secret

The bot requires a `DISCORD_TOKEN` Replit Secret before it can connect to Discord. Never commit the token to the repository or place it in source code.