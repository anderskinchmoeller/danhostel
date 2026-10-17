#!/bin/zsh
set -euo pipefail

cd /Users/anderskinch/hostel

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

exec /Users/anderskinch/hostel/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
