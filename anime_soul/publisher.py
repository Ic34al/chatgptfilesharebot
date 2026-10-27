"""Publish the episode announcement with a GET EPISODE button."""
from __future__ import annotations

import asyncio
import logging

from pyrogram import enums
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .captions import CAPTION_LIMIT, DEFAULT_TEMPLATE, MESSAGE_LIMIT, caption_values, render
from .deeplink import build_link
from .tg import call_with_flood

log = logging.getLogger(__name__)


class AmbiguousPublish(Exception):
    """The send may or may not have reached Telegram (e.g. network timeout)."""


def episode_keyboard(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("GET EPISODE", url=link)]])


async def build_post(db, cfg, anime: dict, episode: dict, bot_username: str):
    template = await db.get_caption(anime["_id"]) or DEFAULT_TEMPLATE
    values = caption_values(anime, episode, cfg)
    photo = anime.get("thumbnail_file_id") or (anime.get("cover_url") if anime.get("use_cover", True) else None)
    text = render(template, values, CAPTION_LIMIT if photo else MESSAGE_LIMIT)
    link = build_link(bot_username, episode["token"])
    return text, photo, episode_keyboard(link)


async def send_post(client, chat_id, text, photo, keyboard):
    if photo:
        try:
            return await call_with_flood(client.send_photo, chat_id, photo, caption=text,
                                         parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
        except RPCError as e:
            # Bad thumbnail (e.g. cover URL Telegram can't fetch) — fall back to text post.
            if "PHOTO" not in str(e).upper() and "WEBPAGE" not in str(e).upper() and "MEDIA" not in str(e).upper():
                raise
            log.warning("Thumbnail rejected (%s); posting text only", e)
    return await call_with_flood(client.send_message, chat_id, text, parse_mode=enums.ParseMode.HTML,
                                 reply_markup=keyboard, disable_web_page_preview=True)


async def publish(client, db, cfg, anime: dict, episode: dict, bot_username: str, *, timeout_s: int = 120):
    """Publish exactly once. Returns message id, or None if it was already published."""
    key = episode["_id"]
    if not episode.get("storage") or not episode.get("token"):
        raise ValueError("Refusing to publish before the upload is verified")
    text, photo, keyboard = await build_post(db, cfg, anime, episode, bot_username)
    if not await db.claim_post(key, cfg.post_channel_id):
        post = await db.get_post(key)
        if post and post.get("state") == "published":
            return None
        raise AmbiguousPublish("A previous publish attempt did not confirm; check the channel, then "
                               f"/retry {key} publish")
    try:
        msg = await asyncio.wait_for(send_post(client, cfg.post_channel_id, text, photo, keyboard), timeout_s)
    except RPCError:
        await db.abort_post(key)          # Telegram rejected it: nothing was posted
        raise
    except (asyncio.TimeoutError, OSError, ConnectionError) as e:
        raise AmbiguousPublish(f"Network problem while posting ({e!r}); check the channel, then "
                               f"/retry {key} publish")
    await db.finish_post(key, msg.id)
    return msg.id
