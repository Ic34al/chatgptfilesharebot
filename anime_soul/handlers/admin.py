"""Administrator commands. Every handler re-checks ctx.is_admin (defence in depth)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil

from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait, InputUserDeactivated, PeerIdInvalid, RPCError, UserIsBlocked
from pyrogram.handlers import MessageHandler

from .. import models as M
from .. import processing
from ..logging_setup import redact
from ..sources.base import SourceItem
from ..uploader import upload_episode
from ..captions import DEFAULT_TEMPLATE, PLACEHOLDERS, TemplateError, caption_values, render, validate_template
from ..deeplink import build_link
from ..netsafe import UnsafeURL
from ..publisher import episode_keyboard
from ..schedule import ScheduleError
from ..sources import build_adapter
from ..tg import call_with_flood, esc
from ..timeutil import ensure_utc, fmt_ist, utcnow
from .user import bind

log = logging.getLogger(__name__)
HTML = enums.ParseMode.HTML

ADMIN_COMMANDS = ["status", "queue", "anime", "addanime", "removeanime", "setanime", "pending", "retry", "pause",
                  "resume",
                  "broadcast", "captions", "sources", "testupload", "attach", "setthumb", "dryrun", "stats",
                  "adminhelp"]

ADMIN_HELP = """<b>ANIME SOUL admin commands</b>
/status – monitor, worker, FFmpeg &amp; source health
/queue – queued / processing episodes + next upcoming (IST)
/anime [page] – tracked anime with next episode time (IST); 🛰 = auto-discovered
/addanime &lt;AniList id or name&gt; [season=2 quality=1080p language=Hindi]
/removeanime &lt;id&gt;
/setanime &lt;id&gt; season=2 quality=720p language=… aliases=A;B cover=on|off
/pending – pending/failed jobs and upcoming episodes
/retry &lt;anilist:ID:EP | ID EP&gt; [publish]  (IDs are shown by /queue, e.g. anilist:154587:5)
/pause · /resume · /dryrun on|off
/attach &lt;id&gt; &lt;episode&gt; [quality] – reply to an authorized video/file
/setthumb &lt;id&gt; – reply to a photo (or: /setthumb &lt;id&gt; none)
/captions – list · show/set/preview/reset &lt;id|default&gt;
/sources – list · add_rss · add_feed · add_dir · enable · disable · remove · check
/testupload [full] [publish [live]] – reply to a sample video you own
   full = download → FFmpeg check → upload (tests the real pipeline path)
   publish = TEST post to TEST_CHANNEL_ID (add "live" to post in the main channel)
/broadcast – reply to a message
/stats – stored share-link files
/id – reply to a message forwarded from a channel to get its numeric ID
/help – user help (+ this list for admins)"""


def _join_limit(lines, limit: int = 4000) -> str:
    """Join whole lines up to Telegram's limit (never cuts inside an HTML tag)."""
    out, size = [], 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 > limit - 40:
            out.append(f"… and {len(lines) - i} more")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def _args(message) -> list[str]:
    text = message.text or message.caption or ""
    try:
        return shlex.split(text)[1:]
    except ValueError:
        return text.split()[1:]


def _kv(args):
    pos, kv = [], {}
    for a in args:
        if "=" in a and not a.startswith("http"):
            k, v = a.split("=", 1)
            kv[k.strip().lower()] = v.strip()
        else:
            pos.append(a)
    return pos, kv


def _age(dt) -> str:
    if not dt:
        return "never"
    secs = int((utcnow() - ensure_utc(dt)).total_seconds())
    return f"{secs}s ago" if secs < 120 else f"{secs // 60}m ago" if secs < 7200 else f"{secs // 3600}h ago"


