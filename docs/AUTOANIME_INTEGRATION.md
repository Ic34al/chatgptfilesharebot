# AutoAnime.zip integration (28 Oct 2026, branch `feature/autoanime-integration`)

The uploaded `AutoAnime.zip` contains 49 files: a kurigram bot (GPL-3.0 `LICENSE`) that gets its episodes by
collecting streams from unlicensed websites and downloading them with the N_m3u8DL-RE binary. ANIME SOUL keeps
its own architecture (`anime_soul/`), and the zip's bot features that work with authorized sources were
re-implemented inside it. The scrapers, proxy/Tor rotation and the stream-ripping binary were not used.
The reasons are listed below.

**No code was copied from the zip.** Every adapted feature was written fresh for this codebase, so the
zip's GPL-3.0 terms do not apply to this repository. If code is ever copied from it, the combined project
must be released under GPL-3.0 with its licence text and copyright notices.

## What was added

| Feature (zip file) | ANIME SOUL implementation | Who can use it |
|---|---|---|
| Ban / unban / banned list (`callbacks/admin.py`, `decorators.py check_ban`) | `/ban <id> [reason]`, `/unban`, `/banned`. A ban blocks `/start` links, TRY AGAIN, file sharing, `/search` and `/ongoing`. Admins can't be banned. Collection `banned_users`. | admins |
| Extra admins (`admin_cmds.py add_admin/rm_admin/admins`) | `/addadmin`, `/rmadmin` (owner only: `OWNER_ID`/`ADMIN_IDS`), `/admins`. Collection `bot_admins`, reloaded at startup. Env owners can't be removed from the bot. | owner / admins |
| User count (`admin_cmds.py users`) | `/users` (total, reachable, blocked, banned) | admins |
| Stats panel with psutil (`callbacks/admin.py cb_stats`) | `/stats`: the original first line `📊 Stored files: N`, then users, admins, episodes published/failed, MongoDB size, load, RAM, disk. Uses the standard library only (no psutil). | admins |
| Auto-delete delivered files (`download.py` `asyncio.sleep` + delete, `/autodel`) | `/autodel <seconds>` (0 = off, 60–604800). Queued in MongoDB (`autodelete_queue`) and removed by a 15-second sweeper, so it survives restarts, unlike the zip's in-memory sleep. Admins are exempt. Off by default. | admins |
| `/ping`, `/restart` (`commands.py`) | `/ping` (Telegram + MongoDB latency, public). `/restart` stops jobs gracefully (leases released), re-executes the process and edits the message to "restarted". | public / admins |
| Settings panel (`/manage`, `callbacks/settings.py`) | `/manage` inline panel: pause/resume uploads, dry run on/off, auto-delete presets, mapped posts on/off. Callback `mng:` is admin-checked. | admins |
| Mapped channels (`admin_cmds.py setmap/unmap/maplist`, `pages.py`) | `/setmap <channel> <anilist id>` (the bot must be admin there), `/unmap`, `/maplist`. After the main post, each mapped channel gets the same post with the same GET EPISODE deep link, once per episode. A failure there never fails the episode. | admins |
| Schedule (`/ongoing`, `/schedule` scraped from aniwatchtv) | `/ongoing` shows today's timetable from AniList (the existing `schedule_post` builder) | public |
| Search (`plugins/search.py`, `callbacks/anime.py` episode pages) | `/search <name>` searches ANIME SOUL's own published library and shows EP buttons that are the existing deep links (`?start=<token>`), 24 per page. It does not search other sites. | public |
| Bot command menu (`telegram/utils.py set_bot_commands`) | Set at startup: public menu for everyone, plus admin entries for each admin's chat | — |
| Random start picture (`core/images.py`, `Script.py`) | Optional `START_PICS` (comma-separated https URLs). If it's empty, the welcome text is unchanged. | — |
| HLS download (`binary/N_m3u8DL-RE`) | `anime_soul/hls.py`, for authorized providers only. See below. | — |
| Progress bar (`download.py`) | Already covered by `reporter.py` (LOG_CHANNEL_ID) from PR #4 | — |
| Caption/file name format (`core/anilist.py CAPTION_FORMAT`) | Already covered by `captions.py` / `naming.py` | — |
| FSUB + join requests (`helpers.py`, `callbacks/fsub.py`) | Already present (env `FSUB_CHANNELS`, join-request mode, TRY AGAIN). Unchanged. | — |
| Broadcast (`callbacks/admin.py cb_broadcast_prompt`) | Already present (`/broadcast` as a reply). Unchanged. | — |

### HLS from authorized providers (`anime_soul/hls.py`)

Some licensed providers deliver episodes as HLS (`.m3u8`) instead of one file. A JSON feed item may now say
`"format": "hls"`, or its URL may end in `.m3u8`. The pipeline then:

1. checks every playlist and segment URL against `ALLOWED_SOURCE_HOSTS` (public IPs only, redirects re-checked);
2. **refuses encrypted/DRM streams**: any `#EXT-X-KEY` or `#EXT-X-SESSION-KEY` with a METHOD other than
   NONE stops the download with a clear error. Nothing is ever decrypted;
