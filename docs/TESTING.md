# Testing report and live demonstration checklist

## Update — AutoAnime.zip integration (28 Oct 2026, branch `feature/autoanime-integration`)

`python -m pytest -q` → **237 passed, 4 skipped** (mongomock). With a real MongoDB 7.0
(`REAL_MONGODB_URI=mongodb://127.0.0.1:27099`) → **241 passed**. `pyflakes anime_soul tests main.py` clean.

New `tests/test_autoanime_features.py` (24 tests):
- **bans** block `/start` links, TRY AGAIN, file sharing, `/search`, `/ongoing` and search buttons; unban restores the old link; admins can't be banned; users can't ban;
- **admins**: `/addadmin` is owner-only, an added admin passes the admin guard but can't add admins, the list survives a restart (MongoDB), env owners can't be removed;
- **/manage**: non-admins are refused (command and `mng:` callback); pause / dry run / auto-delete presets / mapped toggles saved;
- **auto-delete**: off by default; admins exempt; the minimum is enforced; queued delivery + notice deleted only when due (time-shifted sweep, i.e. after a restart); a delete error doesn't block the queue;
- `/ping`; `/restart` is admin-only, sets the stop event and the restart flag, then the message is edited once after the restart;
- `/stats` keeps the original first line and adds details; the command menu has separate public and admin scopes, a failure never raises, and help stays under 4096 characters; `START_PICS` is optional (default text unchanged);
- **/search** returns EP buttons that are the existing deep links (`?start=<token>`), unpublished episodes have no button, pagination works and the regex is escaped; `/ongoing` works;
- **mapped channels**: `/setmap` requires the bot to be admin and refuses the post/storage channel; a failed post is released for retry, one post per channel per episode, and posting can be switched off; **full pipeline**: one main post + one mapped post, no duplicates after a retry/restart; a mapped-post crash does not fail the episode;
- **HLS**: master parsing and quality pick; AES-128, SAMPLE-AES and SESSION-KEY refused; live (no ENDLIST), too many segments and non-HLS refused; a segment on a host outside the allowlist refused; size cap enforced with no output left behind; **real FFmpeg remux** of locally generated HLS segments into a ~6 s MKV with progress, temp dir cleaned; a JSON feed item with `format: hls` is marked and routed to the HLS downloader.

Two bugs were found by these tests and fixed: mapped posts read the wrong database object, and the HLS
output folder wasn't created.

Remaining integration tests (need your accounts, not run here): a real `/restart` on Heroku (re-exec in the
dyno), the command menu showing in Telegram, auto-delete on a real chat, `/setmap` with a real channel, and a
real licensed provider's HLS stream (host reachability, segment content types). No live Telegram, Heroku or
provider test was done for this branch.

## Update — Telegram provider channel source (28 Oct 2026, branch `feature/telegram-channel-source`)

`python -m pytest -q` → **213 passed, 4 skipped** (mongomock). With a real MongoDB 7.0
(`REAL_MONGODB_URI=mongodb://127.0.0.1:27099`) → **217 passed**. `pyflakes` clean.

New `tests/test_telegram_source.py` (22 tests): video/document detection; caption and `#anilist` parsing;
authorization and numeric chat ID required; `/sources add_tg` refuses missing licence, non-admin bot and the
storage channel; non-admins can't add sources; **full flow** provider post → episode queued → `send_cached_media`
to storage → one post with GET EPISODE (`?start=<token>`) → `/start` blocked until FSUB joined → delivered;
unregistered chats and non-videos ignored; too large / too short / batch rejected and never uploaded; duplicate
message, re-posted file and post-publication copies don't upload or post again; quality + v2 preference;
unmatched alert + `/assign`; rematch after `/addanime`; monitor catch-up after downtime (stops at an empty batch,
no reprocessing); catch-up failure → backoff + alert; `/sources scan` backfill; temporary upload errors retried;
permanent upload error → `UPLOAD_FAILED`, no post; dry run never uploads; progress log edits one message and
leaks no file IDs; HTML/JSON content types rejected and the partial file removed; two dynos receiving the same
post queue it once.

Not verified: a real provider channel, a real Telegram upload, or a Heroku deployment of this branch.

