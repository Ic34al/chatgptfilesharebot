"""Live intake for Telegram provider channels (see anime_soul/sources/telegram_channel.py).

Only chats registered with ``/sources add_tg`` are read; every other channel or group is ignored.
Runs in its own handler group so the existing /start, file-sharing, FSUB and admin handlers are
unaffected (they only handle private chats / commands).
"""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from .user import bind

log = logging.getLogger(__name__)

INGEST_GROUP = 7


async def provider_post_handler(ctx, client, msg) -> None:
    if ctx.sources is None or msg.chat is None:
        return
    try:
        channels = await ctx.sources.channel_adapters()
    except Exception:  # noqa: BLE001
        log.exception("Could not load provider channels")
        return
    ad = next((a for a in channels if a.chat_id == msg.chat.id), None)
    if ad is None:
        return
    try:
        result = await ad.ingest(msg, ctx.notifier)
        log.info("Provider %s message %s: %s", ad.name, msg.id, result)
    except Exception:  # noqa: BLE001 - catch-up in the monitor retries this message later
        log.exception("Provider %s message %s could not be stored", ad.name, msg.id)


def register(app: Client, ctx) -> None:
    media = filters.video | filters.document
    app.add_handler(MessageHandler(bind(provider_post_handler, ctx), (filters.channel | filters.group) & media),
                    group=INGEST_GROUP)