def _episode_key_from(args):
    if not args:
        raise ValueError("Give an episode key like anilist:21:1150 or: 21 1150")
    if args[0].startswith("anilist:"):
        M.parse_episode_key(args[0])
        return args[0], args[1:]
    if len(args) >= 2 and args[0].isdigit() and args[1].isdigit():
        return M.episode_key(int(args[0]), int(args[1])), args[2:]
    raise ValueError("Give an episode key like anilist:21:1150 or: 21 1150")


# ---------------------------------------------------------------- commands
async def cmd_adminhelp(ctx, client, m):
    await m.reply_text(ADMIN_HELP, parse_mode=HTML)


async def cmd_status(ctx, client, m):
    counts = await ctx.db.count_by_state()
    hb_mon = await ctx.db.get_setting("heartbeat:monitor") or {}
    hb_pipe = await ctx.db.get_setting("heartbeat:pipeline") or {}
    last = await ctx.db.get_setting("monitor:last_stats") or {}
    err = await ctx.db.get_setting("monitor:last_error") or {}
    dry = await ctx.db.get_setting("dry_run", None)
    dry = ctx.cfg.dry_run if dry is None else dry
    lines = [
        "<b>ANIME SOUL status</b>",
        f"Role: <code>{ctx.cfg.role}</code> · Paused: <b>{'yes' if await ctx.db.is_paused() else 'no'}</b>"
        f" · Dry run: <b>{'on' if dry else 'off'}</b>",
        f"Monitor heartbeat: {_age(hb_mon.get('at'))} · Pipeline heartbeat: {_age(hb_pipe.get('at'))}",
        f"Last monitor cycle: {esc({k: v for k, v in last.items() if k != 'at'})}",
        f"Tracked anime: {len(await ctx.db.list_anime())}",
        "Episodes: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v),
        f"Storage channel: {'set' if ctx.cfg.storage_channel_id else 'MISSING'} · "
        f"Post channel: {'set' if ctx.cfg.post_channel_id else 'MISSING'} · FSUB channels: {len(ctx.cfg.fsub_channels)}",
    ]
    tools = processing.tools_available()
    lines.append(f"FFmpeg: {'ok' if tools['ffmpeg'] else 'MISSING'} · FFprobe: {'ok' if tools['ffprobe'] else 'MISSING'}"
                 f" · Process mode: <code>{ctx.cfg.process_mode}</code>")
    if ctx.sources is not None:
        n = await ctx.sources.configured_count()
        lines.append(f"Authorized sources enabled: {n}" + (f" — <b>{M.SOURCE_NOT_CONFIGURED}</b>" if n == 0 else ""))
    holder = await ctx.db.lock_holder("monitor")
    lines.append(f"Monitor lock: {esc(holder) if holder else 'free'}")
    if err:
        lines.append(f"Last schedule error ({_age(err.get('at'))}): {esc(redact(str(err.get('error'))))}")
    await m.reply_text("\n".join(lines), parse_mode=HTML)


async def cmd_queue(ctx, client, m):
    active = (M.AIRING, M.AVAILABLE, M.DOWNLOADING, M.PROCESSING, M.UPLOADING, M.UPLOADED)
    rows = await ctx.db.list_by_states(active, 30)
    titles = {a["_id"]: a.get("title_romaji") for a in await ctx.db.list_anime(active_only=False)}
    if rows:
        out = ["<b>Queue</b> (use the code with /retry)"]
    else:
        out = ["Queue is empty. /pending shows failed jobs and upcoming episodes."]
    for d in rows:
        note = d.get("source_status") or ""
        if d.get("last_error"):
            note = (d.get("last_error") or "")[:90]
        nxt = f" · next {fmt_ist(d['next_attempt_at'])}" if d.get("next_attempt_at") else ""
        out.append(f"• <code>{esc(d['_id'])}</code> {esc(titles.get(d.get('anime_id'), ''))} "
                   f"E{d.get('episode')} — {esc(M.STATE_LABELS.get(d['state'], d['state']))}{nxt}"
                   + (f"\n   ↳ {esc(redact(note))}" if note else ""))
    up = await ctx.db.list_upcoming(10)
    if up:
        out.append("\n<b>Upcoming (IST)</b>")
        for d in up:
            est = " (estimated)" if d.get("estimated") else ""
            out.append(f"• {fmt_ist(d.get('airing_at'))} — {esc(titles.get(d.get('anime_id'), d.get('anime_id')))} "
                       f"E{d.get('episode')}{est}")
    await m.reply_text(_join_limit(out), parse_mode=HTML)


