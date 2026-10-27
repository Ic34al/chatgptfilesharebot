# ANIME SOUL — Telegram file-share bot with automatic episode publishing

Builds on the original **Fast Telegram File Share Bot** (Pyrogram + MongoDB). Everything the old bot
did still works: `/start`, `/help`, send a file → get a share link, `/stats`.

New features:
- **Schedule tracking** from AniList (Jikan as fallback), times shown in **IST**, rescheduling and sequels handled.
- **Authorized sources only**: admin uploads (`/attach`), allow-listed partner JSON feeds, or a local licensed library. No scraping, no DRM bypass.
- **Validated uploads** to a private storage channel (size, container signature, checksum, optional ffprobe).
- **Channel posts** with thumbnail, HTML caption templates and a **GET EPISODE** button that opens the bot's delivery flow.
- **Force-subscribe (FSUB)** on one or more channels, optional join-request mode, TRY AGAIN button.
- **Admin controls**: `/status /anime /addanime /removeanime /setanime /pending /retry /pause /resume /broadcast /captions /sources /testupload /attach /setthumb /dryrun /stats /adminhelp`.
- **Safe 24/7 operation**: MongoDB leases (no duplicate uploads or posts), bounded retries with back-off, delay alerts, FloodWait handling, restart recovery, graceful shutdown, dry-run mode.

## Docs
- [docs/AUDIT.md](docs/AUDIT.md) — what was in the repository and what was wrong
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Telegram, MongoDB Atlas, GitHub and Heroku steps from an iPhone, troubleshooting, costs and limits
- [docs/TESTING.md](docs/TESTING.md) — automated test report and live demonstration checklist
- [.env.example](.env.example) — every setting, with placeholders only

## Layout
```
main.py              entry point (python main.py [--role all|bot|pipeline])
bot.py, database.py, helper_func.py   original modules (kept, compatible)
anime_soul/
  config.py          settings from environment variables
  schedule/          AniList + Jikan clients
  monitor.py         schedule loop, delay alerts, sequels
  sources/           authorized source adapters (inbox, local dir, http feed)
  netsafe.py         download allow-list, SSRF protection
  validate.py        file checks
  uploader.py        storage-channel upload
  publisher.py       channel post + GET EPISODE button
  pipeline.py        job state machine with leases and retries
  fsub.py, delivery.py, deeplink.py, captions.py
  handlers/          user and admin commands
  runner.py          startup, roles, graceful shutdown
tests/               93 tests with mocked Telegram/APIs
```

## Local run
```bash
python3.11 -m venv venv && . venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest            # tests, no network needed
export API_ID=... API_HASH=... BOT_TOKEN=... MONGODB_URI=... OWNER_ID=... \
       STORAGE_CHANNEL_ID=-100... POST_CHANNEL_ID=-100... DRY_RUN=true
python main.py
```

## Heroku
`Procfile`: `worker: python main.py` (scale to 1) and an optional `uploader` process for split mode.
Python version is set by `.python-version` (3.11). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Secrets live only in environment variables / Heroku Config Vars. Never commit them.

## Content responsibility
The bot only publishes files from sources you register as authorized, and asks for a licence note
for each one. You are responsible for holding redistribution rights for anything it publishes.
