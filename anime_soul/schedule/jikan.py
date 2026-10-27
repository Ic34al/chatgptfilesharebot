"""Jikan (MyAnimeList) fallback provider.

Jikan only exposes a weekly broadcast slot (day + JST time), not per-episode
air times, so slots produced here are flagged ``estimated=True`` and are
never allowed to overwrite a confirmed AniList time.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

import aiohttp

from ..timeutil import JST, ensure_utc
from .base import AiringSlot, ScheduleError, ScheduleProvider

log = logging.getLogger(__name__)
DAYS = {"mondays": 0, "tuesdays": 1, "wednesdays": 2, "thursdays": 3, "fridays": 4, "saturdays": 5, "sundays": 6}


def next_broadcast(broadcast: dict, after: datetime) -> Optional[datetime]:
    """Compute the next weekly broadcast (UTC) from Jikan's broadcast object."""
    day = DAYS.get((broadcast.get("day") or "").strip().lower())
    time_s = broadcast.get("time")
    if day is None or not time_s:
        return None
    try:
        hh, mm = (int(x) for x in time_s.split(":")[:2])
    except ValueError:
        return None
    local_after = ensure_utc(after).astimezone(JST)
    candidate = local_after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    candidate += timedelta(days=(day - candidate.weekday()) % 7)
    if candidate <= local_after:
        candidate += timedelta(days=7)
    return candidate.astimezone(ensure_utc(after).tzinfo)


class JikanProvider(ScheduleProvider):
    name = "jikan"

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._owns = session is None

    async def _sess(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self):
        if self._owns and self._session and not self._session.closed:
            await self._session.close()

    async def broadcast(self, mal_id: int) -> dict:
        sess = await self._sess()
        await asyncio.sleep(0.5)  # Jikan allows ~3 req/s; stay well below
        try:
            async with sess.get(f"https://api.jikan.moe/v4/anime/{int(mal_id)}") as r:
                if r.status == 429:
                    raise ScheduleError("Jikan rate limited")
                if r.status != 200:
                    raise ScheduleError(f"Jikan HTTP {r.status}")
                data = (await r.json(content_type=None)).get("data") or {}
                return {"broadcast": data.get("broadcast") or {}, "airing": data.get("airing")}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise ScheduleError(f"Jikan network error: {e}")

    async def estimate(self, items: Sequence[dict], start: datetime, end: datetime) -> List[AiringSlot]:
        """items: [{anime_id, id_mal, next_episode}] -> estimated slots inside the window."""
        out = []
        for it in items:
            if not it.get("id_mal") or not it.get("next_episode"):
                continue
            try:
                info = await self.broadcast(it["id_mal"])
            except ScheduleError as e:
                log.warning("Jikan fallback failed for %s: %s", it["anime_id"], e)
                continue
            if not info.get("airing"):
                continue
            when = next_broadcast(info["broadcast"], start - timedelta(days=7))
            while when and when < start:
                when += timedelta(days=7)
            if when and when <= end:
                out.append(AiringSlot(it["anime_id"], int(it["next_episode"]), when, "jikan", estimated=True))
        return out

    async def airing_window(self, anime_ids: Sequence[int], start: datetime, end: datetime) -> List[AiringSlot]:
        raise NotImplementedError("Use estimate(); Jikan needs MAL ids and episode numbers")
