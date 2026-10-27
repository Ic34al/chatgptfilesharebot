"""Release monitor: polls schedule providers and maintains episode state."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import timedelta
from typing import Optional


from . import models as M
from .config import Config
from .db import AnimeDB
from .matcher import matches_episode
from .schedule import AniListProvider, JikanProvider, ScheduleError
from .tg import Notifier, esc
from .timeutil import fmt_ist, utcnow

log = logging.getLogger(__name__)

_SEASON_RES = (re.compile(r"\bseason\s*(\d{1,2})\b", re.I),
               re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", re.I),
               re.compile(r"\bs(\d{1,2})\b", re.I))


def guess_season(info) -> int:
    """Season number from AniList titles ("… Season 2", "… 2nd Season", "… S2"); 1 if not stated."""
    for title in (info.title_english, info.title_romaji, *(info.synonyms or [])):
        for rx in _SEASON_RES:
            m = rx.search(title or "")
            if m and 1 <= int(m.group(1)) <= 30:
                return int(m.group(1))
    return 1


class ReleaseMonitor:
    def __init__(self, cfg: Config, db: AnimeDB, notifier: Notifier,
                 anilist: Optional[AniListProvider] = None, jikan: Optional[JikanProvider] = None,
                 sources=None):
        self.cfg = cfg
        self.sources = sources
        self.db = db
        self.notifier = notifier
        self.anilist = anilist or AniListProvider()
        self.jikan = jikan if jikan is not None else (JikanProvider() if cfg.jikan_fallback else None)

    async def poll_once(self) -> dict:
        """One monitor cycle. Returns counters (useful for tests and /status)."""
        stats = {"new": 0, "rescheduled": 0, "aired": 0, "delayed": 0, "sequels": 0, "errors": 0, "feed_new": 0,
                 "auto_discovered": 0}
        anime = await self.db.list_anime(active_only=True)
        now = utcnow()
        if getattr(self.cfg, "auto_discover_airing_anime", True):
            # Automatic discovery: schedule data only. Video files still come exclusively from
            # authorized sources (/sources, /attach); nothing here creates a download URL.
            try:
                discovered = await self.anilist.discover_releasing(getattr(self.cfg, "auto_discover_max_pages", 4))
                added = 0
                for info in discovered:
                    existing = await self.db.get_anime(info.anime_id)
                    if existing:
                        continue          # already tracked, or removed with /removeanime (stays removed)
                    data = info.to_doc()
                    data.update({
                        "next_episode": info.next_episode,
                        "quality": self.cfg.default_quality,
                        "language": self.cfg.default_language,
                        "season_number": guess_season(info),
                        "aliases": [],
                        "auto_discovered": True,
                    })
                    await self.db.upsert_anime(info.anime_id, data)
                    added += 1
                stats["auto_discovered"] = added
                if added:
                    log.info(
                        "Auto-discovered %s currently releasing anime",
                        added,
                    )
                    anime = await self.db.list_anime(active_only=True)
            except ScheduleError as exc:
                stats["errors"] += 1
                log.warning(
                    "Automatic anime discovery failed: %s",
                    exc,
                )
        if anime:
            ids = [a["_id"] for a in anime]
            start = now - timedelta(hours=self.cfg.lookback_hours)
            end = now + timedelta(hours=self.cfg.lookahead_hours)
            slots = []
            try:
                slots = await self.anilist.airing_window(ids, start, end)
                await self.db.set_setting("monitor:last_ok", now)
                await self._refresh_info(ids, stats)
            except ScheduleError as e:
                stats["errors"] += 1
                log.warning("Primary schedule provider failed: %s", e)
                await self.db.set_setting("monitor:last_error", {"at": now, "error": str(e)[:300]})
                await self.notifier.alert(f"⚠️ AniList schedule check failed: {esc(e)}\nUsing fallback if available.",
                                          key="anilist-down")
                if self.jikan:
                    items = [{"anime_id": a["_id"], "id_mal": a.get("id_mal"),
                              "next_episode": a.get("next_episode")} for a in anime]
                    slots = await self.jikan.estimate(items, now, end)
            for s in slots:
                result = await self.db.upsert_schedule(s.anime_id, s.episode, s.airing_at, s.source, s.estimated)
                if result in stats:
                    stats[result] += 1
                if result == "rescheduled":
                    title = next((a.get("title_romaji") for a in anime if a["_id"] == s.anime_id), s.anime_id)
                    await self.notifier.alert(
                        f"🕒 Rescheduled: <b>{esc(title)}</b> episode {s.episode} → {fmt_ist(s.airing_at)}",
                        key=f"resched:{s.anime_id}:{s.episode}:{int(s.airing_at.timestamp())}")
        if anime and self.sources is not None and self.cfg.feed_detect_releases:
            await self._detect_from_feeds(anime, now, stats)
        aired = await self.db.mark_aired(now)
        stats["aired"] = len(aired)
        stats["delayed"] = await self._check_delays(now)
        await self.db.heartbeat("monitor")
        await self.db.set_setting("monitor:last_stats", {"at": now, **stats})
        return stats

    async def _detect_from_feeds(self, anime, now, stats) -> None:
        """Authorized feeds are also release signals: a new feed item for a tracked anime creates
        the episode even if AniList has no airing slot for it (e.g. simulcast delays, specials)."""
        from .sources.base import SourceUnavailable
        oldest = now - timedelta(hours=self.cfg.lookback_hours)
        for ad in await self.sources.feed_adapters():
            try:
                releases = await ad.releases()
            except SourceUnavailable as e:
                stats["errors"] += 1
                log.warning("Feed %s unavailable during release check: %s", ad.name, e)
                continue
            for rel in releases:
                released = rel.get("released_at") or now
                if released < oldest:
                    continue          # never backfill a whole catalogue
                for a in anime:
                    try:
                        ok = matches_episode(a, int(rel["episode"]), title=rel.get("title"),
                                             anime_id=rel.get("anilist_id"), ep=rel.get("episode"),
                                             quality=rel.get("quality"), language=rel.get("language"),
                                             season=rel.get("season"))
                    except (TypeError, ValueError):
                        ok = False
                    if not ok:
                        continue
                    res = await self.db.upsert_schedule(a["_id"], int(rel["episode"]), min(released, now),
                                                        f"feed:{ad.name}", estimated=True)
                    if res == "new":
                        stats["feed_new"] += 1
                        log.info("Feed %s announced %s episode %s", ad.name, a["_id"], rel["episode"])
                    break

    async def _refresh_info(self, ids, stats) -> None:
        try:
            infos = await self.anilist.anime_info(ids)
        except ScheduleError as e:
            log.info("Metadata refresh skipped: %s", e)
            return
        for anime_id, info in infos.items():
            doc = info.to_doc()
            doc["next_episode"] = info.next_episode
            await self.db.anime.update_one({"_id": anime_id}, {"$set": doc})
            if self.cfg.auto_track_sequels:
                parent = await self.db.get_anime(anime_id)
                for seq in info.sequels:
                    if seq.status not in ("RELEASING", "NOT_YET_RELEASED"):
                        continue
                    if seq.format not in (None, "TV", "TV_SHORT", "ONA"):
                        continue
                    if await self.db.get_anime(seq.anime_id):
                        continue
                    data = seq.to_doc()
                    data.update({"auto_added_from": anime_id, "next_episode": seq.next_episode,
                                 "quality": (parent or {}).get("quality"), "language": (parent or {}).get("language"),
                                 "season_number": ((parent or {}).get("season_number") or 1) + 1,
                                 "aliases": []})
                    await self.db.upsert_anime(seq.anime_id, data)
                    stats["sequels"] += 1
                    await self.notifier.alert(
                        f"🆕 Auto-tracking new season <b>{esc(seq.title_romaji)}</b> (AniList {seq.anime_id}), "
                        f"sequel of {esc(info.title_romaji)}. Use /removeanime {seq.anime_id} to stop.")

    async def _check_delays(self, now) -> int:
        threshold = now - timedelta(minutes=self.cfg.delay_alert_minutes)
        docs = await self.db.episodes.find({"state": M.AIRING, "airing_at": {"$lte": threshold},
                                            "delay_alerted": {"$ne": True}}).to_list(length=200)
        quiet = 0
        for d in docs:
            anime = await self.db.get_anime(d["anime_id"]) or {}
            if anime.get("auto_discovered"):
                quiet += 1        # one summary below instead of one DM per episode
            else:
                await self.notifier.alert(
                    f"⏳ <b>{esc(anime.get('title_romaji', d['anime_id']))}</b> episode {d['episode']} aired at "
                    f"{fmt_ist(d['airing_at'])} but no authorized file has been found yet.\n"
                    f"Key: <code>{esc(d['_id'])}</code>. Attach one with /attach or check /sources.")
            await self.db.episodes.update_one({"_id": d["_id"]}, {"$set": {"delay_alerted": True}})
        if quiet:
            await self.notifier.alert(
                f"⏳ {quiet} episode(s) of auto-discovered anime aired, but no authorized file has been found. "
                "They stay queued (nothing is downloaded or posted). See /queue; add a source with /sources.",
                key="delay-auto-discovered")
        # Give up after SOURCE_GIVEUP_HOURS with a final alert.
        giveup = now - timedelta(hours=self.cfg.source_giveup_hours)
        stale = await self.db.episodes.find({"state": M.AIRING, "airing_at": {"$lte": giveup}}).to_list(length=200)
        quiet_ids = []
        for d in stale:
            await self.db.set_state(d["_id"], M.FAILED, last_error="No authorized source within give-up window")
            if (await self.db.get_anime(d["anime_id"]) or {}).get("auto_discovered"):
                quiet_ids.append(d["_id"])
                continue
            await self.notifier.alert(f"❌ Gave up waiting for <code>{esc(d['_id'])}</code> "
                                      f"(no authorized file after {self.cfg.source_giveup_hours}h). Use /retry to resume.")
        if quiet_ids:
            await self.notifier.alert(
                f"❌ Gave up on {len(quiet_ids)} auto-discovered episode(s) with no authorized file after "
                f"{self.cfg.source_giveup_hours}h (e.g. <code>{esc(quiet_ids[0])}</code>). Use /retry to resume.",
                key="giveup-auto-discovered")
        return len(docs)

    async def run(self, stop: asyncio.Event) -> None:
        log.info("Release monitor started (interval %ss)", self.cfg.poll_interval_seconds)
        backoff = 5
        ttl = max(120, self.cfg.poll_interval_seconds * 3)
        while not stop.is_set():
            try:
                if not await self.db.acquire_lock("monitor", ttl):
                    holder = await self.db.lock_holder("monitor")
                    log.info("Another release monitor is active (%s); this one stays idle", holder)
                    await self._wait(stop, self.cfg.poll_interval_seconds)
                    continue
                stats = await self.poll_once()
                log.info("Monitor cycle: %s", stats)
                backoff = 5
                wait = self.cfg.poll_interval_seconds
            except Exception as e:  # DB outage, unexpected error: keep the loop alive
                log.exception("Monitor cycle failed: %s", e)
                wait = backoff
                backoff = min(backoff * 2, 300)
            await self._wait(stop, wait)
        with contextlib.suppress(Exception):
            await self.db.release_lock("monitor")

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
