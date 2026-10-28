# Credits and licences

* **Original bot**: Fast Telegram File Share Bot (Pyrogram + MongoDB) — the base of this repository.
* **Design reference**: [AutoAnimeBot](https://github.com/kaif-00z/AutoAnimeBot) by kAiF_00z
  ([GitHub](https://github.com/kaif-00z), [Telegram](https://t.me/kAiF_00z)), licensed **GPL-3.0**.
  Patterns that informed ANIME SOUL: feed release hash check (duplicate detection), skipping batch
  releases, title parsing, the daily IST airing timetable, deep-link delivery of stored files.
  **No source code was copied.** Everything here was written independently for the `anime_soul/`
  architecture, so GPL-3.0 obligations (licence notice, source availability) are not triggered.
  If you ever copy code from AutoAnimeBot into this repository, the combined work must be released
  under GPL-3.0 with its licence text and copyright headers kept.
* Earlier reference: [MatizTech/Auto-Anime-Bot](https://github.com/MatizTech/Auto-Anime-Bot)
  (pipeline ideas only, no code copied).
* Libraries: Pyrogram (LGPL-3.0), Motor/PyMongo (Apache-2.0), aiohttp (Apache-2.0), defusedxml (PSF),
  FFmpeg (LGPL/GPL, installed by the Heroku buildpack, not bundled). Schedule data: AniList API, Jikan API.