async def cmd_anime(ctx, client, m):
    rows = await ctx.db.list_anime()
    if not rows:
        await m.reply_text("No anime tracked yet. Use /addanime <AniList id or name>.")
        return
    args = _args(m)
    per_page = 40
    pages = (len(rows) + per_page - 1) // per_page
    page = min(max(int(args[0]) if args and args[0].isdigit() else 1, 1), pages)
    nxt = await ctx.db.next_airing_map()
    auto = sum(1 for a in rows if a.get("auto_discovered"))
    out = [f"<b>Tracked anime</b> — {len(rows)} ({auto} auto-discovered) · page {page}/{pages}"]
    for a in rows[(page - 1) * per_page: page * per_page]:
        ep = nxt.get(a["_id"])
        when = (f"E{ep['episode']} {fmt_ist(ep['airing_at'])}" if ep
                else f"next ep {a.get('next_episode') or '?'}")
        mark = "🛰 " if a.get("auto_discovered") else ""
        out.append(f"• {mark}<code>{a['_id']}</code> {esc(a.get('title_romaji'))} — S{a.get('season_number') or 1}, "
                   f"{esc(a.get('quality') or ctx.cfg.default_quality)}, {esc(when)}")
    if page < pages:
        out.append(f"\nMore: /anime {page + 1}")
    await m.reply_text(_join_limit(out), parse_mode=HTML)


async def cmd_addanime(ctx, client, m):
    pos, kv = _kv(_args(m))
    if not pos:
        await m.reply_text("Usage: /addanime <AniList id or name> [season=1 quality=1080p language=...]")
        return
    try:
        if len(pos) == 1 and pos[0].isdigit():
            info = (await ctx.anilist.anime_info([int(pos[0])])).get(int(pos[0]))
            if not info:
                await m.reply_text("No anime with that AniList id.")
                return
        else:
            results = await ctx.anilist.search(" ".join(pos), 5)
            if not results:
                await m.reply_text("No results on AniList.")
                return
            if len(results) > 1:
                await m.reply_text("Several matches — repeat with the id:\n" + "\n".join(
                    f"• <code>/addanime {r.anime_id}</code> {esc(r.title_romaji)} ({r.season_year or '?'}, "
                    f"{r.status})" for r in results), parse_mode=HTML)
                return
            info = results[0]
    except ScheduleError as e:
        await m.reply_text(f"AniList is unavailable right now: {e}")
        return
    doc = info.to_doc()
    doc.update({"next_episode": info.next_episode,
                "season_number": int(kv.get("season", 1)),
                "quality": kv.get("quality", ctx.cfg.default_quality),
                "language": kv.get("language", ctx.cfg.default_language),
                "aliases": [a for a in kv.get("aliases", "").split(";") if a]})
    new = await ctx.db.upsert_anime(info.anime_id, doc)
    await ctx.db.set_anime_active(info.anime_id, True)
    await m.reply_text(f"{'✅ Added' if new else '♻️ Updated'} <b>{esc(info.title_romaji)}</b> "
                       f"(<code>{info.anime_id}</code>), status {info.status}, next episode "
                       f"{info.next_episode or '?'}.", parse_mode=HTML)
    if ctx.monitor:
        t = asyncio.create_task(ctx.monitor.poll_once())
        ctx.background.add(t)
        t.add_done_callback(ctx.background.discard)