## Update — ongoing-release system (28 Oct 2026, branch `feature/ongoing-release-system`)

| Run | Result |
|---|---|
| `python -m pytest` (Python 3.11.17, mongomock, real ffmpeg/ffprobe) | **191 passed**, 4 skipped (real-MongoDB file) |
| Same suite against real MongoDB 7.0.2 (disposable local server) | **195 passed** |
| `pyflakes anime_soul tests` | clean |

New `tests/test_release_system.py` (31, providers and Telegram mocked, synthetic FFmpeg clips):
- **Parsing:** titles, episodes and batches, including numbered titles. Group, v2, CRC and language tags. RSS GUID release IDs.
- **Sources:** v2 preferred and batches skipped. Structured `metadata()` fields, and a round-trip with old documents.
- **Invalid URLs:** `file://` and hosts not on the allow-list are rejected.
- **Validation:** checksum and size mismatch raise `InvalidFile`.
- **Retries and backoff:** temporary errors are retried, then exponential backoff is saved and survives a restart. Recovery resets the counter. Permanent errors are not retried. Network exceptions become `SourceUnavailable`.
- **Release history:** duplicates removed across polls. Catch-up after 5 days offline without backfilling older releases. Bounds of the catch-up window. Feed-gap and repeated-failure alerts.
- **Lifecycle:** DOWNLOADING → VALIDATING → PROCESSING → UPLOADING → PUBLISHED, with `telegram_uploads` (chat/message ID, anime, episode, quality, sha256) and `pipeline_events` written. A checksum mismatch → `VALIDATION_FAILED`, nothing uploaded.
- **Failure codes:** a Telegram upload error → `UPLOAD_FAILED`, an alert and `/status` counts; never posted. A missing source → `SOURCE_NOT_CONFIGURED` with an alert. Mapping to public state names.
- **Recovery:** a saved upload record prevents a duplicate upload after a crash. A restart during VALIDATING re-downloads.
- **Delivery:** a deleted storage message → user reply, admin alert without the token, `/checkfiles` finds it, and `/reupload` keeps the same token and post (no new announcement) so the old link works again. Invalid deep links are handled. `/events` works.
- **Schedule post:** posted once per IST day, not before the configured time, admins only in dry run, off by default.

New `tests/test_heroku_config.py` (16):
- **Procfile:** process types and commands.
- **`app.json`:** buildpacks (Python + FFmpeg), formation, required secrets without defaults, `DRY_RUN` default `true`, and every variable is read by the code.
- **`.env.example`:** consistent with the code and holds placeholders only.
- **No credentials committed:** checked in the docs and config files.
- **Python and requirements:** `.python-version` 3.11, no `runtime.txt`, pinned requirements, no test packages in the runtime list.
- **Config loading:** loads with the minimum Config Vars. Bad values fail fast, and missing required variables are reported.

**Not tested here:**
- Heroku itself (no access from this environment).
- Your live bot, channels and FSUB.
- A real partner feed, because no authorized video provider is configured. A real feed and video server need a
  permitted provider and a test file you own; follow the live checklist below with `DRY_RUN=true` first.
- Telegram's real behaviour for deleted storage messages (simulated with `MessageIdInvalid`).

---

## Update — automatic airing-anime discovery (27 Oct 2026)

| Run | Result |
|---|---|
| `python -m pytest` (Python 3.11.17, mongomock, real ffmpeg) | **144 passed**, 4 skipped (real-MongoDB file) |
| Same suite against real MongoDB 7.0.2 | **148 passed** |
| `py_compile` + `pyflakes` on all modified files | clean |
| Real AniList call `discover_releasing(2)` (public API, no key) | 100 anime, all `RELEASING`, formats TV/TV_SHORT/ONA; season guesses correct |

New `tests/test_auto_discovery.py` (12, AniList mocked): query parsing and non-RELEASING skipped ·
stops when `hasNextPage` is false · page limit (2, cap 10, min 1) · new anime saved with defaults and
scheduled in the same cycle · tracked anime not duplicated or overwritten, removed anime stays removed ·
AniList discovery failure doesn't stop schedule polling or the heartbeat · disabled by
`AUTO_DISCOVER_AIRING_ANIME=false` · sequels still tracked · grouped delay alerts · discovered episode
queued with `SOURCE_NOT_CONFIGURED` and **never downloaded, uploaded or posted** without an authorized
source · `/anime` shows IST times, 🛰 marker and pages under 4000 chars; `/queue` shows upcoming (IST).

