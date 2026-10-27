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