async def cmd_removeanime(ctx, client, m):
    a = _args(m)
    if not a or not a[0].isdigit():
        await m.reply_text("Usage: /removeanime <AniList id>")
        return
    ok = await ctx.db.set_anime_active(int(a[0]), False)
    await m.reply_text("🗑 Stopped tracking." if ok else "Not found.")


async def cmd_setanime(ctx, client, m):
    pos, kv = _kv(_args(m))
    if not pos or not pos[0].isdigit() or not kv:
        await m.reply_text("Usage: /setanime <id> season=2 quality=720p language=Hindi aliases=A;B cover=on|off")
        return
    upd = {}
    if "season" in kv:
        upd["season_number"] = int(kv["season"])
    for k in ("quality", "language"):
        if k in kv:
            upd[k] = kv[k]
    if "aliases" in kv:
        upd["aliases"] = [x for x in kv["aliases"].split(";") if x]
    if "cover" in kv:
        upd["use_cover"] = kv["cover"].lower() in ("on", "yes", "true", "1")
    res = await ctx.db.anime.update_one({"_id": int(pos[0])}, {"$set": upd})
    await m.reply_text("✅ Updated." if res.matched_count else "Not found.")


async def cmd_pending(ctx, client, m):
    rows = await ctx.db.list_pending(25)
    out = ["<b>Pending / failed</b>"] if rows else ["No pending episodes."]
    for d in rows:
        err = f" — {esc((d.get('last_error') or '')[:80])}" if d.get("last_error") else ""
        out.append(f"• <code>{d['_id']}</code> {d['state']} (try {d.get('attempts', 0)}){err}")
    up = await ctx.db.list_upcoming(8)
    if up:
        out.append("\n<b>Upcoming</b>")
        for d in up:
            est = " (estimated)" if d.get("estimated") else ""
            out.append(f"• <code>{d['_id']}</code> {fmt_ist(d.get('airing_at'))}{est}")
    await m.reply_text(_join_limit(out), parse_mode=HTML)


async def cmd_retry(ctx, client, m):
    try:
        key, rest = _episode_key_from(_args(m))
    except ValueError as e:
        await m.reply_text(str(e))
        return
    ok = await ctx.db.reset_for_retry(key, include_publish="publish" in rest)
    await m.reply_text(f"🔁 Queued <code>{key}</code> for retry." if ok else "Not found or already published.",
                       parse_mode=HTML)


async def cmd_pause(ctx, client, m):
    await ctx.db.set_setting("paused", True)
    await m.reply_text("⏸ Automatic downloading, uploading and posting paused. Schedules keep updating.")


async def cmd_resume(ctx, client, m):
    await ctx.db.set_setting("paused", False)
    await m.reply_text("▶️ Processing resumed.")


async def cmd_dryrun(ctx, client, m):
    a = _args(m)
    if not a or a[0].lower() not in ("on", "off"):
        cur = await ctx.db.get_setting("dry_run", None)
        await m.reply_text(f"Dry run is {'on' if (ctx.cfg.dry_run if cur is None else cur) else 'off'}. "
                           "Use /dryrun on|off")
        return
    await ctx.db.set_setting("dry_run", a[0].lower() == "on")
    await m.reply_text(f"🧪 Dry run {'enabled — nothing will be uploaded or posted' if a[0].lower() == 'on' else 'disabled'}.")


def _replied_media(m):
    r = m.reply_to_message or m
    for attr in ("video", "document", "animation"):
        media = getattr(r, attr, None)
        if media:
            return r, attr, media
    return r, None, None


