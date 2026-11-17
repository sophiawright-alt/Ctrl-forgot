#!/bin/sh
# Mac: double-click to start Second Brain in its own window.
# Closing the app window ends the session; this Terminal window then closes too.
cd "$(dirname "$0")" || exit 1
exec python3 -B run.py --app
