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


_GROUP = re.compile(r"^\s*\[(?P<g>[^\]]{1,40})\]")
_CRC = re.compile(r"\[(?P<crc>[0-9A-Fa-f]{8})\]")
_VERSION = re.compile(r"(?:E|\s|-)\d{1,4}v(?P<v>\d)\b", re.I)
_BATCH = re.compile(r"(?i)\bbatch\b|\bcomplete\b|[\(\[]\s*\d{1,4}\s*[-~]\s*\d{1,4}\s*[\)\]]"
                    r"|(?<!\d)\d{1,4}\s*~\s*\d{1,4}(?!\d)|\bEp(?:isodes?)?\.?\s*\d{1,4}\s*-\s*\d{1,4}\b"
                    r"|\bS\d{1,2}E\d{1,4}\s*-\s*E?\d{1,4}\b")
_LANG_TAGS = (
    (re.compile(r"(?i)\bdual[\s._-]?audio\b"), "Dual Audio"),
    (re.compile(r"(?i)\bmulti[\s._-]?(?:audio|sub)s?\b"), "Multi"),
    (re.compile(r"(?i)\bhindi\b"), "Hindi"),
    (re.compile(r"(?i)\b(?:eng(?:lish)?[\s._-]?dub(?:bed)?|dub(?:bed)?)\b"), "English Dub"),
    (re.compile(r"(?i)\b(?:e[\s._-]?subs?|eng(?:lish)?[\s._-]?subs?|subbed)\b"), "English Sub"),
)


@dataclass
class ParsedRelease:
    title: str
    episode: Optional[int]
    season: Optional[int]
    quality: Optional[str]
    group: Optional[str] = None        # release group, e.g. "[Studio]"
    version: int = 1                   # v2 / v3 re-releases
    crc32: Optional[str] = None        # [ABCD1234] checksum tag in the name (informational only)
    language: Optional[str] = None     # tag found in the name (Dual Audio, English Sub…)
    is_batch: bool = False             # "01-12", "[Batch]", "Complete" → never treated as one episode


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
    g = _GROUP.match(base)
    crc = _CRC.search(base)
    v = _VERSION.search(base)
    lang = next((label for rx, label in _LANG_TAGS if rx.search(base)), None)
    meta = dict(group=g.group("g").strip() if g else None, version=int(v.group("v")) if v else 1,
                crc32=crc.group("crc").upper() if crc else None, language=lang,
                is_batch=bool(_BATCH.search(_CRC.sub(" ", base))))
    cleaned = _BRACKETS.sub(" ", base).replace("_", " ").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    for pat in _PATTERNS:
        m = pat.search(cleaned)
        if m:
            season = int(m.group("season")) if "season" in m.groupdict() and m.group("season") else None
            ep = None if meta["is_batch"] else int(m.group("ep"))
            return ParsedRelease(m.group("title").strip(" -._"), ep, season, quality, **meta)
    return ParsedRelease(cleaned, None, None, quality, **meta)


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