async def cmd_attach(ctx, client, m):
    """Register an admin-provided authorized file for an episode."""
    a = _args(m)
    src, media_type, media = _replied_media(m)
    if len(a) < 2 or not a[0].isdigit() or not a[1].isdigit() or not media:
        await m.reply_text("Reply to an authorized video/file with: /attach <AniList id> <episode> [quality]")
        return
    anime_id, ep = int(a[0]), int(a[1])
    anime = await ctx.db.get_anime(anime_id)
    if not anime:
        await m.reply_text("That anime is not tracked. /addanime it first.")
        return
    if (media.file_size or 0) > ctx.cfg.max_file_size_bytes:
        await m.reply_text("File is larger than the configured limit.")
        return
    key = M.episode_key(anime_id, ep)
    await ctx.db.add_inbox_file(key, {"file_id": media.file_id, "file_unique_id": media.file_unique_id,
                                      "file_name": getattr(media, "file_name", None), "file_size": media.file_size,
                                      "media_type": media_type, "quality": a[2] if len(a) > 2 else None,
                                      "language": " ".join(a[3:]) or None, "added_by": m.from_user.id,
                                      "chat_id": src.chat.id, "message_id": src.id})
    ep_doc = await ctx.db.get_episode(key)
    if not ep_doc:
        await ctx.db.upsert_schedule(anime_id, ep, utcnow(), "manual")
        await ctx.db.mark_aired()
    elif ep_doc["state"] in (M.SCHEDULED,):
        await ctx.db.set_state(key, M.AIRING, next_attempt_at=utcnow())
    elif ep_doc["state"] in (M.AIRING, M.FAILED, M.DRY_RUN_OK):
        await ctx.db.reset_for_retry(key)
    await m.reply_text(f"📥 Attached file to <code>{key}</code>. It will be uploaded and posted automatically "
                       f"(state: {(await ctx.db.get_episode(key))['state']}).", parse_mode=HTML)


async def cmd_setthumb(ctx, client, m):
    a = _args(m)
    if not a or not a[0].isdigit():
        await m.reply_text("Reply to a photo with /setthumb <id>, or /setthumb <id> none")
        return
    r = m.reply_to_message
    if len(a) > 1 and a[1].lower() == "none":
        value = None
    elif r and r.photo:
        value = r.photo.file_id
    else:
        await m.reply_text("Reply to a photo.")
        return
    res = await ctx.db.anime.update_one({"_id": int(a[0])}, {"$set": {"thumbnail_file_id": value}})
    await m.reply_text("🖼 Thumbnail updated." if res.matched_count else "Not found.")


def _sample_values(ctx, anime):
    return caption_values(anime or {"title_romaji": "Sample Anime"},
                          {"episode": 1, "airing_at": utcnow(), "storage": {"file_size": 350 * 1024 * 1024}},
                          ctx.cfg)


async def cmd_captions(ctx, client, m):
    a = _args(m)
    sub = a[0].lower() if a else "list"
    target = a[1] if len(a) > 1 else None
    if target and target != "default" and not target.isdigit():
        await m.reply_text("Target must be an AniList id or 'default'.")
        return
    if sub == "list" or not target:
        rows = await ctx.db.list_captions()
        names = ", ".join(r["_id"] for r in rows) or "none (built-in default in use)"
        await m.reply_text(
            f"<b>Caption templates:</b> {esc(names)}\n\nPlaceholders: " +
            " ".join("{" + p + "}" for p in PLACEHOLDERS) +
            "\n\n/captions show &lt;id|default&gt;\n/captions set &lt;id|default&gt; (reply to the template text)"
            "\n/captions preview &lt;id&gt;\n/captions reset &lt;id|default&gt;", parse_mode=HTML)
        return
    if sub == "show":
        tpl = (await ctx.db.captions.find_one({"_id": target}) or {}).get("template")
        await m.reply_text(f"<pre>{esc(tpl or DEFAULT_TEMPLATE)}</pre>", parse_mode=HTML)
        return
    if sub == "set":
        r = m.reply_to_message
        if not r or not (r.text or r.caption):
            await m.reply_text("Reply to a message containing the template.")
            return
        body = r.text or r.caption
        tpl = str(body) if "<" in str(body) else (getattr(body, "html", None) or str(body))
        try:
            validate_template(tpl)
            render(tpl, _sample_values(ctx, None))
        except TemplateError as e:
            await m.reply_text(f"❌ {e}")
            return
        await ctx.db.set_caption(target, tpl)
        await m.reply_text(f"✅ Caption saved for {target}. Try /captions preview {target}")
        return
    if sub == "preview":
        anime = await ctx.db.get_anime(int(target)) if target.isdigit() else None
        tpl = await ctx.db.get_caption(int(target) if target.isdigit() else None) or DEFAULT_TEMPLATE
        try:
            text = render(tpl, _sample_values(ctx, anime))
        except TemplateError as e:
            await m.reply_text(f"❌ {e}")
            return
        me = client.me or await client.get_me()
        await m.reply_text(text, parse_mode=HTML,
                           reply_markup=episode_keyboard(build_link(me.username, "ep" + "preview00000")))
        return
    if sub == "reset":
        ok = await ctx.db.delete_caption(target)
        await m.reply_text("Reset to default." if ok else "No custom template for that target.")
        return
    await m.reply_text("Unknown subcommand. Use /captions for help.")


