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

## Third audit — ongoing-release system (28 Oct 2026, branch `feature/ongoing-release-system`)

Inspected `main` at `1affaf4` (144 passed / 148 with real MongoDB) and the reference
[kaif-00z/AutoAnimeBot](https://github.com/kaif-00z/AutoAnimeBot) (GPL-3.0): `libs/subsplease.py`
(RSS polling), `functions/schedule.py` (daily IST timetable), `functions/info.py` (AniList), `database/`
(uploaded-hash check), `core/executors.py` (download → encode → upload → post), `libs/ariawarp.py`
(aria2 torrents), `Dockerfile` (static FFmpeg).

**Licence decision:** AutoAnimeBot is GPL-3.0. Copying any of its code would make this repository a
GPL-3.0 derivative. Only design patterns were reused, reimplemented from scratch. Attribution is in
[CREDITS.md](../CREDITS.md) and [RELEASE_SYSTEM.md](RELEASE_SYSTEM.md).
**Not adopted:** SubsPlease/Nyaa sources, magnet/torrent downloads (aria2), and the `?start=<message id>` link format
(the existing opaque tokens are kept so old links work). The reference has no authorization model for these sources.

### Compatible modules → where they went
| AutoAnimeBot | ANIME SOUL (existing architecture) |
|---|---|
| `SubsPlease.feed_optimizer` + `is_anime_uploaded(sha256(title))` | `RssFeedSource`/`HttpFeedSource` + `episode_releases` (GUID/URL hash) |
| anitopy parsing, `[Batch]` skip | `matcher.parse_release_name` (group, v2, CRC, language, batch) |
| `functions/schedule.py` (APScheduler 00:30 IST) | `schedule_post.py` called by the monitor (no new dependency) |
| `core/executors.py` stages | existing `pipeline.py` state machine + new VALIDATING stage |
| Dockerfile static FFmpeg | existing Heroku FFmpeg buildpack |

### File-by-file plan (as implemented)
| File | Change |
|---|---|
| `anime_soul/matcher.py` | Release group, version, CRC tag, language tags, batch detection (numbered titles like *Kaiju No. 8 - 12* and *86 - 05* still parse) |
| `anime_soul/sources/base.py` | Adapter interface `health/find/download/validate`. `SourceItem` gains `anime_id, episode, release_id, version` plus `metadata()`. `TransferContext`, `GenericSource` |
| `anime_soul/sources/adapters.py` | Items carry structured metadata; v2 preferred; batches skipped; network errors handled; quick retries; persisted per-source exponential backoff (`retry_at`, `fail_count`, `last_success_at`) |
| `anime_soul/db.py` | New `episode_releases`, `telegram_uploads`, `pipeline_events` (+ indexes, TTL). `source_status` returns the failure count. `record_release / match_release / record_upload / get_upload / prepare_reupload / count_failure_codes / ping / event` |
| `anime_soul/models.py` | `VALIDATING` state, failure codes, `public_state()` mapping to the brief's names |
| `anime_soul/pipeline.py` | Download via `adapter.download()`, new VALIDATING step via `adapter.validate()` + sha256. Failure codes set on every error. Confirmed uploads recorded first and reused after a crash. Existing post kept on re-upload |
| `anime_soul/monitor.py` | Downtime reconciliation (AniList + feeds), release history dedupe, feed gap alert, persistent-failure alert, daily schedule hook |
| `anime_soul/schedule_post.py` (new) | Daily IST timetable, posted once per day by a MongoDB claim; in dry run it goes to admins only |
| `anime_soul/handlers/user.py` | Delivery failure → recorded and admin alert (no tokens/file IDs). TRY AGAIN handles FloodWait |
| `anime_soul/handlers/admin.py` | `/status` adds MongoDB ping, disk, public states, failure codes and feed health. New `/events`, `/checkfiles`, `/reupload`, `/schedule`. `/queue` shows failure codes |
| `anime_soul/runner.py` | MongoDB ping at start-up with a clear error (no URI in logs) |
| `anime_soul/config.py` | `RECONCILE_MAX_HOURS`, `FEED_ALERT_AFTER_FAILURES`, `SCHEDULE_POST_ENABLED/TIME`, `SCHEDULE_CHANNEL_ID` |
| `app.json`, `.env.example` | New optional variables (safe defaults). Nothing existing changed |
| `tests/test_release_system.py`, `tests/test_heroku_config.py` (new) | 47 tests |

**Dependencies:** none added. **Conflicts considered:**
- Collection names in the brief differ from production. The production collections are kept and the mapping is documented.
- `failed` stays one internal state, with `failure_code` alongside, so existing documents and `/retry` keep working.
- Old `candidate` documents without the new fields still load (defaults).
