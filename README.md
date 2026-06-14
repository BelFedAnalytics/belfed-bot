# belfed-bot — production source

Production source of truth for `@BelfedBot` (Telegram bot for BelFed swing trading analytics platform).

## Branches

- **`prod`** — *default*. Mirrors live state of `/home/belfed/` on the production VPS. Updated by direct commits + push, then `git pull` on VPS.
- **`legacy/master-2026-06-14`** — frozen tag of the old `master` branch state, kept for history. The old `master` had diverged significantly from production (1250 lines vs ~3420 lines) and is no longer maintained.

## Files

- `bot.py` — main entry point. Run by `belfedbot.service` on the VPS as `/usr/bin/python3 /home/belfed/bot.py`.
- `positions.py` — helpers for trade-position data.
- `requirements.txt` — minimum pinned deps actually used in production.
- `.gitignore` — excludes secrets, backups, caches, logs.

## Deploy workflow

Production lives at `root@204.168.153.190:/home/belfed/`.

```bash
# 1. Commit + push prod-ready change to GitHub
git add bot.py
git commit -m "feat: ..."
git push origin prod

# 2. Pull on VPS and restart
ssh root@204.168.153.190 'cd /home/belfed && git pull --ff-only && systemctl restart belfedbot.service && sleep 2 && systemctl is-active belfedbot.service'

# 3. Tail logs to verify
ssh root@204.168.153.190 'journalctl -u belfedbot.service -n 50 --no-pager'
```

## Secrets

The bot reads environment from `/etc/belfedbot.env` on the VPS. This file is **never** committed to git. Keys include `TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `BOT_SHARED_SECRET`, etc.

## Backups

Historical pre-git backups (27 files from manual `cp bot.py bot.py.bak-*` flow) are preserved on the VPS under `/home/belfed/.archive/`, ignored by git. A full pre-migration tar lives at `/root/belfed-pre-git-20260614-140423.tar.gz`.
