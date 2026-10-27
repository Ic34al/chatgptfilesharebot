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
                         keepalive: Optional[Callable[[], Awaitable[None]]] = None) -> dict:
    """Upload and verify. Returns the storage record to persist in MongoDB."""
    caption = storage_caption(key, anime, episode, item.quality)
    last = {"pct": -1}

    async def progress(current, total):
        pct = int(current * 100 / total) if total else 0
        if pct // 10 != last["pct"] // 10:
            last["pct"] = pct
            log.info("Uploading %s: %s%%", key, pct)
            if keepalive:
                await keepalive()

    async def _do():
        if item.kind == "telegram":
            return await call_with_flood(client.send_cached_media, storage_chat, item.ref, caption=caption,
                                         parse_mode=enums.ParseMode.DISABLED)
        if not local_path or not os.path.isfile(local_path):
            raise UploadError("Local file missing before upload", transient=False)
        if container == "mp4":
            return await call_with_flood(client.send_video, storage_chat, local_path, caption=caption,
                                         parse_mode=enums.ParseMode.DISABLED,
                                         file_name=item.file_name, supports_streaming=True, progress=progress)
        return await call_with_flood(client.send_document, storage_chat, local_path, caption=caption,
                                     parse_mode=enums.ParseMode.DISABLED,
                                     file_name=item.file_name, force_document=True, progress=progress)

    try:
        msg = await asyncio.wait_for(_do(), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise UploadError("Upload timed out")
    if msg is None:
        raise UploadError("Telegram returned no message")
    media_type, media = _media_of(msg)
    if not media or not getattr(media, "file_id", None):
        raise UploadError("Uploaded message has no media", transient=False)
    if local_path and getattr(media, "file_size", None) and os.path.isfile(local_path):
        if int(media.file_size) != os.path.getsize(local_path):
            raise UploadError("Uploaded size differs from local file")
    return {
        "chat_id": getattr(getattr(msg, "chat", None), "id", storage_chat),
        "message_id": msg.id,
        "file_id": media.file_id,
        "file_unique_id": getattr(media, "file_unique_id", None),
        "file_size": getattr(media, "file_size", None) or item.size,
        "file_name": getattr(media, "file_name", None) or item.file_name,
        "media_type": media_type,
    }
