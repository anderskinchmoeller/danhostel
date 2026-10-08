#!/bin/zsh
set -euo pipefail

cd /Users/anderskinch/hostel

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

exec /Users/anderskinch/hostel/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
