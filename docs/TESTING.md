# Testing report and live demonstration checklist

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
