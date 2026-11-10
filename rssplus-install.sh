#!/usr/bin/env bash
# Run inside a clone of your fork on a VPS. Requires Docker Compose.
set -euo pipefail
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo "Docker Engine is required" >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose plugin is required" >&2; exit 1; }
[[ ! -e .env ]] || { echo ".env already exists; preserving it"; }
if [[ ! -f .env ]]; then
  read -r -s -p "BotFather TOKEN: " BOT_TOKEN; echo
  read -r -p "Numeric Telegram MANAGER ID: " MANAGER_ID
  [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || { echo "Invalid bot token format" >&2; exit 1; }
  [[ "$MANAGER_ID" =~ ^[0-9]+$ ]] || { echo "Invalid manager ID" >&2; exit 1; }
  umask 077
  printf 'TOKEN=%s\nMANAGER=%s\n' "$BOT_TOKEN" "$MANAGER_ID" > .env
  unset BOT_TOKEN
fi
mkdir -p config
if [[ ! -f config/rssplus.json ]]; then
  cp config/rssplus.json.example config/rssplus.json
  MANAGER_ID="$(sed -n 's/^MANAGER=//p' .env)"
  python3 - "$MANAGER_ID" <<'PY'
import json, sys
from pathlib import Path
p=Path("config/rssplus.json")
data=json.loads(p.read_text(encoding="utf-8"))
data["review_chat_id"]=int(sys.argv[1].split(';')[0])
p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
PY
fi
chmod 600 .env config/rssplus.json
docker compose up -d --build
echo "Started. Check with: docker compose logs --tail=100 -f"
