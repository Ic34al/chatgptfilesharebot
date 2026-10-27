"""Replaceable schedule-provider interface."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence


@dataclass
class AiringSlot:
    anime_id: int            # AniList media id (canonical id across the system)
    episode: int
    airing_at: datetime      # aware UTC
    source: str
    estimated: bool = False


@dataclass
class AnimeInfo:
    anime_id: int
    title_romaji: str
    title_english: Optional[str] = None
    synonyms: List[str] = field(default_factory=list)
    status: Optional[str] = None          # RELEASING, FINISHED, NOT_YET_RELEASED, ...
    format: Optional[str] = None
    season: Optional[str] = None
    season_year: Optional[int] = None
    episodes: Optional[int] = None
    id_mal: Optional[int] = None
    cover_url: Optional[str] = None
    next_episode: Optional[int] = None
    sequels: List["AnimeInfo"] = field(default_factory=list)

    def to_doc(self) -> Dict:
        return {
            "title_romaji": self.title_romaji, "title_english": self.title_english,
            "synonyms": self.synonyms[:20], "status": self.status, "format": self.format,
            "season": self.season, "season_year": self.season_year, "total_episodes": self.episodes,
            "id_mal": self.id_mal, "cover_url": self.cover_url,
        }


class ScheduleError(Exception):
    """Temporary schedule-provider failure (network, 5xx, rate limit)."""


class ScheduleProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    async def airing_window(self, anime_ids: Sequence[int], start: datetime, end: datetime) -> List[AiringSlot]:
        ...

    async def anime_info(self, anime_ids: Sequence[int]) -> Dict[int, AnimeInfo]:
        return {}

    async def search(self, query: str, limit: int = 5) -> List[AnimeInfo]:
        return []
