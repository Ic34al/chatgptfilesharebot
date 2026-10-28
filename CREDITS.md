# Credits and licences

* **Original bot**: Fast Telegram File Share Bot (Pyrogram + MongoDB) — the base of this repository.
* **Design reference**: [AutoAnimeBot](https://github.com/kaif-00z/AutoAnimeBot) by kAiF_00z
  ([GitHub](https://github.com/kaif-00z), [Telegram](https://t.me/kAiF_00z)), licensed **GPL-3.0**.
  Patterns that informed ANIME SOUL: feed release hash check (duplicate detection), skipping batch
  releases, title parsing, the daily IST airing timetable, deep-link delivery of stored files, the
  automatic "new release → file → storage upload → post with button" flow (used for the Telegram
  provider channel source) and the per-job progress log message (`Reporter` → `anime_soul/reporter.py`).
  The full file-by-file map is in docs/RELEASE_SYSTEM.md.
  **No source code was copied.** Everything here was written independently for the `anime_soul/`
  architecture, so GPL-3.0 obligations (licence notice, source availability) are not triggered.
  If you ever copy code from AutoAnimeBot into this repository, the combined work must be released
  under GPL-3.0 with its licence text and copyright headers kept.
* **AutoAnime.zip** (uploaded by the owner, kurigram bot, GPL-3.0 `LICENSE`): bot features re-implemented
  without copying code — bans, extra admins, auto-delete, /ping, /restart, /manage, mapped channels,
  /ongoing, library /search, command menu, random start picture. Its scrapers of unlicensed sites,
  proxy/Tor rotation and the N_m3u8DL-RE binary were **not** used. Full map: docs/AUTOANIME_INTEGRATION.md.
* Earlier reference: [MatizTech/Auto-Anime-Bot](https://github.com/MatizTech/Auto-Anime-Bot)
  (pipeline ideas only, no code copied).
* Libraries: Pyrogram (LGPL-3.0), Motor/PyMongo (Apache-2.0), aiohttp (Apache-2.0), defusedxml (PSF),
  FFmpeg (LGPL/GPL, installed by the Heroku buildpack, not bundled). Schedule data: AniList API, Jikan API.

## Licences and video files

The AutoAnimeBot software licence (GPL-3.0) covers its code only. It gives no right to download or
redistribute anime episodes. Every source added with `/sources` needs a licence/permission note, and you are
responsible for having that permission from the rights holder or a licensed provider.

If you ever copy AutoAnimeBot code into this repository, check before publishing: the combined project would
need a GPL-3.0 `LICENSE` file, the original copyright notices, and source availability for everyone you
distribute the bot to. This repository currently has no licence file, so nothing from AutoAnimeBot was copied.
