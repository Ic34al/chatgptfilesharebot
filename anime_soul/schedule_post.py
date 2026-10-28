"""Daily airing timetable (IST).

Design pattern adapted from kaif-00z/AutoAnimeBot (functions/schedule.py, GPL-3.0): post the day's
airing list once a day shortly after midnight IST. This is an independent reimplementation that uses
the bot's own AniList schedule data in MongoDB; no code was copied.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta

from pymongo.errors import DuplicateKeyError
from pyrogram import enums

from . import models as M
from .tg import call_with_flood, esc
from .timeutil import IST, ensure_utc, utcnow

log = logging.getLogger(__name__)


def ist_day_bounds(now=None):
    now_ist = ensure_utc(now or utcnow()).astimezone(IST)
    start = datetime.combine(now_ist.date(), dtime(0, 0), tzinfo=IST)
    return start, start + timedelta(days=1)


async def build_schedule_text(db, now=None) -> str:
    start, end = ist_day_bounds(now)
    rows = await db.episodes.find({"airing_at": {"$gte": start, "$lt": end}}).sort("airing_at", 1).to_list(200)
    titles = {a["_id"]: a.get("title_english") or a.get("title_romaji") or str(a["_id"])
              for a in await db.list_anime(active_only=False)}
    head = f"📅 <b>Today's anime schedule</b> — {start:%A, %d %B %Y} (IST)"
    if not rows:
        return head + "\n\nNo tracked episodes are scheduled today."
    lines = [head, ""]
    for d in rows:
        t = ensure_utc(d["airing_at"]).astimezone(IST)
        done = " ✅" if d.get("state") == M.PUBLISHED else ""
        est = " (est.)" if d.get("estimated") else ""
        lines.append(f"<code>[{t:%H:%M}]</code> {esc(titles.get(d.get('anime_id'), d.get('anime_id')))} — "
                     f"Episode {d.get('episode')}{est}{done}")
    lines.append("\nTimes can change if the broadcaster delays an episode. Files are posted only after they "
                 "are available from an authorized source.")
    text = "\n".join(lines)
    return text[:4000]


async def maybe_post_schedule(cfg, db, client, notifier, dry_run: bool, now=None) -> bool:
    """Post today's timetable once per IST day after SCHEDULE_POST_TIME. Safe across dynos/restarts."""
    if not getattr(cfg, "schedule_post_enabled", False):
        return False
    now_ist = ensure_utc(now or utcnow()).astimezone(IST)
    hh, mm = (int(x) for x in cfg.schedule_post_time.split(":"))
    if (now_ist.hour, now_ist.minute) < (hh, mm):
        return False
    claim = f"schedule_post:{now_ist:%Y-%m-%d}"
    try:
        await db.settings.insert_one({"_id": claim, "at": utcnow(), "state": "posting"})
    except DuplicateKeyError:
        return False                       # already posted (or being posted) today
    text = await build_schedule_text(db, now)
    chat = cfg.schedule_channel_id or cfg.post_channel_id
    try:
        if dry_run or chat is None:
            await notifier.alert("🧪 <b>DRY RUN</b> — daily schedule post preview (not posted):\n\n" + text)
        else:
            await call_with_flood(notifier.client.send_message, chat, text, parse_mode=enums.ParseMode.HTML,
                                  disable_web_page_preview=True)
        await db.settings.update_one({"_id": claim}, {"$set": {"state": "posted", "dry_run": bool(dry_run)}})
        return True
    except Exception as e:  # noqa: BLE001 - report; allow a retry next cycle
        log.warning("Daily schedule post failed: %s", e)
        await db.settings.delete_one({"_id": claim})
        await notifier.alert(f"⚠️ Daily schedule post failed: {esc(type(e).__name__)}", key="schedule-post")
        return False
