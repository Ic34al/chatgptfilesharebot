# ANIME SOUL — Telegram file-share bot with automatic episode publishing

Builds on the original **Fast Telegram File Share Bot** (Pyrogram + MongoDB). Everything the old bot
did still works: `/start`, `/help`, send a file → get a share link, `/stats`.

New features:
- **Schedule tracking** from AniList (Jikan as fallback), times shown in **IST**, rescheduling and sequels handled.
- **Automatic discovery** of currently airing TV/TV_SHORT/ONA anime from AniList (`AUTO_DISCOVER_AIRING_ANIME`, most popular first). Discovery only adds schedules — never video URLs.
- **Release detection** from AniList and from authorized partner RSS/Atom/JSON feeds; duplicate-safe.
- **Authorized sources only**: admin uploads (`/attach`), allow-listed partner RSS/Atom or JSON feeds, or a local licensed library. No scraping, no torrents, no DRM bypass. With no source configured, episodes wait as `SOURCE_NOT_CONFIGURED`.
- **Provider Telegram channel (fully automatic)**: a licensed provider uploads episodes to a private intake channel where the bot is admin; each file is checked, matched to the anime and episode, copied to storage and published with GET EPISODE — no `/attach`. Missed posts are read after downtime. `/sources add_tg`, `/sources scan`, `/unmatched`, `/assign`. Optional `LOG_CHANNEL_ID` progress messages.
- **FFmpeg processing**: FFprobe validation (video/audio streams, duration, size), lossless MKV → MP4 remux when possible, optional transcode/downscale, disk and timeout limits.
- **Validated uploads** to a private storage channel (size, container signature, checksum, FFprobe).
- **Channel posts** with thumbnail, HTML caption templates and a **GET EPISODE** button that opens the bot's delivery flow.
- **Force-subscribe (FSUB)** on one or more channels, optional join-request mode, TRY AGAIN button.
- **Admin controls**: `/status /queue /help /anime /addanime /removeanime /setanime /pending /retry /pause /resume /broadcast /captions /sources /testupload /attach /setthumb /dryrun /stats /adminhelp /events /checkfiles /reupload /schedule /unmatched /assign`.
- **Ongoing-release system** ([docs/RELEASE_SYSTEM.md](docs/RELEASE_SYSTEM.md)): one adapter interface for every source (`health/find/download/validate`), release history in `episode_releases`, feed retries with exponential backoff, catch-up after downtime, batch/v2/language parsing, `telegram_uploads` and `pipeline_events` records, typed failure codes, recovery for deleted storage messages, optional daily IST schedule post.
- **Safe 24/7 operation**: MongoDB leases (no duplicate uploads or posts), single-monitor lock, bounded retries with back-off, delay alerts, FloodWait handling, restart recovery, graceful shutdown, dry-run mode (default on) and `/testupload full`.

Pipeline: `DETECTED → QUEUED → DOWNLOADING → VALIDATING → PROCESSING → UPLOADING → PUBLISHED`.
Failures: `SOURCE_NOT_CONFIGURED, DOWNLOAD_FAILED, VALIDATION_FAILED, PROCESSING_FAILED, UPLOAD_FAILED, PUBLISH_FAILED` (retry with `/retry`).

## Docs
- [docs/AUDIT.md](docs/AUDIT.md) — what was in the repository, what was wrong, and the file-by-file plans
- [docs/RELEASE_SYSTEM.md](docs/RELEASE_SYSTEM.md) — sources, lifecycle, collections, monitoring, delivery
- [CREDITS.md](CREDITS.md) — attribution (AutoAnimeBot design reference, GPL-3.0) and licences
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
  monitor.py         schedule + feed release detection, release history, downtime catch-up, alerts, leader lock
  schedule_post.py   optional daily IST airing timetable
  sources/           authorized source adapters (inbox, local dir, JSON feed, RSS/Atom feed)
  processing.py      FFprobe validation, FFmpeg remux/transcode
  netsafe.py         download allow-list, SSRF protection
  validate.py        file checks
  uploader.py        storage-channel upload
  publisher.py       channel post + GET EPISODE button
  pipeline.py        job state machine with leases and retries
  fsub.py, delivery.py, deeplink.py, captions.py
  handlers/          user and admin commands
  runner.py          startup, roles, graceful shutdown
tests/               195 tests (Telegram/feeds mocked; real FFmpeg; optional real MongoDB)
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
Python version is set by `.python-version` (3.11). FFmpeg comes from the
`heroku-buildpack-activestorage-preview` buildpack listed in `app.json`. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Secrets live only in environment variables / Heroku Config Vars. Never commit them.

## Content responsibility
The bot only publishes files from sources you register as authorized, and asks for a licence note
for each one. You are responsible for holding redistribution rights for anything it publishes.
