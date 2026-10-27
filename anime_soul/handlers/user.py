"""Subscriber-facing handlers: /start deep links, FSUB, file delivery, file links."""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, RPCError
from pyrogram.handlers import CallbackQueryHandler, ChatJoinRequestHandler, ChatMemberUpdatedHandler, MessageHandler

from .. import models as M
from ..deeplink import build_link, parse_start_payload
from ..delivery import deliver_episode
from ..tg import call_with_flood

log = logging.getLogger(__name__)

WELCOME = ("👋 Welcome to File Share Bot!\n\n"
           "Send me a Telegram file and I will create a shareable link.\n\n"
           "⚡ Fast • 🔗 Shareable • 24/7")
INVALID = "❌ Invalid or expired file link."
FSUB_TEXT = ("🔒 To get this episode, please join our channel(s) first.\n\n"
             "After joining, tap <b>TRY AGAIN</b>.")


async def resolve_token(ctx, token: str):
    """Return ("episode", doc) | ("file", doc) | (None, None)."""
    if not token or not M.is_valid_token(token):
        return None, None
    if token.startswith(M.EPISODE_TOKEN_PREFIX):
        ep = await ctx.db.get_episode_by_token(token)
        if ep:
            return "episode", ep
    doc = await ctx.filedb.get_file(token)
    if doc:
        return "file", doc
    return None, None


async def needs_fsub(ctx, kind: str, doc: dict) -> bool:
    if not ctx.cfg.fsub_channels:
        return False
    if kind == "episode":
        return True
    return bool(ctx.cfg.fsub_legacy_links or doc.get("fsub"))


async def send_resolved(ctx, client, chat_id: int, kind: str, doc: dict):
    if kind == "episode":
        anime = await ctx.db.get_anime(doc["anime_id"])
        return await deliver_episode(client, chat_id, doc, anime, protect=ctx.cfg.protect_content)
    # Original behaviour for share links.
    return await call_with_flood(client.send_cached_media, chat_id, doc["file_id"],
                                 protect_content=doc.get("protect", False))


async def start_handler(ctx, client, message):
    user_id = message.from_user.id if message.from_user else None
    if user_id:
        try:
            await ctx.db.touch_user(user_id)
        except Exception:  # never block delivery on analytics
            log.debug("touch_user failed", exc_info=True)
    raw_parts = (message.text or "").split(maxsplit=1)
    if len(raw_parts) < 2:
        await message.reply_text(WELCOME)
        return
    token = parse_start_payload(message.text)
    kind, doc = await resolve_token(ctx, token) if token else (None, None)
    if not doc:
        await message.reply_text(INVALID)
        return
    if await needs_fsub(ctx, kind, doc):
        missing = await ctx.fsub.missing(user_id)
        if missing:
            await message.reply_text(FSUB_TEXT, reply_markup=ctx.fsub.keyboard(missing, f"fsub:{token}"),
                                     disable_web_page_preview=True)
            return
    try:
        await send_resolved(ctx, client, message.chat.id, kind, doc)
    except FloodWait:
        await message.reply_text("⏳ Telegram is rate-limiting me. Please try again in a minute.")
    except (RPCError, ValueError) as e:
        log.warning("Delivery failed for token %s…: %s", token[:6], e)
        await message.reply_text("⚠️ Sorry, I couldn't send this file right now. Please try again later.")


async def try_again_handler(ctx, client, query):
    data = query.data or ""
    token = data.split(":", 1)[1] if ":" in data else ""
    kind, doc = await resolve_token(ctx, token)
    if not doc:
        await query.answer("This link is invalid or expired.", show_alert=True)
        return
    missing = await ctx.fsub.missing(query.from_user.id) if await needs_fsub(ctx, kind, doc) else []
    if missing:
        await query.answer("You still need to join: " + ", ".join(m.title for m in missing)[:150], show_alert=True)
        try:
            await query.message.edit_reply_markup(ctx.fsub.keyboard(missing, f"fsub:{token}"))
        except RPCError:
            pass
        return
    await query.answer("✅ Verified! Sending your file…")
    try:
        await query.message.delete()
    except RPCError:
        pass
    try:
        await send_resolved(ctx, client, query.from_user.id, kind, doc)
    except (RPCError, ValueError) as e:
        log.warning("Delivery after TRY AGAIN failed: %s", e)
        await client.send_message(query.from_user.id, "⚠️ Sorry, I couldn't send this file right now.")


async def file_handler(ctx, client, message):
    """Original feature: send a file in private chat → get a share link."""
    if not ctx.cfg.public_file_links and not ctx.is_admin(message.from_user.id if message.from_user else None):
        return
    media = message.document or message.video or message.audio or message.photo or message.animation
    unique_id = getattr(media, "file_unique_id", "")
    existing = await ctx.filedb.find_by_unique_id(unique_id)
    # Old tokens were base64(file_id) and exceed Telegram's 64-char /start limit; reuse only valid ones.
    token = existing["token"] if existing and M.is_valid_token(existing["token"]) else M.new_file_token()
    await ctx.filedb.save_file(token, media.file_id, unique_id, bool(getattr(message, "protect_content", False)))
    me = client.me or await client.get_me()
    link = build_link(me.username, token)
    size = getattr(media, "file_size", 0) or 0
    suffix = f"\n📦 Size: `{size / (1024 * 1024):.2f} MB`" if size else ""
    await message.reply_text(f"✅ **File saved!**\n\n🔗 **Share link:**\n{link}{suffix}",
                             disable_web_page_preview=True)


async def help_handler(ctx, client, message):
    await message.reply_text(
        "Send me a document, video, audio, photo or animation.\n"
        "I will return a permanent share link while its record remains in the database.\n\n"
        "Got here from a GET EPISODE button? Just tap it again after joining the required channels.")


async def join_request_handler(ctx, client, request):
    if ctx.cfg.fsub_join_request:
        await ctx.db.record_join_request(request.from_user.id, request.chat.id)


async def member_update_handler(ctx, client, update):
    new = getattr(update, "new_chat_member", None)
    user = getattr(new, "user", None) if new else getattr(getattr(update, "old_chat_member", None), "user", None)
    if user is None:
        return
    status = getattr(new, "status", None)
    from pyrogram import enums
    if new is None or status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED):
        await ctx.db.clear_join_request(user.id, update.chat.id)


def bind(fn, ctx):
    """Pyrogram only awaits real coroutine functions, so wrap instead of using lambdas."""
    async def _handler(client, update):
        await fn(ctx, client, update)
    _handler.__name__ = fn.__name__
    return _handler


def register(app: Client, ctx) -> None:
    media = filters.document | filters.video | filters.audio | filters.photo | filters.animation
    app.add_handler(MessageHandler(bind(start_handler, ctx), filters.command("start")))
    app.add_handler(MessageHandler(bind(help_handler, ctx), filters.command("help")))
    app.add_handler(CallbackQueryHandler(bind(try_again_handler, ctx), filters.regex(r"^fsub:")))
    app.add_handler(MessageHandler(bind(file_handler, ctx), filters.private & media))
    fsub_ids = [c for c in ctx.cfg.fsub_channels if isinstance(c, int)]
    fsub_names = [c.lstrip("@") for c in ctx.cfg.fsub_channels if isinstance(c, str)]
    chats = filters.chat(fsub_ids + fsub_names) if (fsub_ids or fsub_names) else filters.chat([])
    app.add_handler(ChatJoinRequestHandler(bind(join_request_handler, ctx), chats))
    app.add_handler(ChatMemberUpdatedHandler(bind(member_update_handler, ctx), chats))
