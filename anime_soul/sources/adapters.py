"""Built-in authorized source adapters."""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

from .. import models as M
from ..matcher import VIDEO_EXTS, matches_episode, parse_release_name
from ..netsafe import DownloadError, UnsafeURL, fetch_json, make_session, validate_url
from .base import SourceAdapter, SourceItem, SourceUnavailable

log = logging.getLogger(__name__)


class TelegramInboxSource(SourceAdapter):
    """Files an administrator sent to the bot with /attach (always enabled)."""
    type = "telegram_inbox"

    def __init__(self, db):
        super().__init__("admin-inbox", {})
        self.db = db

    async def find(self, anime, episode):
        doc = await self.db.get_inbox_file(M.episode_key(anime["_id"], episode))
        if not doc:
            return None
        return SourceItem(self.name, "telegram", doc["file_id"], doc.get("file_name") or "episode",
                          size=doc.get("file_size"), quality=doc.get("quality"), language=doc.get("language"),
                          extra={"media_type": doc.get("media_type"), "chat_id": doc.get("chat_id"),
                                 "message_id": doc.get("message_id"), "file_unique_id": doc.get("file_unique_id")})


class LocalDirSource(SourceAdapter):
    """A directory on the server you own/are licensed to distribute (VPS or mounted disk)."""
    type = "local_dir"

    def __init__(self, name, config, allowed_roots):
        super().__init__(name, config)
        path = os.path.realpath(config.get("path", ""))
        roots = [os.path.realpath(r) for r in allowed_roots]
        if not roots or not any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots):
            raise ValueError("Directory is outside LOCAL_SOURCE_ROOTS")
        self.path = path

    def _scan(self) -> List[str]:
        out = []
        if not os.path.isdir(self.path):
            raise SourceUnavailable(f"Directory not found: {self.path}")
        for root, dirs, files in os.walk(self.path):
            if root[len(self.path):].count(os.sep) >= 2:
                dirs[:] = []
            for f in files:
                if f.lower().endswith(VIDEO_EXTS) and not f.endswith(".part"):
                    out.append(os.path.join(root, f))
        return out

    async def find(self, anime, episode):
        import asyncio
        for full in await asyncio.to_thread(self._scan):
            real = os.path.realpath(full)
            if not real.startswith(self.path + os.sep):
                continue  # symlink escaping the directory
            p = parse_release_name(os.path.basename(real))
            if matches_episode(anime, episode, title=p.title, ep=p.episode, quality=p.quality, season=p.season):
                # Skip files still being written (modified in the last 60 s).
                if time.time() - os.path.getmtime(real) < 60:
                    continue
                return SourceItem(self.name, "local", real, os.path.basename(real),
                                  size=os.path.getsize(real), quality=p.quality, owned=False)
        return None

    async def health(self):
        return "ok" if os.path.isdir(self.path) else "directory missing"


