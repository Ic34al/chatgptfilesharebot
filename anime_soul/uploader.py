"""Upload validated episode files to the Telegram storage channel."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional

from pyrogram import enums

from .sources.base import SourceItem
from .tg import call_with_flood

log = logging.getLogger(__name__)


class UploadError(Exception):
    def __init__(self, msg, transient=True):
        super().__init__(msg)
        self.transient = transient


def _media_of(msg):
    for attr in ("video", "document", "animation"):
        m = getattr(msg, attr, None)
        if m:
            return attr, m
    return None, None


def storage_caption(key: str, anime: dict, episode: int, quality: Optional[str]) -> str:
    title = anime.get("title_english") or anime.get("title_romaji") or str(anime.get("_id"))
    return f"{title} — Episode {episode}\n{quality or ''}\n#{key.replace(':', '_')}"[:1000]


async def upload_episode(client, storage_chat, key: str, anime: dict, episode: int, item: SourceItem,
                         local_path: Optional[str], *, timeout_s: int, container: Optional[str] = None,
                         media: Optional[dict] = None,
                         keepalive: Optional[Callable[[], Awaitable[None]]] = None) -> dict:
    """Upload and verify. Returns the storage record to persist in MongoDB."""
    media = media or {}
    quality = media.get("quality") or item.quality
    caption = storage_caption(key, anime, episode, quality)
    file_name = item.file_name
    if local_path and os.path.splitext(local_path)[1].lower() != os.path.splitext(file_name)[1].lower():
        file_name = os.path.splitext(file_name)[0] + os.path.splitext(local_path)[1]   # remuxed/transcoded
    last = {"pct": -1}

    async def progress(current, total):
        pct = int(current * 100 / total) if total else 0
        if pct // 10 != last["pct"] // 10:
            last["pct"] = pct
            log.info("Uploading %s: %s%%", key, pct)
            if keepalive:
                await keepalive()

    async def _do():
        if item.kind == "telegram" and not local_path:
            return await call_with_flood(client.send_cached_media, storage_chat, item.ref, caption=caption,
                                         parse_mode=enums.ParseMode.DISABLED)
        if not local_path or not os.path.isfile(local_path):
            raise UploadError("Local file missing before upload", transient=False)
        if container == "mp4":
            extra = {k: int(media[k]) for k in ("duration", "width", "height") if media.get(k)}
            return await call_with_flood(client.send_video, storage_chat, local_path, caption=caption,
                                         parse_mode=enums.ParseMode.DISABLED,
                                         file_name=file_name, supports_streaming=True, progress=progress, **extra)
        return await call_with_flood(client.send_document, storage_chat, local_path, caption=caption,
                                     parse_mode=enums.ParseMode.DISABLED,
                                     file_name=file_name, force_document=True, progress=progress)

    try:
        msg = await asyncio.wait_for(_do(), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise UploadError("Upload timed out")
    if msg is None:
        raise UploadError("Telegram returned no message")
    media_type, m = _media_of(msg)
    if not m or not getattr(m, "file_id", None):
        raise UploadError("Uploaded message has no media", transient=False)
    if local_path and getattr(m, "file_size", None) and os.path.isfile(local_path):
        if int(m.file_size) != os.path.getsize(local_path):
            raise UploadError("Uploaded size differs from local file")
    return {
        "chat_id": getattr(getattr(msg, "chat", None), "id", storage_chat),
        "message_id": msg.id,
        "file_id": m.file_id,
        "file_unique_id": getattr(m, "file_unique_id", None),
        "file_size": getattr(m, "file_size", None) or item.size,
        "file_name": getattr(m, "file_name", None) or file_name,
        "media_type": media_type,
        "quality": quality,
        "duration": media.get("duration") or getattr(m, "duration", None),
        "source": item.source,
        "source_ref": item.ref if item.kind != "url" else item.ref.split("?", 1)[0],
    }
