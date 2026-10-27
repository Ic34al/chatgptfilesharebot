"""Release-name parsing and episode matching."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional

VIDEO_EXTS = (".mkv", ".mp4", ".webm", ".avi", ".mov", ".ts", ".m4v")
_QUAL = re.compile(r"(?<!\d)(2160|1440|1080|720|576|480|360)p", re.I)
_BRACKETS = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")
_PATTERNS = [
    re.compile(r"^(?P<title>.+?)[\s._-]+S(?P<season>\d{1,2})E(?P<ep>\d{1,4})(?:v\d)?\b", re.I),
    re.compile(r"^(?P<title>.+?)[\s._]+-[\s._]+(?P<ep>\d{1,4})(?:v\d)?(?:[\s._]|$)", re.I),
    re.compile(r"^(?P<title>.+?)[\s._]+(?:Ep(?:isode)?\.?\s*)(?P<ep>\d{1,4})\b", re.I),
]


@dataclass
class ParsedRelease:
    title: str
    episode: Optional[int]
    season: Optional[int]
    quality: Optional[str]


def normalize_title(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"(?i)\b(season|s)\s*\d+\b|\b\d+(st|nd|rd|th)\s+season\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(text.split())


def parse_release_name(name: str) -> ParsedRelease:
    base = name.rsplit("/", 1)[-1]
    for ext in VIDEO_EXTS:
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    q = _QUAL.search(base)
    quality = f"{q.group(1)}p" if q else None
    cleaned = _BRACKETS.sub(" ", base).replace("_", " ").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    for pat in _PATTERNS:
        m = pat.search(cleaned)
        if m:
            season = int(m.group("season")) if "season" in m.groupdict() and m.group("season") else None
            return ParsedRelease(m.group("title").strip(" -._"), int(m.group("ep")), season, quality)
    return ParsedRelease(cleaned, None, None, quality)


def title_matches(candidate: str, names: Iterable[str], threshold: float = 0.88) -> bool:
    cand = normalize_title(candidate)
    if not cand:
        return False
    for n in names:
        target = normalize_title(n)
        if not target:
            continue
        if cand == target or SequenceMatcher(None, cand, target).ratio() >= threshold:
            return True
    return False


def anime_names(anime: dict) -> list[str]:
    names = [anime.get("title_romaji"), anime.get("title_english")]
    names += list(anime.get("synonyms") or []) + list(anime.get("aliases") or [])
    return [n for n in names if n]


def matches_episode(anime: dict, episode: int, *, title: Optional[str] = None, anime_id: Optional[int] = None,
                    ep: Optional[int] = None, quality: Optional[str] = None, language: Optional[str] = None,
                    season: Optional[int] = None) -> bool:
    """Decide whether a source item is exactly the episode we want."""
    if ep is None or int(ep) != int(episode):
        return False
    if anime_id is not None:
        if int(anime_id) != int(anime["_id"]):
            return False
    elif not title or not title_matches(title, anime_names(anime)):
        return False
    want_q = anime.get("quality")
    if want_q and quality and quality.lower() != want_q.lower():
        return False
    want_lang = anime.get("language")
    if want_lang and language and normalize_title(language) != normalize_title(want_lang):
        return False
    want_season = anime.get("season_number")
    if want_season and season and int(season) != int(want_season):
        return False
    return True