class HttpFeedSource(SourceAdapter):
    """JSON feed published by a distributor/partner who authorizes redistribution.

    Feed format::

        {"items": [{"anilist_id": 21, "title": "...", "episode": 5, "season": 1,
                    "quality": "1080p", "language": "Japanese (English Sub)",
                    "url": "https://cdn.partner.example/ep5.mkv",
                    "size": 1234567, "sha256": "...", "file_name": "..."}]}
    """
    type = "http_feed"

    def __init__(self, name, config, allowlist, allow_http=False):
        super().__init__(name, config)
        self.allowlist = tuple(allowlist)
        self.allow_http = allow_http
        validate_url(config.get("url", ""), self.allowlist, allow_http)
        self.url = config["url"]
        self._session = None
        self._cache: tuple[float, list] | None = None

    def headers(self) -> Dict[str, str]:
        env = self.config.get("auth_header_env")
        if env and os.getenv(env):
            return {self.config.get("auth_header_name", "Authorization"): os.environ[env]}
        return {}

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = make_session()
        return self._session

    async def items(self) -> list:
        if self._cache and time.monotonic() - self._cache[0] < 60:
            return self._cache[1]
        try:
            data = await fetch_json(await self.session(), self.url, self.allowlist, self.allow_http, self.headers())
        except (DownloadError, UnsafeURL, ValueError) as e:
            raise SourceUnavailable(str(e))
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise SourceUnavailable("Feed has no 'items' list")
        self._cache = (time.monotonic(), items)
        return items

    async def find(self, anime, episode):
        for it in await self.items():
            if not isinstance(it, dict) or not it.get("url"):
                continue
            try:
                ok = matches_episode(anime, episode, title=it.get("title"), anime_id=it.get("anilist_id"),
                                     ep=it.get("episode"), quality=it.get("quality"),
                                     language=it.get("language"), season=it.get("season"))
            except (TypeError, ValueError):
                continue
            if not ok:
                continue
            try:
                validate_url(it["url"], self.allowlist, self.allow_http)
            except UnsafeURL as e:
                log.warning("Feed %s item rejected: %s", self.name, e)
                continue
            name = it.get("file_name") or it["url"].rsplit("/", 1)[-1].split("?")[0] or f"episode{episode}.mkv"
            return SourceItem(self.name, "url", it["url"], os.path.basename(name), size=it.get("size"),
                              sha256=it.get("sha256"), quality=it.get("quality"), language=it.get("language"))
        return None

    async def health(self):
        try:
            return f"ok ({len(await self.items())} items)"
        except SourceUnavailable as e:
            return f"error: {e}"

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


def build_adapter(doc: dict, cfg) -> SourceAdapter:
    if not doc.get("authorized") or not (doc.get("license_note") or "").strip():
        raise ValueError("Source is not marked as authorized")
    t = doc.get("type")
    conf = doc.get("config") or {}
    if t == "local_dir":
        return LocalDirSource(doc["_id"], conf, cfg.local_source_roots)
    if t == "http_feed":
        return HttpFeedSource(doc["_id"], conf, cfg.allowed_source_hosts, cfg.allow_http_sources)
    raise ValueError(f"Unknown source type {t!r}")


class SourceRegistry:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self.inbox = TelegramInboxSource(db)
        self._adapters: Dict[str, SourceAdapter] = {}
        self._loaded_at = 0.0

    async def adapters(self) -> List[SourceAdapter]:
        if time.monotonic() - self._loaded_at > 60:
            docs = await self.db.list_sources(enabled_only=True)
            fresh: Dict[str, SourceAdapter] = {}
            for d in docs:
                key = f"{d['_id']}:{d.get('updated_at')}"
                if key in self._adapters:
                    fresh[key] = self._adapters.pop(key)
                    continue
                try:
                    fresh[key] = build_adapter(d, self.cfg)
                except ValueError as e:
                    log.warning("Source %s disabled: %s", d["_id"], e)
                    await self.db.source_status(d["_id"], False, str(e))
            for old in self._adapters.values():
                await old.close()
            self._adapters, self._loaded_at = fresh, time.monotonic()
        return [self.inbox, *self._adapters.values()]

    async def find(self, anime: dict, episode: int) -> Optional[SourceItem]:
        errors = []
        for ad in await self.adapters():
            try:
                item = await ad.find(anime, episode)
                if ad is not self.inbox:
                    await self.db.source_status(ad.name, True)
                if item:
                    return item
            except SourceUnavailable as e:
                errors.append(f"{ad.name}: {e}")
                await self.db.source_status(ad.name, False, str(e))
        if errors:
            log.info("Sources unavailable for %s ep %s: %s", anime["_id"], episode, "; ".join(errors))
        return None

    def adapter_by_name(self, name: str) -> Optional[SourceAdapter]:
        for ad in self._adapters.values():
            if ad.name == name:
                return ad
        return None

    async def close(self):
        for ad in self._adapters.values():
            await ad.close()