3. refuses unfinished live playlists (no `#EXT-X-ENDLIST`) and retries later. It also caps the segment
   count (5000) and the total size (`MAX_FILE_SIZE_MB`);
4. picks the variant for the requested quality, downloads the segments one at a time, then remuxes them
   locally with FFmpeg (`-protocol_whitelist file`, so FFmpeg never touches the network) into one `.mkv`;
5. continues with the usual validation, storage upload, file-ID storage and duplicate-safe posting.

## Not adopted, and why

| Zip file(s) | Reason |
|---|---|
| `anime/scraper/base.py`, `anikoto.py`, `animesalt.py`, `animetsu.py`, `cantarellatv.py`, `search.py`, `__init__.py` | They collect streams from unlicensed sites (aniwatchtv.to, anikototv.to, animesalt.to, megaplay.buzz, …). The master prompt forbids unauthorized sources. |
| `telegram/ongoing.py` | The release loop is built around those scrapers. ANIME SOUL already has its own monitor (AniList + authorized sources), so a second loop would duplicate it. |
| `telegram/download.py` (download part), `callbacks/download.py`, `callbacks/anime.py` (range/quality download) | On-demand downloads from the scrapers. Delivery here always comes from the storage channel. |
| `binary/N_m3u8DL-RE`, `binary/N_m3u8DL-RE.exe`, `Dockerfile` (downloads it), `run.sh` | A stream-ripping binary used together with the scrapers. Replaced by `hls.py`, which refuses DRM. |
| `core/proxy.py`, `proxies.txt`, `Aptfile` (`tor`) | IP rotation through proxies and Tor, used to get around site blocks |
| `requirements.txt` extras: `curl_cffi`, `beautifulsoup4`, `pycryptodome`, `requests`, `guessit`, `psutil`, `kurigram` | Used only by the excluded parts, or replaced by existing code or the standard library. Pyrogram 2.0.106 stays (kurigram `ButtonStyle` is not needed). |
| `Procfile` (`web: bash run.sh`), `runtime.txt` | ANIME SOUL runs as Heroku `worker` / `uploader` dynos. Kept unchanged. |
| `callbacks/fsub.py` dynamic FSUB channels (`addchnl`/`delchnl`) | FSUB channels stay in env vars (`FSUB_CHANNELS`) so existing production config isn't changed |
| Favorites (`commands.py favorites`, `callbacks/anime.py`) | Tied to the scraper search catalogue |
| `config.py` | It has bugs (`int("")` crash when `OWNER_ID` is empty, plugins root named "cantarella"). ANIME SOUL's validated `anime_soul/config.py` is kept. |
| `core/database.py`, `core/state.py`, `core/utils.py`, `button.py`, `callbacks/helpers.py`, `callbacks/start.py`, `pages.py`, `__main__.py`, `__init__.py` files, `.gitignore`, `README.md`, `LICENSE`, `Script.py` (texts) | Equivalents already exist in ANIME SOUL (db.py, models.py, user.py, publisher.py, runner.py). The ideas used are listed in the table above. |

## Inventory of all 49 files

`.gitignore`, `Aptfile`, `Dockerfile`, `LICENSE`, `Procfile`, `README.md`, `config.py`, `proxies.txt`,
`requirements.txt`, `run.sh`, `runtime.txt`, `Script.py`, `binary/N_m3u8DL-RE`, `binary/N_m3u8DL-RE.exe`,
`anime/__init__.py`, `anime/__main__.py`, `anime/button.py`,
`anime/core/{__init__,anilist,database,images,proxy,state,utils}.py`,
`anime/scraper/{__init__,anikoto,animesalt,animetsu,base,cantarellatv,search}.py`,
`anime/telegram/{__init__,decorators,download,ongoing,pages,utils}.py`,
`anime/telegram/plugins/{__init__,admin_cmds,commands,search}.py`,
`anime/telegram/plugins/callbacks/{__init__,admin,anime,download,fsub,helpers,settings,start}.py`.
Each one appears in one of the two tables above.

## Files changed in ANIME SOUL

* new: `anime_soul/hls.py`, `anime_soul/handlers/extras.py`, `tests/test_autoanime_features.py`, this document
* `anime_soul/db.py`: collections `banned_users`, `bot_admins`, `autodelete_queue`, `mapped_channels`,
  `mapped_posts` (new collections only; existing records untouched)
* `anime_soul/context.py`: `extra_admins`, `is_owner()`, `stop_event`, `restart_requested`
* `anime_soul/handlers/user.py`: ban check and auto-delete hook. Deep-link decoding, FSUB and TRY AGAIN are unchanged.
* `anime_soul/handlers/admin.py`: extended `/stats`, help text
* `anime_soul/pipeline.py`: mapped-channel posts after a successful publish
* `anime_soul/runner.py`: registers the extras, loads admins, auto-delete sweeper, command menu, `/restart`
* `anime_soul/sources/base.py`, `sources/adapters.py`: HLS routing for feed items
* `anime_soul/config.py`, `.env.example`: optional `START_PICS`
* `requirements.txt`, `Procfile`, `app.json`: **unchanged**
