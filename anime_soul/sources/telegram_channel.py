"""Telegram provider channel: an authorized provider uploads episode files to a Telegram channel
(or group), and ANIME SOUL picks them up automatically.

This adapts the automatic "new release → file → upload → post" flow of kaif-00z/AutoAnimeBot
(libs/subsplease.py + bot.py `anime()`; GPL-3.0) to a source that can actually be authorized. AutoAnimeBot
reads SubsPlease RSS and downloads torrents with aria2. Here the provider's own Telegram channel is the
release feed **and** the file host. Independent implementation for Pyrogram; no code copied (see CREDITS.md).

How it works
* An admin registers the channel with a licence note: ``/sources add_tg <name> <chat_id> | <licence>``.
  The bot must be an administrator there (Telegram only delivers channel posts to admin bots).
* Every new video/document post in that chat is checked (video type, size, duration). It is parsed from
  the file name or caption (title, season, episode, quality, language, v2, batch), matched to a tracked anime,
  and stored in ``source_files``. The matched episode is created/woken immediately.
* Files are de-duplicated by Telegram's ``file_unique_id`` (a re-post of the same file is ignored).
* After downtime, ``catch_up()`` (called by the existing release monitor, not a new loop) reads the message
  IDs after the last one seen. Telegram lets bots fetch messages by ID, not channel history.
* Files that can't be matched are kept as ``unmatched``, and admins are told once: ``/assign`` fixes them.
* The pipeline then copies the file to the storage channel by ``file_id`` (no re-download). With
  ``PROCESS_TELEGRAM_FILES=true`` it downloads, validates (FFprobe) and processes it first.
The bot never joins channels on its own, uses no user account/session, and doesn't read channels it isn't an admin of.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from pymongo.errors import DuplicateKeyError

from .. import models as M
from ..matcher import VIDEO_EXTS, matches_episode, parse_release_name
from ..timeutil import ensure_utc, utcnow
from .base import SourceAdapter, SourceItem, make_release_id

log = logging.getLogger(__name__)

VIDEO_MIME = re.compile(r"^(video/|application/(x-matroska|mp4|octet-stream)$)")
_ANILIST_TAG = re.compile(r"(?i)(?:#?anilist[\s:_#-]*(?:id)?[\s:#-]*)(\d{2,7})")
_EP_TAG = re.compile(r"(?i)\b(?:episode|ep|e)[\s.:#-]*(\d{1,4})\b")
_QUALITY_RANK = {"2160p": 6, "1440p": 5, "1080p": 4, "720p": 3, "576p": 2, "480p": 1, "360p": 0}
SCAN_BATCH = 100          # Telegram returns up to 200 messages per get_messages call
SCAN_MAX_EMPTY_BATCHES = 1


def chat_key(chat_id: int, message_id: int) -> str:
    return f"{int(chat_id)}:{int(message_id)}"


def extract_media(msg) -> Optional[Dict[str, Any]]:
    """Video or video-like document from a Pyrogram message, else None."""
    media, kind = None, None
    if getattr(msg, "video", None):
        media, kind = msg.video, "video"
    elif getattr(msg, "document", None):
        media, kind = msg.document, "document"
    if media is None:
        return None
    name = getattr(media, "file_name", None) or ""
    mime = (getattr(media, "mime_type", None) or "").lower()
    ext = os.path.splitext(name)[1].lower()
    if kind == "document" and not (VIDEO_MIME.match(mime) and (ext in VIDEO_EXTS or mime.startswith("video/"))):
        return None
    return {"media_type": kind, "file_id": media.file_id, "file_unique_id": media.file_unique_id,
            "file_size": getattr(media, "file_size", None), "file_name": name, "mime_type": mime,
            "duration": getattr(media, "duration", None), "width": getattr(media, "width", None),
            "height": getattr(media, "height", None)}


def parse_post(media: Dict[str, Any], caption: str) -> Dict[str, Any]:
    """Release metadata from the file name, falling back to / enriched by the caption."""
    caption = caption or ""
    first_line = next((ln.strip() for ln in caption.splitlines() if ln.strip()), "")
    p = parse_release_name(media.get("file_name") or first_line)
    if p.episode is None and first_line:
        p2 = parse_release_name(first_line)
        if p2.episode is not None:
            p = p2
    out = {"title": p.title, "episode": p.episode, "season": p.season, "quality": p.quality,
           "language": p.language, "version": p.version, "is_batch": p.is_batch, "group": p.group}
    tag = _ANILIST_TAG.search(caption)
    out["anilist_id"] = int(tag.group(1)) if tag else None
    if out["episode"] is None and not p.is_batch:
        e = _EP_TAG.search(caption)
        if e:
            out["episode"] = int(e.group(1))
    if not out["quality"] and media.get("height"):
        out["quality"] = f"{int(media['height'])}p"
    if not out["language"]:
        out["language"] = parse_release_name(caption).language
    return out


def check_media(media: Dict[str, Any], cfg) -> Optional[str]:
    """Reason to reject, or None. Telegram's own metadata (duration, size, type)."""
    size = media.get("file_size") or 0
    if size and size > cfg.max_file_size_bytes:
        return f"file too large ({size} bytes)"
    if size and size < cfg.min_file_size_bytes:
        return f"file too small ({size} bytes)"
    dur = media.get("duration")
    if dur and dur < cfg.min_duration_seconds:
        return f"video too short ({dur}s)"
    return None