async def cmd_sources(ctx, client, m):
    a = _args(m)
    sub = a[0].lower() if a else "list"
    text = m.text or ""
    note = text.split("|", 1)[1].strip() if "|" in text else ""
    if sub == "list":
        rows = await ctx.db.list_sources()
        out = ["<b>Sources</b>", "• admin-inbox (telegram, /attach) — always on"]
        for s in rows:
            state = "on" if s.get("enabled") else "off"
            health = "ok" if s.get("last_ok") else (esc(s.get("last_error")) or "unchecked")
            out.append(f"• <b>{esc(s['_id'])}</b> ({s.get('type')}, {state}) — {health} · licence: "
                       f"{esc((s.get('license_note') or '')[:60])}")
        out.append("\nAdd: /sources add_rss &lt;name&gt; &lt;https RSS/Atom url&gt; | &lt;licence/permission note&gt;"
                   "\n/sources add_feed &lt;name&gt; &lt;https JSON url&gt; | &lt;licence/permission note&gt;"
                   "\n/sources add_dir &lt;name&gt; &lt;path&gt; | &lt;licence note&gt;"
                   "\n/sources enable|disable|remove &lt;name&gt; · /sources check")
        await m.reply_text("\n".join(out)[:4000], parse_mode=HTML)
        return
    if sub in ("add_feed", "add_rss", "add_dir"):
        if len(a) < 3 or not note:
            await m.reply_text("You must include a licence/permission note after '|' confirming you are "
                               "authorized to download and redistribute files from this source.")
            return
        name, target = a[1], a[2].split("|")[0].strip()
        doc = {"_id": name, "type": {"add_feed": "http_feed", "add_rss": "rss_feed"}.get(sub, "local_dir"),
               "config": {"path": target} if sub == "add_dir" else {"url": target},
               "authorized": True, "license_note": note[:500], "enabled": True, "added_by": m.from_user.id}
        if len(a) > 3 and a[3].startswith("auth_env="):
            doc["config"]["auth_header_env"] = a[3].split("=", 1)[1]
        try:
            build_adapter(doc, ctx.cfg)
        except (ValueError, UnsafeURL) as e:
            await m.reply_text(f"❌ {e}")
            return
        await ctx.db.upsert_source(name, {k: v for k, v in doc.items() if k != "_id"})
        await m.reply_text(f"✅ Source {name} added.")
        return
    if sub in ("enable", "disable", "remove") and len(a) > 1:
        ok = (await ctx.db.remove_source(a[1])) if sub == "remove" else \
            (await ctx.db.set_source_enabled(a[1], sub == "enable"))
        await m.reply_text("Done." if ok else "Not found.")
        return
    if sub == "check":
        out = []
        for ad in await ctx.sources.adapters():
            out.append(f"• {ad.name}: {await ad.health()}")
        await m.reply_text("\n".join(out) or "No sources.")
        return
    await m.reply_text("Unknown subcommand. Use /sources")


