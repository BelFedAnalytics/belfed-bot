#!/usr/bin/env bash
# Rotate TELEGRAM_BOT_TOKEN in /etc/belfedbot.env and restart the service.
#
# Runs ON THE VPS. The new token arrives on stdin (one line) and is never passed
# as an argument, never echoed, and never written anywhere except the env file,
# so it does not leak into `ps`, shell history or CI logs.
#
# Usage (from the deploy workflow):
#   scp scripts/rotate_bot_token.sh root@vps:/tmp/rotate_bot_token.sh
#   printf '%s' "$TOKEN" | ssh root@vps 'bash /tmp/rotate_bot_token.sh; rm -f /tmp/rotate_bot_token.sh'

set -euo pipefail

ENV_FILE=/etc/belfedbot.env
SERVICE=belfedbot

IFS= read -r NEW_TOKEN || true
if [ -z "${NEW_TOKEN:-}" ]; then
  echo "::error::no token received on stdin" >&2
  exit 1
fi
case "$NEW_TOKEN" in
  [0-9]*:*) : ;;
  *) echo "::error::token does not look like a Telegram bot token" >&2; exit 1 ;;
esac

if [ ! -f "$ENV_FILE" ]; then
  echo "::error::$ENV_FILE not found" >&2
  exit 1
fi

umask 077
BACKUP="$ENV_FILE.bak.$(date -u +%Y%m%dT%H%M%SZ)"
cp -a "$ENV_FILE" "$BACKUP"
echo "backup: $BACKUP"

NEW_TOKEN="$NEW_TOKEN" ENV_FILE="$ENV_FILE" python3 <<'PY'
import os

path = os.environ["ENV_FILE"]
token = os.environ["NEW_TOKEN"]

with open(path, "r", encoding="utf-8") as fh:
    lines = fh.read().splitlines()

out, replaced = [], False
for line in lines:
    if line.startswith("TELEGRAM_BOT_TOKEN="):
        out.append("TELEGRAM_BOT_TOKEN=" + token)
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append("TELEGRAM_BOT_TOKEN=" + token)

with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")

print("TELEGRAM_BOT_TOKEN " + ("replaced" if replaced else "appended"))
PY

unset NEW_TOKEN
chmod 600 "$ENV_FILE"

systemctl restart "$SERVICE"
sleep 8

if ! systemctl is-active --quiet "$SERVICE"; then
  echo "::error::$SERVICE is not active after the token rotation, restoring $BACKUP" >&2
  cp -a "$BACKUP" "$ENV_FILE"
  systemctl restart "$SERVICE" || true
  exit 1
fi

sleep 7
if ! systemctl is-active --quiet "$SERVICE"; then
  echo "::error::$SERVICE died during the settle window, restoring $BACKUP" >&2
  cp -a "$BACKUP" "$ENV_FILE"
  systemctl restart "$SERVICE" || true
  exit 1
fi

if journalctl -u "$SERVICE" --since "-40s" --no-pager | grep -q "Application started"; then
  echo "confirmed: Application started with the new token"
else
  echo "::warning::did not see 'Application started' in the last 40s of logs"
fi

echo "$SERVICE is active"
