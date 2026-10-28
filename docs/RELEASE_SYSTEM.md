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