async def cmd_testupload(ctx, client, m):
    """Upload a sample you own to storage, create a deep link and (only if asked) a TEST post.

    /testupload            copy the replied file to storage (fast)
    /testupload full       download → FFprobe/FFmpeg → upload, i.e. the real pipeline path
    ... publish            TEST post to TEST_CHANNEL_ID; "publish live" posts to POST_CHANNEL_ID
    """
    args = [a.lower() for a in _args(m)]
    src, media_type, media = _replied_media(m)
    if not media:
        await m.reply_text("Reply to a sample video you own (or may redistribute) with /testupload [full] [publish]")
        return
    if not ctx.cfg.storage_channel_id:
        await m.reply_text("STORAGE_CHANNEL_ID is not set.")
        return
    token = M.new_file_token()
    if "full" in args:
        stored = await _testupload_full(ctx, client, m, src, media, token)
        if not stored:
            return
        file_id, unique_id, chat_id, msg_id, size = (stored["file_id"], stored.get("file_unique_id"),
                                                     stored["chat_id"], stored["message_id"], stored.get("file_size"))
    else:
        try:
            msg = await call_with_flood(client.copy_message, ctx.cfg.storage_channel_id, src.chat.id, src.id,
                                        caption="#testupload")
        except RPCError as e:
            await m.reply_text(f"❌ Upload to storage channel failed: {e}\nIs the bot an admin there?")
            return
        smedia = msg.video or msg.document or msg.animation
        file_id, unique_id, chat_id, msg_id, size = (smedia.file_id, smedia.file_unique_id, msg.chat.id, msg.id,
                                                     smedia.file_size)
    await ctx.filedb.save_file(token, file_id, unique_id, False, fsub=True, test=True,
                               storage_chat_id=chat_id, storage_message_id=msg_id)
    me = client.me or await client.get_me()
    link = build_link(me.username, token)
    await m.reply_text(f"✅ Stored in storage channel (message {msg_id}, {size} bytes).\n"
                       f"Test link (FSUB enforced): {link}", disable_web_page_preview=True)
    if "publish" not in args:
        return
    target = ctx.cfg.test_channel_id
    if target is None:
        if "live" not in args:
            await m.reply_text("Not posted. Set TEST_CHANNEL_ID for test posts, or use "
                               "/testupload publish live to post in the main ANIME SOUL channel.")
            return
        target = ctx.cfg.post_channel_id
    if target is None:
        await m.reply_text("POST_CHANNEL_ID is not set.")
        return
    text = render(DEFAULT_TEMPLATE, _sample_values(ctx, None))
    try:
        await call_with_flood(client.send_message, target, "🧪 TEST POST\n\n" + text,
                              parse_mode=HTML, reply_markup=episode_keyboard(link))
        where = "test channel" if target == ctx.cfg.test_channel_id else "main post channel"
        await m.reply_text(f"📣 Test post published to the {where}.")
    except RPCError as e:
        await m.reply_text(f"❌ Posting failed: {e}")


