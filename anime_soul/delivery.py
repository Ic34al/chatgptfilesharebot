"""Send the actual stored episode file to a subscriber."""
from __future__ import annotations

import logging

from pyrogram import enums
from pyrogram.errors import FloodWait, RPCError

from .tg import call_with_flood

log = logging.getLogger(__name__)


def delivery_caption(anime: dict | None, episode: dict) -> str:
    title = (anime or {}).get("title_english") or (anime or {}).get("title_romaji") or "Episode"
    return f"{title} — Episode {episode.get('episode')}"[:1000]


async def deliver_episode(client, chat_id: int, episode: dict, anime: dict | None, protect: bool = False):
    """Send by file_id; fall back to copying the storage-channel message."""
    storage = episode.get("storage") or {}
    if not storage.get("file_id"):
        raise ValueError("Episode has no stored file")
    caption = delivery_caption(anime, episode)
    try:
        return await call_with_flood(client.send_cached_media, chat_id, storage["file_id"], caption=caption, parse_mode=enums.ParseMode.DISABLED,
                                     protect_content=protect)
    except FloodWait:
        raise
    except RPCError as e:
        log.warning("send_cached_media failed (%s); copying from storage channel", e)
        if not storage.get("chat_id") or not storage.get("message_id"):
            raise
        return await call_with_flood(client.copy_message, chat_id, storage["chat_id"], storage["message_id"],
                                     caption=caption, parse_mode=enums.ParseMode.DISABLED,
                                     protect_content=protect)
