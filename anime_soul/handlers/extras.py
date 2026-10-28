"""Bot features adapted from AutoAnime.zip ("An Advanced Anime Downloader Bot", GPL-3.0).

Independent Pyrogram re-implementations of the zip's *legitimate* bot features, wired into ANIME SOUL's
existing context, MongoDB and admin guard. No zip code was copied. The zip's scrapers, proxy rotation,
Tor and N_m3u8DL-RE stream ripping are not included (see docs/AUTOANIME_INTEGRATION.md).

Zip feature → here
* check_ban decorator, ban/unban/banned list panel → /ban /unban /banned + is_banned()
* /add_admin /rm_admin /admins (owner only)     → /addadmin /rmadmin /admins (OWNER_ID/ADMIN_IDS only)
* /autodel + schedule_deletion (in-memory sleep) → /autodel + MongoDB autodelete_queue (survives restarts)
* /ping /restart (os.execl) /users              → /ping /restart (graceful stop, then re-exec) /users
* /setmap /unmap /maplist (mapped channels)     → same commands, keyed by AniList ID, posted once per episode
* /manage inline settings panel                 → /manage (auto-upload, dry run, auto-delete, mapped posts)
* /ongoing /schedule (scraped site schedule)    → public /ongoing from the bot's own AniList data
* search + episode list buttons (scraped sites) → public /search over ANIME SOUL's published episodes
* set_bot_commands on startup                   → set_bot_commands() (public + admin scopes)
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import time
from datetime import timedelta

from pyrogram import Client, enums, filters
from pyrogram.errors import RPCError
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (BotCommand, BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardButton,
                            InlineKeyboardMarkup)

from ..deeplink import build_link
from ..publisher import build_post, send_post
from ..tg import call_with_flood, esc
from ..timeutil import utcnow
from .user import bind

log = logging.getLogger(__name__)
HTML = enums.ParseMode.HTML
AUTODEL_PRESETS = (0, 300, 600, 1800, 3600)
SWEEP_INTERVAL = 15
BANNED_TEXT = "⛔ You are banned from using this bot."


def _args(m):
    return (m.text or "").split()[1:]


def _uid(m):
    return m.from_user.id if getattr(m, "from_user", None) else None


# ------------------------------------------------------------------ bans
async def is_banned(ctx, user_id) -> bool:
    if not user_id or ctx.is_admin(user_id):
        return False
    try:
        return await ctx.db.get_ban(int(user_id)) is not None
    except Exception:  # noqa: BLE001 - a DB hiccup must not lock everyone out
        log.debug("ban lookup failed", exc_info=True)
        return False


async def cmd_ban(ctx, client, m):
    a = _args(m)
    if not a or not a[0].lstrip("-").isdigit():
        await m.reply_text("Usage: /ban <user id> [reason]")
        return
    uid = int(a[0])
    if ctx.is_admin(uid):
        await m.reply_text("❌ Administrators can't be banned.")
        return
    reason = " ".join(a[1:]) or "No reason given"
    await ctx.db.ban_user(uid, reason, _uid(m))
    await m.reply_text(f"🚫 Banned <code>{uid}</code>. Reason: {esc(reason)}", parse_mode=HTML)


async def cmd_unban(ctx, client, m):
    a = _args(m)
    if not a or not a[0].lstrip("-").isdigit():
        await m.reply_text("Usage: /unban <user id>")
        return
    ok = await ctx.db.unban_user(int(a[0]))
    await m.reply_text("✅ Unbanned." if ok else "That user wasn't banned.")


async def cmd_banned(ctx, client, m):
    rows = await ctx.db.list_bans()
    if not rows:
        await m.reply_text("No banned users.")
        return
    lines = ["<b>Banned users</b>"] + [f"• <code>{r['_id']}</code> — {esc(r.get('reason') or '')}" for r in rows]
    await m.reply_text("\n".join(lines)[:4000], parse_mode=HTML)


# ------------------------------------------------------------------ admins
async def load_admins(ctx) -> None:
    ctx.extra_admins = {int(d["_id"]) for d in await ctx.db.list_bot_admins()}


async def cmd_addadmin(ctx, client, m):
    if not ctx.is_owner(_uid(m)):
        await m.reply_text("⛔ Only the owner (OWNER_ID / ADMIN_IDS) can add admins.")
        return
    a = _args(m)
    if not a or not a[0].isdigit():
        await m.reply_text("Usage: /addadmin <user id>")
        return
    added = await ctx.db.add_bot_admin(int(a[0]), _uid(m))
    await load_admins(ctx)
    await m.reply_text("✅ Admin added." if added else "Already an admin.")


async def cmd_rmadmin(ctx, client, m):
    if not ctx.is_owner(_uid(m)):
        await m.reply_text("⛔ Only the owner (OWNER_ID / ADMIN_IDS) can remove admins.")
        return
    a = _args(m)
    if not a or not a[0].isdigit():
        await m.reply_text("Usage: /rmadmin <user id>")
        return
    if ctx.is_owner(int(a[0])):
        await m.reply_text("Owners come from the OWNER_ID / ADMIN_IDS Config Vars; change them in Heroku.")
        return
    ok = await ctx.db.remove_bot_admin(int(a[0]))
    await load_admins(ctx)
    await m.reply_text("✅ Admin removed." if ok else "That user isn't an added admin.")


async def cmd_admins(ctx, client, m):
    owners = ", ".join(f"<code>{i}</code>" for i in ctx.cfg.all_admin_ids) or "—"
    extra = ", ".join(f"<code>{i}</code>" for i in sorted(ctx.extra_admins)) or "—"
    await m.reply_text(f"<b>Owners</b> (Config Vars): {owners}\n<b>Added admins</b>: {extra}", parse_mode=HTML)


async def cmd_users(ctx, client, m):
    total = await ctx.db.users.count_documents({})
    blocked = await ctx.db.users.count_documents({"blocked": True})
    banned = await ctx.db.banned.count_documents({})
    await m.reply_text(f"👤 Users: {total} · reachable: {total - blocked} · blocked the bot: {blocked} · banned: {banned}")


# ------------------------------------------------------------------ ping / restart
async def cmd_ping(ctx, client, m):
    t0 = time.perf_counter()
    msg = await m.reply_text("Pong…")
    ms = (time.perf_counter() - t0) * 1000
    try:
        db_ms = f" · MongoDB {await ctx.db.ping():.0f} ms"
    except Exception:  # noqa: BLE001
        db_ms = " · MongoDB unreachable"
    await msg.edit_text(f"🏓 Pong! Telegram {ms:.0f} ms{db_ms}")


async def cmd_restart(ctx, client, m):
    msg = await m.reply_text("🔄 Restarting… running jobs are stopped safely and resume after the restart.")
    await ctx.db.set_setting("restart_notice", {"chat_id": msg.chat.id, "message_id": msg.id})
    ctx.restart_requested = True
    if ctx.stop_event is not None:
        ctx.stop_event.set()


async def announce_restart(ctx, client) -> None:
    try:
        note = await ctx.db.get_setting("restart_notice")
    except Exception:  # noqa: BLE001
        return
    if not note:
        return
    await ctx.db.set_setting("restart_notice", None)
    try:
        await client.edit_message_text(note["chat_id"], note["message_id"], "✅ Bot restarted successfully.")
    except (RPCError, KeyError, TypeError, AttributeError):
        pass


def system_stats() -> str:
    """CPU load / RAM / disk without extra dependencies (the zip used psutil)."""
    parts = [f"OS {platform.system()} {platform.release()} · Python {platform.python_version()}"]
    try:
        la = os.getloadavg()
        parts.append(f"Load {la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f} ({os.cpu_count()} CPU)")
    except (OSError, AttributeError):
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, v = ln.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        parts.append(f"RAM {_fmt(total - avail)} / {_fmt(total)}")
    except (OSError, KeyError, ValueError):
        pass
    try:
        with open("/proc/self/status") as f:
            rss = next(int(ln.split()[1]) * 1024 for ln in f if ln.startswith("VmRSS"))
        parts.append(f"Bot process {_fmt(rss)}")
    except (OSError, StopIteration, ValueError):
        pass
    du = shutil.disk_usage("/")
    parts.append(f"Disk {_fmt(du.used)} / {_fmt(du.total)}")
    return "\n".join(parts)


def _fmt(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


async def db_stats(db) -> str:
    try:
        s = await db.db.command("dbStats")
        return f"MongoDB {_fmt(s.get('storageSize', 0) + s.get('indexSize', 0))} used"
    except Exception:  # noqa: BLE001
        return "MongoDB size unavailable"


# ------------------------------------------------------------------ auto-delete
async def autodel_seconds(ctx) -> int:
    return int(await ctx.db.get_setting("autodel_seconds", 0) or 0)


async def after_delivery(ctx, client, chat_id: int, sent, user_id=None) -> None:
    """Queue a delivered file (+ notice) for deletion when /autodel is on. Admins are exempt."""
    secs = await autodel_seconds(ctx)
    if secs <= 0 or sent is None or ctx.is_admin(user_id):
        return
    ids = [sent.id]
    try:
        mins = max(1, secs // 60)
        note = await client.send_message(chat_id, f"🗑 This file will be deleted in {mins} min. "
                                                  "Forward it to Saved Messages to keep it.",
                                         reply_to_message_id=sent.id)
        ids.append(note.id)
    except RPCError:
        pass
    await ctx.db.queue_autodelete(chat_id, ids, utcnow() + timedelta(seconds=secs))


async def sweep_autodeletes(ctx, client, now=None) -> int:
    n = 0
    for d in await ctx.db.due_autodeletes(now or utcnow()):
        try:
            await call_with_flood(client.delete_messages, d["chat_id"], d["message_ids"])
        except RPCError as e:   # already deleted / user blocked the bot: nothing more to do
            log.debug("autodelete %s: %s", d["_id"], e)
        await ctx.db.done_autodelete(d["_id"])
        n += 1
    return n


async def autodelete_loop(ctx, client, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await sweep_autodeletes(ctx, client)
        except Exception:  # noqa: BLE001
            log.exception("autodelete sweep failed")
        try:
            await asyncio.wait_for(stop.wait(), SWEEP_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def cmd_autodel(ctx, client, m):
    a = _args(m)
    if not a or not a[0].isdigit():
        cur = await autodel_seconds(ctx)
        await m.reply_text(f"🕒 Auto-delete: {f'{cur} s' if cur else 'off'}.\n"
                           "Usage: /autodel 600 (delete delivered files after 10 min) · /autodel 0 (off)")
        return
    secs = int(a[0])
    if 0 < secs < 60 or secs > 7 * 86400:
        await m.reply_text("Use 0 (off) or 60–604800 seconds.")
        return
    await ctx.db.set_setting("autodel_seconds", secs)
    await m.reply_text(f"✅ Auto-delete {'set to ' + str(secs) + ' s' if secs else 'disabled'}. "
                       "Applies to files delivered from now on; admins are exempt.")


# ------------------------------------------------------------------ mapped channels
async def cmd_setmap(ctx, client, m):
    a = _args(m)
    if len(a) < 2 or not a[0].lstrip("-").isdigit() or not a[1].isdigit():
        await m.reply_text("Usage: /setmap <channel id> <anilist id>\nNew episodes of that anime are also posted "
                           "in that channel (with the same GET EPISODE button).")
        return
    chat_id, anime_id = int(a[0]), int(a[1])
    if chat_id in (ctx.cfg.storage_channel_id, ctx.cfg.post_channel_id):
        await m.reply_text("❌ That is the storage/post channel already.")
        return
    if not await ctx.db.get_anime(anime_id):
        await m.reply_text("That anime isn't tracked. /addanime it first.")
        return
    try:
        member = await client.get_chat_member(chat_id, "me")
        status = getattr(getattr(member, "status", None), "value", str(getattr(member, "status", "")))
    except RPCError as e:
        await m.reply_text(f"❌ I can't access {chat_id} ({e}). Add the bot as an admin with 'Post messages'.")
        return
    if status not in ("administrator", "owner"):
        await m.reply_text("❌ Make the bot an administrator of that channel first.")
        return
    await ctx.db.map_channel(chat_id, anime_id)
    await m.reply_text(f"✅ Episodes of {anime_id} will also be posted in {chat_id}.")


async def cmd_unmap(ctx, client, m):
    a = _args(m)
    if not a or not a[0].lstrip("-").isdigit():
        await m.reply_text("Usage: /unmap <channel id> [anilist id]")
        return
    ok = await ctx.db.unmap_channel(int(a[0]), int(a[1]) if len(a) > 1 and a[1].isdigit() else None)
    await m.reply_text("✅ Mapping removed." if ok else "No such mapping.")


async def cmd_maplist(ctx, client, m):
    rows = await ctx.db.list_mappings()
    if not rows:
        await m.reply_text("No mapped channels. /setmap <channel id> <anilist id>")
        return
    lines = ["<b>Mapped channels</b>"] + [f"• <code>{r['_id']}</code> ← {', '.join(map(str, r.get('anime_ids', [])))}"
                                          for r in rows]
    await m.reply_text("\n".join(lines)[:4000], parse_mode=HTML)


async def post_to_mapped(ctx_or_db, client, cfg, anime: dict, episode: dict, bot_username: str, notifier=None) -> int:
    """Also post a published episode in its mapped channels. Never fails the episode."""
    db = ctx_or_db.db if hasattr(ctx_or_db, "cfg") else ctx_or_db   # Ctx or AnimeDB (AnimeDB.db is raw Motor)
    if not await db.get_setting("mapped_posts_enabled", True):
        return 0
    n = 0
    for chat in await db.channels_for_anime(anime["_id"]):
        if not await db.claim_mapped_post(episode["_id"], chat):
            continue
        try:
            text, photo, kb = await build_post(db, cfg, anime, episode, bot_username)
            msg = await send_post(client, chat, text, photo, kb)
            await db.finish_mapped_post(episode["_id"], chat, msg.id)
            n += 1
        except Exception as e:  # noqa: BLE001
            await db.finish_mapped_post(episode["_id"], chat, None, error=str(e))
            log.warning("Mapped post %s → %s failed: %s", episode["_id"], chat, e)
            if notifier is not None:
                await notifier.alert(f"⚠️ Could not post {esc(episode['_id'])} in mapped channel {chat}: "
                                     f"{esc(str(e)[:200])}", key=f"mapped:{chat}")
    return n


# ------------------------------------------------------------------ /manage panel
async def _panel(ctx):
    paused = bool(await ctx.db.get_setting("paused", False))
    dry = await ctx.db.get_setting("dry_run", None)
    dry = ctx.cfg.dry_run if dry is None else bool(dry)
    secs = await autodel_seconds(ctx)
    mapped = bool(await ctx.db.get_setting("mapped_posts_enabled", True))
    text = ("<b>⚙️ ANIME SOUL settings</b>\n"
            f"Automatic upload: {'⏸ paused' if paused else '▶️ running'}\n"
            f"Dry run: {'🧪 on (nothing is uploaded/posted)' if dry else 'off'}\n"
            f"Auto-delete delivered files: {f'{secs // 60} min' if secs else 'off'}\n"
            f"Mapped-channel posts: {'on' if mapped else 'off'}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Resume uploads" if paused else "⏸ Pause uploads", callback_data="mng:pause")],
        [InlineKeyboardButton("🧪 Dry run: turn off" if dry else "🧪 Dry run: turn on", callback_data="mng:dry")],
        [InlineKeyboardButton(f"🗑 Auto-delete: {f'{secs // 60} min' if secs else 'off'} (tap to change)",
                              callback_data="mng:autodel")],
        [InlineKeyboardButton(f"📣 Mapped posts: {'on' if mapped else 'off'}", callback_data="mng:mapped")],
        [InlineKeyboardButton("✖️ Close", callback_data="mng:close")],
    ])
    return text, kb


async def cmd_manage(ctx, client, m):
    text, kb = await _panel(ctx)
    await m.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def manage_callback(ctx, client, q):
    if not ctx.is_admin(q.from_user.id if q.from_user else None):
        await q.answer("Only administrators can change settings.", show_alert=True)
        return
    action = (q.data or "").split(":", 1)[-1]
    if action == "close":
        try:
            await q.message.delete()
        except RPCError:
            pass
        return
    if action == "pause":
        await ctx.db.set_setting("paused", not bool(await ctx.db.get_setting("paused", False)))
    elif action == "dry":
        cur = await ctx.db.get_setting("dry_run", None)
        cur = ctx.cfg.dry_run if cur is None else bool(cur)
        await ctx.db.set_setting("dry_run", not cur)
    elif action == "autodel":
        cur = await autodel_seconds(ctx)
        nxt = AUTODEL_PRESETS[(AUTODEL_PRESETS.index(cur) + 1) % len(AUTODEL_PRESETS)] if cur in AUTODEL_PRESETS else 0
        await ctx.db.set_setting("autodel_seconds", nxt)
    elif action == "mapped":
        await ctx.db.set_setting("mapped_posts_enabled", not bool(await ctx.db.get_setting("mapped_posts_enabled", True)))
    text, kb = await _panel(ctx)
    await q.answer("Saved")
    try:
        await q.message.edit_text(text, parse_mode=HTML, reply_markup=kb)
    except RPCError:
        pass


# ------------------------------------------------------------------ public: /ongoing, /search
async def cmd_ongoing(ctx, client, m):
    if await is_banned(ctx, _uid(m)):
        await m.reply_text(BANNED_TEXT)
        return
    from ..schedule_post import build_schedule_text
    await m.reply_text(await build_schedule_text(ctx.db), parse_mode=HTML, disable_web_page_preview=True)


def _title(a: dict) -> str:
    return a.get("title_english") or a.get("title_romaji") or str(a["_id"])


async def _episode_buttons(ctx, client, anime: dict, page: int = 0):
    eps = await ctx.db.published_episodes(anime["_id"])
    if not eps:
        return f"<b>{esc(_title(anime))}</b>\nNo episodes uploaded yet.", None
    me = client.me or await client.get_me()
    per = 24
    page = max(0, min(page, (len(eps) - 1) // per))
    chunk = eps[page * per:(page + 1) * per]
    rows, row = [], []
    for e in chunk:
        row.append(InlineKeyboardButton(f"EP {e['episode']}", url=build_link(me.username, e["token"])))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"srch:{anime['_id']}:{page - 1}"))
    if (page + 1) * per < len(eps):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"srch:{anime['_id']}:{page + 1}"))
    if nav:
        rows.append(nav)
    return (f"<b>{esc(_title(anime))}</b>\n{len(eps)} episode(s) available. Tap one to get it.",
            InlineKeyboardMarkup(rows))


async def cmd_search(ctx, client, m):
    if await is_banned(ctx, _uid(m)):
        await m.reply_text(BANNED_TEXT)
        return
    query = " ".join(_args(m)).strip()
    if len(query) < 2:
        await m.reply_text("Usage: /search <anime name>")
        return
    found = await ctx.db.search_anime(query)
    if not found:
        await m.reply_text("Nothing found in ANIME SOUL's library.")
        return
    if len(found) == 1:
        text, kb = await _episode_buttons(ctx, client, found[0])
        await m.reply_text(text, parse_mode=HTML, reply_markup=kb)
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(_title(a)[:60], callback_data=f"srch:{a['_id']}:0")]
                               for a in found])
    await m.reply_text("Choose an anime:", reply_markup=kb)


async def search_callback(ctx, client, q):
    if await is_banned(ctx, q.from_user.id if q.from_user else None):
        await q.answer(BANNED_TEXT, show_alert=True)
        return
    try:
        _, aid, page = (q.data or "").split(":")
        anime = await ctx.db.get_anime(int(aid))
    except (ValueError, TypeError):
        anime = None
    if not anime:
        await q.answer("Not found", show_alert=True)
        return
    text, kb = await _episode_buttons(ctx, client, anime, int(page))
    await q.answer()
    try:
        await q.message.edit_text(text, parse_mode=HTML, reply_markup=kb)
    except RPCError:
        pass


# ------------------------------------------------------------------ bot command menu
PUBLIC_COMMANDS = [("start", "Start the bot"), ("help", "How to use the bot"), ("ongoing", "Today's anime schedule"),
                   ("search", "Find an anime in our library"), ("ping", "Check the bot is alive")]
ADMIN_MENU = [("manage", "Settings panel"), ("status", "Health"), ("queue", "Episode queue"),
              ("sources", "Authorized sources"), ("unmatched", "Provider files to fix"), ("retry", "Retry a job"),
              ("stats", "Statistics"), ("users", "User count"), ("broadcast", "Broadcast (reply)"),
              ("autodel", "Auto-delete timer"), ("ban", "Ban a user"), ("unban", "Unban a user"),
              ("admins", "List admins"), ("restart", "Restart the bot"), ("adminhelp", "All admin commands")]


async def set_bot_commands(ctx, client) -> None:
    try:
        await client.set_bot_commands([BotCommand(c, d) for c, d in PUBLIC_COMMANDS], scope=BotCommandScopeDefault())
        admin_cmds = [BotCommand(c, d) for c, d in PUBLIC_COMMANDS + ADMIN_MENU]
        for uid in set(ctx.cfg.all_admin_ids) | set(ctx.extra_admins):
            try:
                await client.set_bot_commands(admin_cmds, scope=BotCommandScopeChat(uid))
            except RPCError:   # admin never opened the bot yet
                pass
    except Exception as e:  # noqa: BLE001 - cosmetic; never block startup
        log.warning("Could not set the bot command menu: %s", e)


ADMIN_EXTRA = {"ban": cmd_ban, "unban": cmd_unban, "banned": cmd_banned, "admins": cmd_admins,
               "users": cmd_users, "restart": cmd_restart, "autodel": cmd_autodel, "setmap": cmd_setmap,
               "unmap": cmd_unmap, "maplist": cmd_maplist, "manage": cmd_manage}
OWNER_EXTRA = {"addadmin": cmd_addadmin, "rmadmin": cmd_rmadmin}   # checked inside (owner only)
PUBLIC_EXTRA = {"ping": cmd_ping, "ongoing": cmd_ongoing, "search": cmd_search}

EXTRA_HELP = """
<b>Extra tools</b>
/manage – settings panel · /autodel &lt;seconds&gt; – delete delivered files after a delay
/ban &lt;id&gt; [reason] · /unban &lt;id&gt; · /banned · /users · /admins · /restart
/addadmin &lt;id&gt; · /rmadmin &lt;id&gt; – owner only
/setmap &lt;channel id&gt; &lt;anilist id&gt; · /unmap &lt;channel id&gt; [anilist id] · /maplist – extra per-anime channels
Public: /ongoing (today's schedule) · /search &lt;name&gt; · /ping
"""


def register(app: Client, ctx) -> None:
    from .admin import guard
    for name, fn in ADMIN_EXTRA.items():
        app.add_handler(MessageHandler(bind(guard(fn), ctx), filters.command(name)))
    for name, fn in OWNER_EXTRA.items():
        app.add_handler(MessageHandler(bind(guard(fn), ctx), filters.command(name)))
    for name, fn in PUBLIC_EXTRA.items():
        app.add_handler(MessageHandler(bind(fn, ctx), filters.command(name) & filters.private))
    app.add_handler(CallbackQueryHandler(bind(manage_callback, ctx), filters.regex(r"^mng:")))
    app.add_handler(CallbackQueryHandler(bind(search_callback, ctx), filters.regex(r"^srch:")))
