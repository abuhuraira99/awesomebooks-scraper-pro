#!/usr/bin/env bash
# Start the dashboard on macOS / Linux (creates the virtualenv on first run).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi

exec .venv/bin/python -m awesomebooks_pro "$@"