Not verified: the Heroku deployment and your live bot (no Heroku access from this environment).

---

## Automated tests — result (27 Oct 2026, branch `feature/anime-soul-full-pipeline`)

Command: `pip install -r requirements-dev.txt && python -m pytest -v`

| Run | Environment | Result |
|---|---|---|
| Mocked suite | Python 3.11.17 (Heroku target) and 3.14, mongomock-motor, real ffmpeg/ffprobe binaries | **132 passed**, 4 skipped (the real-MongoDB file) |
| Same suite against **real MongoDB 7.0.2** (`REAL_MONGODB_URI=mongodb://127.0.0.1:…`, disposable local server) | Python 3.11.17 | **136 passed**, 0 failed |
| Real AniList API call (public, no key) | `anime_info([154587])` with the new `description` field | Returned title + clean synopsis |

`pyflakes anime_soul tests`: no warnings.

### What is mocked and what is real

| Component | In the tests |
|---|---|
| Telegram (uploads, posts, deep-link delivery, FSUB membership) | **Mocked** (`FakeClient`). Handlers are also registered on a real Pyrogram `Client` object (no network). |
| MongoDB | mongomock by default; **real MongoDB 7.0** in the second run (indexes, unique keys, leases, monitor lock, state history) |
| FFmpeg / FFprobe | **Real binaries** on synthetic test-pattern clips generated during the test (no copyrighted content) |
| Authorized source feeds (RSS/JSON) and HTTP downloads | **Mocked** (feed XML/JSON in the test; download copies a local synthetic clip). The streaming downloader itself is tested against a local aiohttp server. |
| AniList / Jikan | Mocked in tests; one manual real AniList call (above) |
| Heroku | **Not tested** — no Heroku access. Startup + SIGTERM shutdown are tested locally for every ROLE. |

### Coverage by area

| Area (file, tests) | What is verified |
|---|---|
| FFmpeg processing (`test_processing.py`, 16) | ffprobe stream/duration/size parsing; H.264/AAC MKV → MP4 **remux without re-encoding**; MKV with ASS subtitles left untouched; MP4 left untouched; `copy` mode; transcode + downscale keeping subtitles; auto-transcode only above `MAX_OUTPUT_HEIGHT`; rejects no-audio, too-short, garbage, too-large files; missing ffmpeg is a transient, alerted error (or skipped when `FFMPEG_REQUIRED=false`); disk-space check; failed encode removes partial output and keeps the source; subprocess timeout and cancel kill FFmpeg |
| Feeds + full flow (`test_feeds_full_pipeline.py`, 22) | RSS 2.0 + Atom parsing; torrents/magnets ignored; XML entity bombs rejected (defusedxml); allow-list enforced; feed outage; **feed-based release detection with dedupe** and no catalogue backfill; monitor leader lock; **end-to-end: release detected → authorized file downloaded → FFprobe/remux → one storage upload (chat_id + message_id saved) → one post with GET EPISODE deep link → FSUB blocks → TRY AGAIN delivers**; second run creates no duplicate upload/post; invalid source file rejected and not uploaded; crash during processing + wiped `/tmp` → re-download and finish once; transient FFmpeg error retries without re-downloading; missing FFmpeg blocks upload and alerts; dry-run-process downloads + validates but never uploads; dry run switched on mid-job blocks posting; dry run is the default; config errors name the variable but never echo values; `/queue` + `/status` show `SOURCE_NOT_CONFIGURED`; admin `/help`; `/sources add_rss`; `/testupload publish` needs `TEST_CHANNEL_ID` or `live`; `/testupload full` runs real FFmpeg and uploads |
| Real MongoDB (`test_integration_mongodb.py`, 4, opt-in) | 20 concurrent schedule upserts → 1 episode; job claim exclusive between 3 workers and lease takeover; 10 concurrent monitor-lock attempts → exactly 1 winner, expiry takeover; full state sequence incl. `processing`; published never retried |
| Schedule (`test_schedule.py`, 10) | AniList parsing, UTC → IST, Jikan fallback, new/rescheduled episodes, delay alert, sequels, 429 retry |
| Sources & validation (`test_sources_validation.py`, 20) | Release-name parsing, matching, unauthorized sources refused, SSRF/allow-list, JSON feed, local dir + symlink escape, container/size/checksum checks |
| Captions & links (`test_captions_links.py`, 17) | Templates, escaping, unsafe templates rejected, 1024-char limit, ≤64-char deep-link tokens, **synopsis only when AniList gives one, verified quality shown, release status** |
| Pipeline (`test_pipeline.py`, 17) | State machine, leases, back-off, no duplicate upload/post after restart, ambiguous publish not re-posted, pause, FloodWait, `SOURCE_NOT_CONFIGURED` |
| FSUB, delivery, admin (`test_fsub_delivery_admin.py`, 17) | Membership states, fail-closed, join requests, TRY AGAIN, tampered tokens, legacy share links, every admin command refused for non-admins |
| Resilience, downloads, runner (11) | Lease release on shutdown, loops survive DB outages, secret redaction, atomic downloads, redirect/private-IP blocking, start + graceful SIGTERM for `all`/`bot`/`pipeline` |

