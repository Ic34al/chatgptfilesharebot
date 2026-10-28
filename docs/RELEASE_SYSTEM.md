# Ongoing-release system (Oct 2026)

How ANIME SOUL detects new episodes of ongoing anime, gets the file from an **authorized** source,
checks it, stores it in the private Telegram storage channel and posts it with a GET EPISODE button.
Design reference: [kaif-00z/AutoAnimeBot](https://github.com/kaif-00z/AutoAnimeBot) (GPL-3.0) — see
[CREDITS.md](../CREDITS.md). No code was copied from it; the patterns below were reimplemented inside
the existing `anime_soul/` architecture.

## Two separate jobs: metadata vs. the video file

| Job | Where it comes from | What it may do |
|---|---|---|
| **Metadata discovery** (what airs, when, which episode) | AniList GraphQL (Jikan fallback), and titles/dates in authorized RSS/Atom/JSON feeds | Create/refresh schedules and episode jobs. Never supplies a video URL. |
| **Video file** | Only a source an admin registered with `/sources` **and** a licence note (`authorized_sources` = `sources` collection), or a file an admin sent with `/attach` | Provide the file for one episode. |

A feed entry is **not** treated as permission to download or redistribute. Permission comes from the
admin who registered the source. Torrent/magnet links are ignored, URLs must be on `ALLOWED_SOURCE_HOSTS`
(HTTPS only by default, private/loopback IPs blocked), and the bot never logs in to sites, bypasses DRM or
paywalls, or searches the web for files. If no authorized source is enabled, the episode is marked
`SOURCE_NOT_CONFIGURED` and admins get an alert. Nothing fails silently.

## Source adapter interface (`anime_soul/sources/base.py`)

Every source type implements the same methods, so the monitor and pipeline never depend on one provider:

```
health()                         -> "ok" or an error
find(anime, episode)             -> SourceItem | None
download(item, destination, ctx) -> DownloadOutcome(path, sha256, size)
validate(item, path, ctx)        -> FileReport (size, container, duration) or raises InvalidFile
```

`SourceItem.metadata()` returns: `anime_id, episode, filename, expected_size, checksum (sha256),
quality, language, source, source_id, version`. Types: `telegram` inbox (`/attach`), `local_dir`,
`http_feed` (JSON), `rss_feed` (RSS 2.0 / Atom enclosures). New providers subclass `SourceAdapter`;
nothing else changes.

## Lifecycle

```
DETECTED → QUEUED → DOWNLOADING → VALIDATING → PROCESSING → UPLOADING → PUBLISHED
```
Failure codes (stored in `failure_code`, shown in `/status`, `/queue`, alerts):
`SOURCE_NOT_CONFIGURED, DOWNLOAD_FAILED, VALIDATION_FAILED, PROCESSING_FAILED, UPLOAD_FAILED, PUBLISH_FAILED`.

Internal state names are unchanged so existing MongoDB records stay valid:

| Brief | Internal `state` |
|---|---|
| DETECTED | `scheduled` |
| QUEUED | `airing` (waiting for a source) / `available` (source found) |
| DOWNLOADING | `downloading` |
| VALIDATING | `validating` (new) |
| PROCESSING | `processing` |
| UPLOADING | `uploading`, `uploaded` (stored, post pending) |
| PUBLISHED | `published` |
| failure | `failed` + `failure_code` (temporary errors retry with back-off first) |

Every job records attempts, `last_error`, `failure_code`, timestamps and a history; each change is also
written to `pipeline_events`. Jobs are claimed with MongoDB leases (two workers cannot process the
same episode). After a restart the lease expires and the job continues from its saved state. If the
temporary file disappeared (dyno restart wipes `/tmp`), it downloads again.

Files already on Telegram (`/attach`, without `PROCESS_TELEGRAM_FILES`) are copied by `file_id` and skip
DOWNLOADING/VALIDATING: there is no local file to check, and Telegram already holds it.

**Published only after Telegram confirms.** The upload must return a message with the expected media
and size. That is recorded in `telegram_uploads` *before* the state moves on. The post must return a
message ID. Only then is the episode `PUBLISHED`. An unclear post result (timeout) is never retried
automatically; the admin checks the channel first (`/retry <key> publish`).

## MongoDB collections

| Brief | Collection | Unique index / purpose |
|---|---|---|
| anime_schedule | `anime` + `episodes.airing_at` | `anime._id` = AniList ID |
| episode_jobs | `episodes` | `_id` = `anilist:<id>:<ep>`, unique `token`; leases, history |
| episode_releases | `episode_releases` (new) | `_id` = release id (feed GUID/URL hash); `matched_key` |
| authorized_sources | `sources` | `_id` = name; licence note, health, `fail_count`, `retry_at`, `last_success_at` |
| telegram_uploads | `telegram_uploads` (new) | unique `episode_key`; storage chat/message ID, file_id, quality, sha256, status |
| pipeline_events | `pipeline_events` (new) | TTL 60 days; state changes and errors (no secrets) |
| — | `posts` | unique `episode_key`: one announcement per episode |

The existing collections were not renamed. Renaming would orphan production data and break old links.

## Release monitoring

* Polls AniList every `POLL_INTERVAL_SECONDS` (default 600 s). Optional auto-discovery of airing TV/ONA.
* Parses release names: title, season, episode, quality, version (`v2`), release group, CRC tag,
  language tags (Dual Audio, English Sub/Dub, Hindi…). **Batches** (`01-12`, `[Batch]`, `Complete`) are
  never treated as a single episode. Titles are matched against romaji/English/native/synonyms with
  normalisation.
* Every feed release is recorded once in `episode_releases`. A release already matched to an episode is
  skipped, so nothing is published twice. A v2 re-release is preferred for an episode that is not yet
  published. A published episode is never re-announced.
* **Temporary feed errors** (timeouts, 5xx, 429, connection resets) are retried twice within a check
  (0.5 s, 1 s). After that the feed **backs off exponentially** (60 s → 2 → 4 … up to 1 h). The backoff is saved in
  MongoDB, so it survives restarts. After `FEED_ALERT_AFTER_FAILURES` failures in a row, admins get an alert.
  Permanent errors (host not allow-listed, bad XML) are not retried quickly.
* **Reconciliation after downtime**: the look-back window extends to the last successful check (for AniList
  and for each feed), capped at `RECONCILE_MAX_HOURS` (default 7 days, so it never backfills a whole
  catalogue). A feed lists only its newest entries. If the oldest entry is newer than the last successful
  check, admins are told that releases may be missing, rather than the bot silently skipping them.
* Times are stored in UTC and shown in **Asia/Kolkata**. `/status` shows each feed's last success,
  failures and backoff.

## Delivery robustness

* GET EPISODE keeps the existing `?start=<token>` format. Old links keep working.
* Force-subscribe, join requests, TRY AGAIN, admin commands and access controls are unchanged.
* If a stored file can't be delivered (deleted storage message, inaccessible file), the user gets a polite
  message, and the upload is marked `delivery_failed`. Admins get one alert per episode. The alert never
  contains tokens or file IDs.
  `/checkfiles` checks recent storage messages. `/reupload <key>` gets the file again, keeps the **same post
  and link**, and doesn't announce it again.
* Rate limits (FloodWait) are waited out. Invalid or overlong deep links get a clear reply.

## Daily schedule post (optional)

Pattern from AutoAnimeBot's daily timetable: with `SCHEDULE_POST_ENABLED=true` the monitor posts today's
airing list (IST) once per day after `SCHEDULE_POST_TIME` (default 00:30) to `SCHEDULE_CHANNEL_ID`
(default the post channel). A MongoDB claim makes sure it is posted once per day. In dry run it goes to
admins only. `/schedule` previews it.

## What was taken from AutoAnimeBot (patterns only)

| AutoAnimeBot | Here |
|---|---|
| SubsPlease RSS polling + `is_anime_uploaded` hash check | Generic authorized RSS/Atom/JSON adapters + `episode_releases` history (SubsPlease and torrents **not** used) |
| `anitopy` title parsing, skip `[Batch]` | Own parser with group/version/CRC/language/batch detection (no new dependency) |
| APScheduler daily IST schedule post | `schedule_post.py`, once-per-day MongoDB claim |
| `?start=<id>` deep link to the stored message | Existing opaque `?start=<token>` (kept for backward compatibility) |
| aria2 torrent downloader | Not adopted (no authorization model) |
| Static FFmpeg in Docker | Heroku FFmpeg buildpack (already in `app.json`) |

## Provider Telegram channel — automatic file intake (`anime_soul/sources/telegram_channel.py`)

This is ANIME SOUL's version of AutoAnimeBot's automatic flow (**new release → get file → upload to storage → post
with button**). AutoAnimeBot gets files by reading the SubsPlease RSS feed and downloading torrents with aria2.
That source gives no permission to redistribute, so here the release feed **and** the file host is a Telegram
channel where a licensed provider uploads episodes.

```
provider posts video in intake channel ──► handlers/source_ingest.py (live, bot dyno)
          │ (missed while offline)            │
          └──► monitor._provider_channels ────┤  catch_up(): get_messages by ID after last_message_id
                                              ▼
            TelegramChannelSource.ingest(): video type / size / duration / batch check
            → parse file name + caption (title, S/E, quality, v2, #anilist tag)
            → telegram_source_files (unique per source + file_unique_id = no duplicates)
            → match tracked anime → upsert episode (AIRING, woken now) + episode_releases
                                              ▼
            existing pipeline: find() → SourceItem(kind="telegram", ref=file_id)
            → UPLOADING: send_cached_media to STORAGE_CHANNEL_ID (no re-download;
              PROCESS_TELEGRAM_FILES=true downloads + FFprobe/FFmpeg first)
            → telegram_uploads record → publish to POST_CHANNEL_ID with GET EPISODE (?start=<token>)
            → /start <token>: FSUB check → direct delivery (unchanged)
```

* Collection **`telegram_source_files`**: `_id` = `"<chat_id>:<message_id>"`, `source`, `file_id`,
  `file_unique_id` (unique per source), `file_name`, `file_size`, `mime_type`, `duration`, `width/height`,
  parsed `title/episode/season/quality/language/version/is_batch/anilist_id`, `status` (`ok`, `unmatched`,
  `rejected`), `reject_reason`, `matched_key`, `posted_at`. The source document stores `last_message_id`
  and `last_file_at`.
* Choosing between several files for one episode: the tracked quality first, then the highest version (v2), then
  the highest resolution, then the newest post. After an episode is published, later copies are ignored (the
  episode state and the `telegram_uploads` record block a second upload or post).
* Rejected (never uploaded): non-video documents, files above `MAX_FILE_SIZE_MB` / below `MIN_FILE_SIZE_MB`,
  videos shorter than `MIN_DURATION_SECONDS`, batch releases. Unmatched files are kept, the owner is alerted
  once, they are retried every monitor cycle (e.g. after `/addanime`), and `/assign` fixes them by hand.
* Errors: a catch-up failure (bot removed as admin, etc.) uses the same source backoff as feeds
  (60 s doubling to 1 h) and alerts after `FEED_ALERT_AFTER_FAILURES`. Upload errors use the existing retry
  and `UPLOAD_FAILED` handling; nothing is posted until Telegram confirms the storage upload.
* Duplicate processing on Heroku: live posts are only handled by the dyno that runs the bot handlers
  (`worker`); if two dynos ever receive the same post, the unique `_id` lets only one queue it, and the
  episode lease (`claim_next`) lets only one pipeline upload it.
* Limits: Telegram bots can't read channel history, only messages by ID, so catch-up reads ID ranges after the
  last seen post and stops at the first empty batch of 100 (the `/sources scan` command tolerates 3 empty batches).
  Bots can download files up to 20 MB with the Bot API, but this bot uses MTProto (Pyrogram), so
  `PROCESS_TELEGRAM_FILES=true` works up to Telegram's 2 GB (4 GB Premium) file limit — on a 0.5 GB-RAM
  Heroku dyno keep it off unless you need FFmpeg.

## Optional progress log (`anime_soul/reporter.py`)

Adapted from AutoAnimeBot's `Reporter`: with `LOG_CHANNEL_ID` set, each job gets one message in that channel
that is edited as the state changes (File found → Downloading → Validating → Processing → Uploading → DONE / Failed
with the error). The message ID is saved on the episode, so edits continue after a restart. It never shows
file IDs, tokens or credentials. Off by default.

## AutoAnimeBot integration map (kaif-00z/AutoAnimeBot @47caf18, GPL-3.0)

| AutoAnimeBot file / function | What it does there | ANIME SOUL | Status |
|---|---|---|---|
| `libs/subsplease.py` `SubsPlease.feed_optimizer/on_new_anime` | Polls 3 SubsPlease RSS feeds every 5 s, anitopy parse, skip `[Batch]`, sha256 uid | `monitor.py` + `sources/adapters.py` (authorized RSS/JSON), `sources/telegram_channel.py` (Telegram provider) | Adapted (idea), SubsPlease not used |
| `database` `is_anime_uploaded` / `store_items` | Hash-based "already done" check | `episode_releases`, `telegram_source_files` unique index, `telegram_uploads`, episode state | Adapted (idea) |
| `libs/ariawarp.py` `Torrent.download_magnet` | aria2c magnet/torrent download | — | **Not adopted**: torrents give no redistribution right, aria2 isn't on Heroku, and it skips the host/size checks |
| `bot.py` `anime()` | feed → info → torrent → executor → post with button | `pipeline.py` state machine (existing) | Already existed; new source plugs into it |
| `core/executors.py` `Executors.execute` | rename or FFmpeg encode, upload to backup channel, `?start=<msg.id>` button | `pipeline.py`, `processing.py`, `uploader.py`, `publisher.py` | Existing; deep-link format **kept** as `?start=<token>` |
| `core/bot.py` `upload_anime` | Pyrogram `send_document` to a channel | `uploader.upload_episode` (`send_cached_media` for Telegram files) | Existing |
| `libs/logger.py` `Reporter` | Edits one log message per job | `reporter.py` `JobReporter` | Adapted (rewritten) |
| `functions/schedule.py` | Daily IST timetable | `schedule_post.py` | Adapted earlier (PR #3) |
| `functions/info.py` `AnimeInfo` | AniList metadata | `schedule/anilist.py` (existing) | Already existed |
| Multi-quality 480/720/1080 buttons, screenshots/sample clip | Extra post buttons / media | — | Not adopted: would change the post layout and episode schema |
| Telethon client | Bot framework | Pyrogram (existing) | Not adopted |

**Copied verbatim: nothing.** Adapted: the automatic flow, duplicate checks, batch skipping, job progress reporter,
daily timetable. New: the Telegram provider source, `/sources add_tg|scan`, `/unmatched`, `/assign`, the
download content-type check.
