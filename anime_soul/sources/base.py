"""Source-adapter interface.

A source is only used when an administrator has registered it with an
explicit authorization note (licence / permission to redistribute). The bot
never searches the open web, never bypasses DRM, logins or paywalls, and never
treats a feed entry as permission by itself: the permission comes from the
administrator who registered the source.

Every adapter exposes the same four operations so the monitoring engine and
pipeline never depend on one provider:

    health()                          -> short status string
    find(anime, episode)              -> SourceItem | None
    download(item, destination, ctx)  -> DownloadOutcome   (file on local disk)
    validate(item, path, ctx)         -> ValidationReport  (size / checksum / container)

Metadata discovery (AniList, a schedule, an RSS/Atom/JSON feed) is separate from
the source that serves the video file: a SourceItem always points at the file
that the authorized source itself provides.
"""
from __future__ import annotations

import abc
import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional


def make_release_id(source: str, *parts: Any) -> str:
    """Stable identifier for one release on one source (feed GUID, URL or path)."""
    raw = "|".join([source, *[str(p) for p in parts if p not in (None, "")]])
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:32]


@dataclass
class SourceItem:
    source: str                 # source name (as registered with /sources)
    kind: str                   # "telegram" | "local" | "url"
    ref: str                    # file_id | absolute path | URL
    file_name: str
    size: Optional[int] = None  # expected size in bytes (if the source states it)
    sha256: Optional[str] = None  # expected checksum (if the source states it)
    quality: Optional[str] = None
    language: Optional[str] = None
    owned: bool = True          # False => never delete (e.g. a file in a local library)
    extra: Dict[str, Any] = field(default_factory=dict)
    anime_id: Optional[int] = None
    episode: Optional[int] = None
    release_id: Optional[str] = None   # stable id of this release on this source
    version: int = 1                   # v2 re-release etc.

    _FIELDS = ("source", "kind", "ref", "file_name", "size", "sha256", "quality", "language",
               "anime_id", "episode", "release_id")

    @property
    def source_id(self) -> str:
        return f"{self.source}:{self.release_id or make_release_id(self.source, self.ref)}"

    def metadata(self) -> Dict[str, Any]:
        """Structured metadata returned by every adapter (safe to log: no auth headers)."""
        return {"anime_id": self.anime_id, "episode": self.episode, "filename": self.file_name,
                "expected_size": self.size, "checksum": self.sha256, "checksum_type": "sha256" if self.sha256 else None,
                "quality": self.quality, "language": self.language, "source": self.source,
                "source_id": self.source_id, "version": self.version}

    def to_doc(self) -> Dict[str, Any]:
        return {**{k: getattr(self, k) for k in self._FIELDS}, "owned": self.owned, "extra": self.extra,
                "version": self.version}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "SourceItem":
        return cls(**{k: d.get(k) for k in cls._FIELDS},
                   owned=d.get("owned", True), extra=d.get("extra") or {}, version=int(d.get("version") or 1))


class SourceUnavailable(Exception):
    """Temporary failure contacting a source."""


@dataclass
class TransferContext:
    """What download()/validate() may use. Built by the pipeline; adapters never read env secrets here."""
    cfg: Any
    http: Optional[Callable[[], Any]] = None          # returns an aiohttp session
    client: Any = None                                # Telegram client (for /attach inbox files)
    keepalive: Optional[Callable[[], Awaitable[None]]] = None


@dataclass
class DownloadOutcome:
    path: str
    sha256: Optional[str] = None
    size: Optional[int] = None


class SourceAdapter(abc.ABC):
    type = "base"

    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = config

    @abc.abstractmethod
    async def find(self, anime: dict, episode: int) -> Optional[SourceItem]:
        """Return a matching authorized item, or None if not (yet) available."""

    async def health(self) -> str:
        return "ok"

    def headers(self) -> Dict[str, str]:
        return {}

    async def download(self, item: SourceItem, destination: str, ctx: TransferContext) -> DownloadOutcome:
        """Put the item's file on local disk. Default handles local, Telegram and allowlisted URLs."""
        from ..netsafe import DownloadError, download
        cfg = ctx.cfg
        if item.kind == "local":
            if not os.path.isfile(item.ref):
                raise DownloadError("Local source file disappeared")
            return DownloadOutcome(item.ref, None, os.path.getsize(item.ref))
        if item.kind == "telegram":
            if ctx.client is None:
                raise DownloadError("No Telegram client available", transient=False)
            path = await asyncio.wait_for(ctx.client.download_media(item.ref, file_name=destination),
                                          timeout=cfg.download_timeout_seconds)
            if not path or not os.path.isfile(path):
                raise DownloadError("Telegram download returned no file")
            return DownloadOutcome(path, None, os.path.getsize(path))
        if item.kind != "url":
            raise DownloadError(f"Unsupported item kind {item.kind}", transient=False)

        async def progress(_n):
            if ctx.keepalive:
                await ctx.keepalive()
        from ..hls import download_hls, is_hls_url
        if is_hls_url(item.ref) or (item.extra or {}).get("format") == "hls":
            # Authorized provider streaming as HLS: segments are fetched through the same safety checks,
            # encrypted/DRM streams are refused, FFmpeg remuxes locally.
            path = await download_hls(ctx.http(), item.ref, destination, cfg.allowed_source_hosts,
                                      allow_http=cfg.allow_http_sources, max_bytes=cfg.max_file_size_bytes,
                                      timeout_s=cfg.download_timeout_seconds, headers=self.headers() or None,
                                      quality=item.quality, on_progress=progress)
            return DownloadOutcome(path, None, os.path.getsize(path))
        res = await download(ctx.http(), item.ref, destination, cfg.allowed_source_hosts,
                             allow_http=cfg.allow_http_sources, max_bytes=cfg.max_file_size_bytes,
                             timeout_s=cfg.download_timeout_seconds, headers=self.headers() or None,
                             on_progress=progress)
        return DownloadOutcome(res.path, res.sha256, os.path.getsize(res.path))

    async def validate(self, item: SourceItem, path: str, ctx: TransferContext,
                       actual_sha256: Optional[str] = None):
        """Check size limits, expected size/checksum and container signature. Raises InvalidFile."""
        from ..validate import validate_video
        cfg = ctx.cfg
        return await validate_video(path, min_bytes=cfg.min_file_size_bytes, max_bytes=cfg.max_file_size_bytes,
                                    expected_size=item.size, expected_sha256=item.sha256,
                                    actual_sha256=actual_sha256)

    async def close(self) -> None:
        pass


class GenericSource(SourceAdapter):
    """Used when an item's source was removed after the item was queued (download/validate only)."""
    type = "generic"

    async def find(self, anime, episode):
        return None
