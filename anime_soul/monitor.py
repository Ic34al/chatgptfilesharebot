"""Release monitor: polls schedule providers and maintains episode state."""
from __future__ import annotations

import asyncio
import contextlib
import logging
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
        stats = {"new": 0, "rescheduled": 0, "aired": 0, "delayed": 0, "sequels": 0, "errors": 0, "feed_new": 0}
        anime = await self.db.list_anime(active_only=True)
        now = utcnow()
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
        for d in docs:
            anime = await self.db.get_anime(d["anime_id"]) or {}
            await self.notifier.alert(
                f"⏳ <b>{esc(anime.get('title_romaji', d['anime_id']))}</b> episode {d['episode']} aired at "
                f"{fmt_ist(d['airing_at'])} but no authorized file has been found yet.\n"
                f"Key: <code>{esc(d['_id'])}</code>. Attach one with /attach or check /sources.")
            await self.db.episodes.update_one({"_id": d["_id"]}, {"$set": {"delay_alerted": True}})
        # Give up after SOURCE_GIVEUP_HOURS with a final alert.
        giveup = now - timedelta(hours=self.cfg.source_giveup_hours)
        stale = await self.db.episodes.find({"state": M.AIRING, "airing_at": {"$lte": giveup}}).to_list(length=200)
        for d in stale:
            await self.db.set_state(d["_id"], M.FAILED, last_error="No authorized source within give-up window")
            await self.notifier.alert(f"❌ Gave up waiting for <code>{esc(d['_id'])}</code> "
                                      f"(no authorized file after {self.cfg.source_giveup_hours}h). Use /retry to resume.")
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
