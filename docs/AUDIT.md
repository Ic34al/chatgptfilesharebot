# Stage 1 — Repository audit (27 Oct 2026)

## Access
- Repository: `Ic34al/chatgptfilesharebot`, default branch `main`, account permission **ADMIN** (push allowed).
- Work branch: `feature/anime-soul-automation`. `main` was not modified.

## What was actually on `main`
`main` contained **no source files**. History (6 commits, all 17 Aug 2026):

| Commit | Change |
|---|---|
| `ade40bd` | Initial commit (README) |
| `d53aa10` | Created an empty file whose *name* was a pasted file tree |
| `2e5698b` | Deleted that directory |
| `85157fd` | Uploaded `Fast-Telegram-File-Share-Bot-Heroku.zip` |
| `0b15c55` | Deleted README.md |
| `ffbdbdd` | Deleted the zip → branch empty |

The real bot was recovered from the zip in `85157fd` and restored as the first commit on the
feature branch, so every change after it is a reviewable diff against the original code.

## Original code (≈150 lines)
| File | Purpose |
|---|---|
| `main.py` | `asyncio.run(run())` |
| `bot.py` | Pyrogram client + handlers: `/start <token>` sends stored file, `/start` welcome, `/help`, any media → share link, `/stats` (owner) |
| `database.py` | `FileDB` (Motor): collection `files` `{token, file_id, unique_id, protect}` unique index on `token` |
| `helper_func.py` | `encode_token` = urlsafe base64 of `file_id`; `decode_token`; `short_hash` |
| `requirements.txt` | pyrogram 2.0.106, tgcrypto 1.2.5, motor 3.7.1, pymongo 4.14.0, dnspython 2.7.0 |
| `Procfile` | `worker: python main.py` |
| `runtime.txt` | `python-3.11.9` |
| `app.json` | Heroku manifest, 1 basic worker |

Config vars: `API_ID`, `API_HASH`, `BOT_TOKEN`, `MONGODB_URI`, `DATABASE_NAME`, `OWNER_ID`.

## Files named in the brief that did not exist
`start.py`, `link_generator.py`, FSUB/subscription handlers, broadcast/admin handlers. There was no
FSUB, no broadcast and no user collection, so these were **built new** rather than "preserved".

## Problems found in the original
1. **Share links were broken for most files.** Tokens were base64 of the full `file_id`
   (≈100+ chars) but Telegram only passes `/start` parameters up to 64 characters
   (`A-Z a-z 0-9 _ -`). New links now use 16-character random tokens; old tokens still resolve.
2. Anyone could upload in any chat where the bot was present (including channels). Share-link
   creation is now limited to private chats; `PUBLIC_FILE_LINKS=false` restricts it to admins.
3. `bot.py` read env vars at import time and crashed if any were missing (untestable).
4. Pyrogram 2.0.106 rejects newer channel IDs (`-100` + 13 digits) with `Peer id invalid`
   (`MIN_CHANNEL_ID = -1002147483647`). Patched at startup in `anime_soul/runner.py`.
5. Pyrogram 2.0.106 is no longer maintained upstream. It was kept (as instructed) and works on
   Python 3.11; a future move to a maintained fork (e.g. Pyrofork/Kurigram) is recommended.
6. `runtime.txt` is deprecated on Heroku; replaced by `.python-version` (`3.11`). Python 3.11 is
   pinned deliberately: Heroku's default for new apps is 3.14, which Pyrogram 2.0.106 was not built for.


---

## Second audit — full automatic pipeline (27 Oct 2026, branch `feature/anime-soul-full-pipeline`)

Inspected `main` after PR #1 (all 93 tests passing) and the reference
[MatizTech/Auto-Anime-Bot](https://github.com/MatizTech/Auto-Anime-Bot). The reference follows
RSS release feed → torrent download → FFmpeg encode → Telegram upload → index-channel post.
Its *structure* was reused (feed monitor, encode stage, upload, post); its torrent/third-party
release sources were **not** — only feeds and files you are authorized to redistribute are supported.

### Already working (kept unchanged)
AniList/Jikan schedule monitor in IST · unique episode keys and MongoDB leases · authorized sources
(`/attach` inbox, JSON partner feed, local library) with SSRF-safe streaming downloads · storage upload
with saved `chat_id`/`message_id` · one post per episode with **GET EPISODE** →
`https://t.me/<bot>?start=<token>` (format from `deeplink.build_link`, decoded by the existing
`/start` handler; legacy share links untouched) · FSUB with join requests and TRY AGAIN · admin
commands · dry run · ROLE=all/bot/pipeline · graceful shutdown.

### Missing or weak → fixed
| Gap | Fix |
|---|---|
| No FFmpeg stage — only an optional ffprobe duration check | New `anime_soul/processing.py`: ffprobe validation (video + audio streams, duration, size, format), `PROCESS_MODE=auto` remuxes only when lossless, optional transcode, timeouts, disk checks; new `processing` state |
| FFmpeg not reliably available on Heroku | `app.json` uses Heroku's `activestorage-preview` buildpack (ffmpeg + ffprobe); startup alert and `/status` line if missing |
| No RSS/Atom support (the reference's release mechanism) | `rss_feed` source (`/sources add_rss`), defusedxml parser, torrents/magnets ignored |
| Releases only from AniList | Authorized feeds also create episodes (`FEED_DETECT_RELEASES`), deduped, no backfill |
| "No source" looked like a silent wait | `SOURCE_NOT_CONFIGURED` status, owner alert, shown in `/queue` and `/status` |
| Two monitors if ROLE=all and the uploader dyno both ran | MongoDB monitor leader lock |
| Dry run checked only before acquiring; an already-queued job could still upload | Dry run/pause re-checked before download, upload and post |
| `/testupload publish` posted straight to the public channel | Needs `TEST_CHANNEL_ID`, or an explicit `publish live` |
| `/testupload` only copied a Telegram message | `/testupload full` runs download → FFmpeg → upload |
| `DRY_RUN` defaulted to false when unset | Defaults to **true** |
| No `/queue`; `/help` didn't list admin commands | Added |
| Post lacked synopsis/status; quality came from the feed label | AniList synopsis (only when present), release status, FFprobe-verified quality |
| Bad numeric config gave a bare `ValueError` | `ConfigError` naming the variable (never its value) |
| Admin alerts/errors could echo secrets in exception text | Redaction applied to alerts and command errors |