### Not verified (needs your accounts)
- A real Telegram bot, channels, uploads and FSUB checks.
- A real Heroku deployment and the FFmpeg buildpack on your dyno (`/status` shows `FFmpeg: ok` once it works).
- Your MongoDB Atlas cluster (logic was checked on real MongoDB 7.0 locally).
- **A real licensed episode source.** None is configured; the bot reports `SOURCE_NOT_CONFIGURED` until you add one.

Run the real-MongoDB tests yourself against a throw-away database only:
`REAL_MONGODB_URI=mongodb://127.0.0.1:27017 python -m pytest` (drops the `test` database).

---

## Live demonstration checklist

Do this with `DRY_RUN=true` first, using a short video **you own** (e.g. a phone recording).
Use a second Telegram account (or a friend) as the "normal user".

**Startup**
- [ ] Heroku logs show `Started as @YourBot (role=all, dry_run=True)`; no `Missing required config vars`.
- [ ] `/status` (owner) shows heartbeat, monitor stats, paused = no, dry-run = yes.
- [ ] Normal user sends `/status` → no admin output.

**Original features still work**
- [ ] `/start` shows the welcome message; `/help` works.
- [ ] Send a file to the bot in private → you get a `t.me/YourBot?start=…` link → opening it returns the file.

**Schedule**
- [ ] `/addanime <name>` lists matches; `/addanime <id>` tracks it; `/pending` shows the next episode with IST time matching AniList.

**FFmpeg + sources**
- [ ] `/status` shows `FFmpeg: ok · FFprobe: ok` and `Authorized sources enabled: N`.
- [ ] With no source, an aired episode shows `SOURCE_NOT_CONFIGURED` in `/queue` (nothing is posted).
- [ ] Reply to your own video with `/testupload full` → "Full pipeline test passed" → file in storage.

**Upload test (no real episode)**
- [ ] Reply to your own video with `/testupload` → file appears in the storage channel; bot sends a test link.
- [ ] Normal user (not joined to FSUB) opens the link → sees join buttons and TRY AGAIN.
- [ ] Join the channel → TRY AGAIN → file is delivered.
- [ ] Leave the channel → link asks to join again.
- [ ] `/testupload publish` (with `TEST_CHANNEL_ID` set) → a TEST post with thumbnail, caption and **GET EPISODE** appears in the test channel; the button opens the bot and delivers the file.

**Episode flow**
- [ ] `/dryrun off`. Reply to an authorized episode file with `/attach <id> <ep>` → `/queue` shows it moving `queued → downloading/processing → uploading → published`.
- [ ] Exactly one storage message and one channel post; caption fields are correct.
- [ ] `/retry <key>` on a published episode does not create a second post.
- [ ] Restart the dyno (More → Restart all dynos) → no duplicate posts; bot answers again.

**Admin**
- [ ] `/pause` → nothing is processed; `/resume` → processing continues.
- [ ] `/broadcast` (reply to a message) reaches users who started the bot.
- [ ] Remove the bot from an FSUB channel → owner receives an alert; users are not let through.
