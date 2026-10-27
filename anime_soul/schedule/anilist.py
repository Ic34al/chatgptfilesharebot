"""AniList GraphQL schedule provider (https://docs.anilist.co).

Respects AniList's 90 req/min limit (30 req/min when degraded) with a local
token bucket plus Retry-After handling on HTTP 429.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Sequence

import aiohttp

from ..timeutil import from_unix
from .base import AiringSlot, AnimeInfo, ScheduleError, ScheduleProvider

log = logging.getLogger(__name__)
ENDPOINT = "https://graphql.anilist.co"

AIRING_QUERY = """
query ($ids: [Int], $start: Int, $end: Int, $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    airingSchedules(mediaId_in: $ids, airingAt_greater: $start, airingAt_lesser: $end, sort: TIME) {
      id episode airingAt mediaId
    }
  }
}"""

MEDIA_FIELDS = """
  id idMal format status season seasonYear episodes synonyms
  title { romaji english }
  coverImage { extraLarge large }
  nextAiringEpisode { episode airingAt }
"""

INFO_QUERY = """
query ($ids: [Int], $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(id_in: $ids, type: ANIME) {
      %s
      relations { edges { relationType node { type %s } } }
    }
  }
}""" % (MEDIA_FIELDS, MEDIA_FIELDS)

SEARCH_QUERY = """
query ($q: String, $n: Int) {
  Page(page: 1, perPage: $n) {
    media(search: $q, type: ANIME, sort: [SEARCH_MATCH]) { %s }
  }
}""" % MEDIA_FIELDS


class _RateLimiter:
    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(1, per_minute)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


def parse_media(m: dict) -> AnimeInfo:
    nxt = m.get("nextAiringEpisode") or {}
    cover = m.get("coverImage") or {}
    info = AnimeInfo(
        anime_id=int(m["id"]),
        title_romaji=(m.get("title") or {}).get("romaji") or f"Anime {m['id']}",
        title_english=(m.get("title") or {}).get("english"),
        synonyms=list(m.get("synonyms") or []),
        status=m.get("status"), format=m.get("format"), season=m.get("season"),
        season_year=m.get("seasonYear"), episodes=m.get("episodes"), id_mal=m.get("idMal"),
        cover_url=cover.get("extraLarge") or cover.get("large"),
        next_episode=nxt.get("episode"),
    )
    for edge in ((m.get("relations") or {}).get("edges") or []):
        node = edge.get("node") or {}
        if edge.get("relationType") == "SEQUEL" and node.get("type") == "ANIME":
            info.sequels.append(parse_media(node))
    return info


def parse_airing(payload: dict) -> List[AiringSlot]:
    page = ((payload.get("data") or {}).get("Page") or {})
    out = []
    for row in page.get("airingSchedules") or []:
        try:
            out.append(AiringSlot(anime_id=int(row["mediaId"]), episode=int(row["episode"]),
                                  airing_at=from_unix(row["airingAt"]), source="anilist"))
        except (KeyError, TypeError, ValueError):
            log.warning("Skipping malformed AniList airing row: %r", row)
    return out


class AniListProvider(ScheduleProvider):
    name = "anilist"

    def __init__(self, session: Optional[aiohttp.ClientSession] = None, per_minute: int = 25,
                 max_attempts: int = 4):
        self._session = session
        self._owns_session = session is None
        self.limiter = _RateLimiter(per_minute)
        self.max_attempts = max_attempts

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                                  headers={"User-Agent": "AnimeSoulBot/1.0"})
        return self._session

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def query(self, query: str, variables: dict) -> dict:
        last_err: Exception | None = None
        for attempt in range(self.max_attempts):
            await self.limiter.wait()
            try:
                sess = await self._sess()
                async with sess.post(ENDPOINT, json={"query": query, "variables": variables},
                                     headers={"Accept": "application/json"}) as resp:
                    if resp.status == 429:
                        retry = int(resp.headers.get("Retry-After", "60") or 60)
                        log.warning("AniList rate limited; sleeping %ss", retry)
                        await asyncio.sleep(min(retry, 120))
                        last_err = ScheduleError("rate limited")
                        continue
                    if resp.status >= 500:
                        last_err = ScheduleError(f"AniList HTTP {resp.status}")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json(content_type=None)
                    if resp.status != 200 or data.get("errors"):
                        raise ScheduleError(f"AniList error {resp.status}: {str(data.get('errors'))[:300]}")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = ScheduleError(f"AniList network error: {e}")
                await asyncio.sleep(2 ** attempt)
        raise last_err or ScheduleError("AniList request failed")

    async def airing_window(self, anime_ids: Sequence[int], start: datetime, end: datetime) -> List[AiringSlot]:
        ids = sorted({int(i) for i in anime_ids})
        slots: List[AiringSlot] = []
        for i in range(0, len(ids), 50):
            chunk, page = ids[i:i + 50], 1
            while page <= 10:
                data = await self.query(AIRING_QUERY, {"ids": chunk, "start": int(start.timestamp()) - 1,
                                                       "end": int(end.timestamp()) + 1, "page": page})
                slots.extend(parse_airing(data))
                if not (((data.get("data") or {}).get("Page") or {}).get("pageInfo") or {}).get("hasNextPage"):
                    break
                page += 1
        return slots

    async def anime_info(self, anime_ids: Sequence[int]) -> Dict[int, AnimeInfo]:
        ids = sorted({int(i) for i in anime_ids})
        out: Dict[int, AnimeInfo] = {}
        for i in range(0, len(ids), 50):
            data = await self.query(INFO_QUERY, {"ids": ids[i:i + 50], "page": 1})
            for m in ((data.get("data") or {}).get("Page") or {}).get("media") or []:
                info = parse_media(m)
                out[info.anime_id] = info
        return out

    async def search(self, query: str, limit: int = 5) -> List[AnimeInfo]:
        data = await self.query(SEARCH_QUERY, {"q": query[:100], "n": max(1, min(limit, 10))})
        return [parse_media(m) for m in ((data.get("data") or {}).get("Page") or {}).get("media") or []]