class TelegramChannelSource(SourceAdapter):
    type = "telegram_channel"

    def __init__(self, name: str, config: Dict[str, Any], db=None, cfg=None):
        super().__init__(name, config)
        try:
            self.chat_id = int(config.get("chat_id"))
        except (TypeError, ValueError):
            raise ValueError("telegram_channel needs a numeric chat_id (e.g. -100…)")
        self.db = db
        self.cfg = cfg

    async def find(self, anime: dict, episode: int) -> Optional[SourceItem]:
        if self.db is None:
            return None
        key = M.episode_key(anime["_id"], episode)
        docs = await self.db.source_files.find({"source": self.name, "matched_key": key, "status": "ok"}) \
            .to_list(length=20)
        if not docs:
            return None
        want = (anime.get("quality") or "").lower()

        def rank(d):
            q = (d.get("quality") or "").lower()
            return (q == want if want else False, d.get("version") or 1, _QUALITY_RANK.get(q, -1),
                    d.get("message_id") or 0)
        best = max(docs, key=rank)
        return SourceItem(self.name, "telegram", best["file_id"], best.get("file_name") or f"episode{episode}.mkv",
                          size=best.get("file_size"), quality=best.get("quality"), language=best.get("language"),
                          owned=False, anime_id=int(anime["_id"]), episode=int(episode),
                          release_id=make_release_id(self.name, best["file_unique_id"]), version=best.get("version") or 1,
                          extra={"chat_id": best["chat_id"], "message_id": best["message_id"],
                                 "media_type": best.get("media_type")})

    async def health(self) -> str:
        if self.db is None:
            return "ok"
        doc = await self.db.sources.find_one({"_id": self.name}) or {}
        n = await self.db.source_files.count_documents({"source": self.name})
        last = doc.get("last_file_at")
        return f"ok · {n} file(s) · last file {ensure_utc(last):%Y-%m-%d %H:%M} UTC" if last else f"ok · {n} file(s)"

    # ---------------------------------------------------------------- ingest
    async def ingest(self, msg, notifier=None) -> str:
        """Process one message from the provider chat. Returns new|duplicate|unmatched|rejected|ignored."""
        media = extract_media(msg)
        if media is None:
            return "ignored"
        db, cfg = self.db, self.cfg
        mid = int(msg.id)
        caption = getattr(msg, "caption", None) or ""
        meta = parse_post(media, caption)
        posted = getattr(msg, "date", None)
        posted = ensure_utc(posted) if posted else utcnow()
        reason = check_media(media, cfg) or ("batch release (not a single episode)" if meta["is_batch"] else None)
        doc = {"_id": chat_key(self.chat_id, mid), "source": self.name, "chat_id": self.chat_id, "message_id": mid,
               **media, **meta, "caption": caption[:1000], "posted_at": posted, "received_at": utcnow(),
               "matched_key": None, "status": "rejected" if reason else "unmatched", "reject_reason": reason}
        try:
            await db.source_files.insert_one(doc)
        except DuplicateKeyError:
            await self._bump_last_id(mid)
            return "duplicate"          # same message again, or the same file_unique_id re-posted
        await self._bump_last_id(mid, file_at=posted)
        if reason:
            log.info("Provider %s message %s rejected: %s", self.name, mid, reason)
            return "rejected"
        key = await self.match_and_queue(doc)
        if key:
            return "new"
        if notifier is not None:
            await notifier.alert(
                f"📥 Provider <b>{_esc(self.name)}</b> posted a file I couldn't match to a tracked anime:\n"
                f"<code>{_esc(media.get('file_name') or caption[:80] or 'no name')}</code>\n"
                f"Fix: /assign {doc['_id']} &lt;anilist_id&gt; &lt;episode&gt; (or /addanime the show).",
                key=f"unmatched:{doc['_id']}")
        return "unmatched"

    async def match_and_queue(self, doc: dict, anime_list: Optional[List[dict]] = None) -> Optional[str]:
        """Match a stored file to an episode; create/wake that episode. Returns the episode key."""
        if doc.get("episode") is None:
            return None
        anime_list = anime_list if anime_list is not None else await self.db.list_anime(active_only=True)
        for a in anime_list:
            ok = matches_episode(a, int(doc["episode"]), title=doc.get("title"), anime_id=doc.get("anilist_id"),
                                 ep=doc.get("episode"), quality=None, language=None, season=doc.get("season"))
            if ok:
                return await self.assign(doc["_id"], int(a["_id"]), int(doc["episode"]))
        return None

    async def assign(self, file_key: str, anime_id: int, episode: int) -> str:
        db = self.db
        key = M.episode_key(anime_id, episode)
        doc = await db.source_files.find_one_and_update(
            {"_id": file_key}, {"$set": {"matched_key": key, "status": "ok", "reject_reason": None,
                                         "anilist_id": anime_id, "episode": episode}})
        posted = (doc or {}).get("posted_at") or utcnow()
        await db.upsert_schedule(anime_id, episode, min(ensure_utc(posted), utcnow()), f"tg:{self.name}",
                                 estimated=True)
        await db.mark_aired()
        # wake an episode that was waiting for a source (backoff) so it is picked up now
        await db.episodes.update_one({"_id": key, "state": M.AIRING}, {"$set": {"next_attempt_at": utcnow()}})
        rid = make_release_id(self.name, (doc or {}).get("file_unique_id") or file_key)
        await db.record_release(rid, self.name, {**(doc or {}), "guid": file_key}, posted)
        await db.match_release(rid, key)
        await db.event(key, "provider_file", source=self.name, file=file_key)
        log.info("Provider %s file %s → %s", self.name, file_key, key)
        return key

    async def rematch_unmatched(self, limit: int = 200) -> int:
        """Retry matching recent unmatched files (e.g. after /addanime). Returns how many matched."""
        anime = await self.db.list_anime(active_only=True)
        n = 0
        async for d in self.db.source_files.find({"source": self.name, "status": "unmatched"}).sort(
                "message_id", -1).limit(limit):
            if await self.match_and_queue(d, anime):
                n += 1
        return n

    async def catch_up(self, client, notifier=None, max_messages: int = 1000,
                       max_empty_batches: int = SCAN_MAX_EMPTY_BATCHES) -> Dict[str, int]:
        """Read messages posted while the bot was offline (by ID, after the last one seen)."""
        src = await self.db.sources.find_one({"_id": self.name}) or {}
        last = src.get("last_message_id")
        stats = {"scanned": 0, "new": 0}
        if last is None:
            return stats            # nothing seen yet: the first live post (or /sources scan) sets the start
        nxt, empty_batches = int(last) + 1, 0
        while stats["scanned"] < max_messages:
            ids = list(range(nxt, nxt + SCAN_BATCH))
            msgs = await client.get_messages(self.chat_id, ids)
            msgs = msgs if isinstance(msgs, list) else [msgs]
            real = [m for m in msgs if m is not None and not getattr(m, "empty", False)]
            stats["scanned"] += len(ids)
            for m in real:
                if await self.ingest(m, notifier) == "new":
                    stats["new"] += 1
            if not real:
                empty_batches += 1
                if empty_batches >= max_empty_batches:
                    break
            else:
                empty_batches = 0
            nxt += SCAN_BATCH
        return stats

    async def _bump_last_id(self, mid: int, file_at=None) -> None:
        upd: Dict[str, Any] = {"$max": {"last_message_id": int(mid)}}
        if file_at is not None:
            upd["$set"] = {"last_file_at": file_at}
        await self.db.sources.update_one({"_id": self.name}, upd)


def _esc(s: str) -> str:
    from ..tg import esc
    return esc(s)
