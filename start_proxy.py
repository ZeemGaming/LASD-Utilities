import os
import subprocess
print("Alive!")

# Completely purge the Replit system database variables out of memory
os.environ.pop("DATABASE_URL", None)

# Run LiteLLM directly while referencing your config file
subprocess.run(["./.pythonlibs/bin/litellm", "--config", "config.yaml"])