async def _testupload_full(ctx, client, m, src, media, token):
    work = os.path.join(ctx.cfg.download_dir, f"testupload_{token}")
    status = await m.reply_text("⬇️ Downloading sample from Telegram…")
    try:
        processing.ensure_disk(work, int(getattr(media, "file_size", 0) or 0) * 2, ctx.cfg.disk_headroom_mb)
        name = getattr(media, "file_name", None) or f"sample_{token}.mp4"
        path = await src.download(file_name=os.path.join(work, "src_" + os.path.basename(name)))
        await status.edit_text("🎞 Validating with FFprobe / FFmpeg…")
        result = await processing.process(path, work, ctx.cfg, base_name="testupload")
        i = result.info
        await status.edit_text(f"⬆️ Uploading ({result.action}; {i.video_codec} {i.width}x{i.height}, "
                               f"{i.duration:.0f}s)…")
        item = SourceItem("testupload", "local", path, os.path.basename(name), size=os.path.getsize(result.path),
                          quality=i.quality, owned=True)
        stored = await upload_episode(client, ctx.cfg.storage_channel_id, f"test:{token}",
                                      {"_id": 0, "title_romaji": "TEST UPLOAD"}, 0, item, result.path,
                                      timeout_s=ctx.cfg.upload_timeout_seconds, container=result.container,
                                      media=i.to_doc())
        await status.edit_text(f"✅ Full pipeline test passed: {result.action}, {i.quality or '?'}, "
                               f"{i.duration:.0f}s.")
        return stored
    except Exception as e:
        await status.edit_text(f"❌ Full test failed: {type(e).__name__}: {redact(str(e))[:300]}")
        return None
    finally:
        real = os.path.realpath(work)
        if real.startswith(os.path.realpath(ctx.cfg.download_dir) + os.sep):
            with contextlib.suppress(Exception):
                shutil.rmtree(real)


async def cmd_broadcast(ctx, client, m):
    if not m.reply_to_message:
        await m.reply_text("Reply to the message you want to broadcast.")
        return
    status = await m.reply_text("📢 Broadcasting…")

    async def run():
        ok = blocked = failed = 0
        async for uid in ctx.db.iter_user_ids():
            try:
                await call_with_flood(m.reply_to_message.copy, uid)
                ok += 1
            except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
                blocked += 1
                await ctx.db.mark_blocked(uid)
            except (RPCError, FloodWait):
                failed += 1
            await asyncio.sleep(0.05)
        try:
            await status.edit_text(f"✅ Broadcast done. Sent: {ok} · Blocked/removed: {blocked} · Failed: {failed}")
        except RPCError:
            pass

    t = asyncio.create_task(run())
    ctx.background.add(t)
    t.add_done_callback(ctx.background.discard)


async def cmd_id(ctx, client, m):
    r = m.reply_to_message
    chat = getattr(r, "forward_from_chat", None) if r else None
    if chat:
        await m.reply_text(f"Channel <b>{esc(chat.title)}</b> ID: <code>{chat.id}</code>", parse_mode=HTML)
    else:
        await m.reply_text(f"Your user ID: <code>{m.from_user.id}</code>\nThis chat: <code>{m.chat.id}</code>\n"
                           "Forward a post from a channel to me, then reply to it with /id.", parse_mode=HTML)


async def cmd_stats(ctx, client, m):
    # Original /stats behaviour (owner) — admins allowed too.
    await m.reply_text(f"📊 Stored files: `{await ctx.filedb.count_files()}`")


COMMANDS = {name: globals()[f"cmd_{name}"] for name in ADMIN_COMMANDS}


def guard(fn):
    async def _wrapped(ctx, client, m):
        uid = m.from_user.id if m.from_user else None
        if not ctx.is_admin(uid):
            log.warning("Non-admin %s tried an admin command", uid)
            await m.reply_text("⛔ This command is only for ANIME SOUL administrators.")
            return
        try:
            await fn(ctx, client, m)
        except Exception as e:
            log.exception("Admin command failed")
            await m.reply_text(f"⚠️ Command failed: {type(e).__name__}: {redact(str(e))[:300]}")
    _wrapped.__name__ = fn.__name__
    return _wrapped


def register(app: Client, ctx) -> None:
    for name, fn in COMMANDS.items():
        app.add_handler(MessageHandler(bind(guard(fn), ctx), filters.command(name)))
    # /id is public: you need your own user ID before you can set OWNER_ID.
    app.add_handler(MessageHandler(bind(cmd_id, ctx), filters.command("id") & filters.private))
