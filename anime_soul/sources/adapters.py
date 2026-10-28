"""Built-in authorized source adapters."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, List, Optional

from .. import models as M
from ..matcher import VIDEO_EXTS, matches_episode, parse_release_name
from ..netsafe import DownloadError, UnsafeURL, fetch_bytes, fetch_json, make_session, validate_url
from ..timeutil import utcnow
from .base import GenericSource, SourceAdapter, SourceItem, SourceUnavailable, make_release_id

# Quick in-call retries for temporary feed errors (timeouts, 5xx, 429). Longer outages are
# handled by the per-source exponential backoff in SourceRegistry (persisted in MongoDB).
FETCH_RETRIES = 2
FETCH_RETRY_BASE_DELAY = 0.5
SOURCE_BACKOFF_BASE = 60          # seconds; doubles per consecutive failure
SOURCE_BACKOFF_MAX = 3600


def source_backoff_seconds(fail_count: int) -> int:
    return int(min(SOURCE_BACKOFF_BASE * (2 ** max(0, fail_count - 1)), SOURCE_BACKOFF_MAX))


async def _fetch_with_retry(fn):
    """Run fn() and retry temporary failures with exponential backoff. Raises SourceUnavailable."""
    import aiohttp
    delay = FETCH_RETRY_BASE_DELAY
    for attempt in range(FETCH_RETRIES + 1):
        try:
            return await fn()
        except DownloadError as e:
            err, transient = str(e), e.transient
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            err, transient = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__, True
        except (UnsafeURL, ValueError) as e:
            err, transient = str(e), False
        if not transient or attempt == FETCH_RETRIES:
            raise SourceUnavailable(err)
        await asyncio.sleep(delay)
        delay *= 2

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
                          anime_id=int(anime["_id"]), episode=int(episode),
                          release_id=make_release_id(self.name, doc.get("file_unique_id") or doc["file_id"]),
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
            if p.is_batch:
                continue
            if matches_episode(anime, episode, title=p.title, ep=p.episode, quality=p.quality, season=p.season):
                # Skip files still being written (modified in the last 60 s).
                if time.time() - os.path.getmtime(real) < 60:
                    continue
                st = os.stat(real)
                return SourceItem(self.name, "local", real, os.path.basename(real),
                                  size=st.st_size, quality=p.quality, language=p.language, owned=False,
                                  anime_id=int(anime["_id"]), episode=int(episode), version=p.version,
                                  release_id=make_release_id(self.name, real, st.st_size, int(st.st_mtime)))
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
        session = await self.session()
        data = await _fetch_with_retry(
            lambda: fetch_json(session, self.url, self.allowlist, self.allow_http, self.headers()))
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise SourceUnavailable("Feed has no 'items' list")
        self._cache = (time.monotonic(), items)
        return items

    async def find(self, anime, episode):
        """Best matching item: highest re-release version (v2 beats v1), then feed order."""
        best = None
        for it in await self.items():
            if not isinstance(it, dict) or not it.get("url") or it.get("is_batch"):
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
            version = _version(it)
            if best is not None and version <= best.version:
                continue
            best = SourceItem(self.name, "url", it["url"], os.path.basename(name), size=_size(it.get("size")),
                              sha256=it.get("sha256"), quality=it.get("quality"), language=it.get("language"),
                              anime_id=int(anime["_id"]), episode=int(episode), version=version,
                              release_id=release_id_of(self.name, it))
        return best

    async def releases(self) -> list:
        """Items with a parsed release time, used by the monitor for feed-based release detection."""
        out = []
        for it in await self.items():
            if not isinstance(it, dict) or not it.get("url") or it.get("episode") is None:
                continue
            if it.get("is_batch"):
                continue
            out.append({**it, "released_at": _parse_time(it.get("released_at")),
                        "release_id": release_id_of(self.name, it), "version": _version(it)})
        return out

    async def health(self):
        try:
            return f"ok ({len(await self.items())} items)"
        except SourceUnavailable as e:
            return f"error: {e}"

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


TORRENT_TYPES = ("application/x-bittorrent",)


def release_id_of(source: str, it: dict) -> str:
    """Feed GUID if present (stable across title edits), else the file URL."""
    return it.get("release_id") or make_release_id(source, it.get("guid") or it.get("id") or it.get("url"))


def _version(it: dict) -> int:
    try:
        return max(1, int(it.get("version") or 1))
    except (TypeError, ValueError):
        return 1


def _size(v):
    try:
        return int(v) if v not in (None, "") and int(v) > 0 else None
    except (TypeError, ValueError):
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _parse_time(value):
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        dt = parsedate_to_datetime(text)                      # RSS pubDate (RFC 822)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))   # Atom / ISO 8601
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_rss(body: bytes) -> list:
    """Parse RSS 2.0 / Atom into feed-item dicts. Uses defusedxml (no entity expansion / XXE).

    Only direct-download enclosures are kept. Torrent/magnet links are ignored: this bot only
    downloads files a distributor serves to you directly under a redistribution agreement.
    """
    from defusedxml import ElementTree as DET
    try:
        root = DET.fromstring(body)
    except Exception as e:  # defusedxml raises several error types
        raise SourceUnavailable(f"Feed is not valid XML: {type(e).__name__}")
    entries = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    items = []
    for el in entries:
        fields, url, size, mime = {}, None, None, ""
        for child in el:
            name = _local(child.tag)
            text = (child.text or "").strip()
            if name in ("enclosure", "content") and child.get("url"):
                url, size, mime = child.get("url"), child.get("length") or child.get("filesize"), child.get("type", "")
            elif name == "link" and child.get("rel") == "enclosure" and child.get("href"):
                url, size, mime = child.get("href"), child.get("length"), child.get("type", "")
            elif text:
                fields.setdefault(name, text)
        if not url:
            continue
        low = url.lower()
        if low.startswith("magnet:") or low.split("?", 1)[0].endswith(".torrent") or mime.lower() in TORRENT_TYPES:
            log.info("Ignoring torrent/magnet enclosure in feed: %s", fields.get("title", "")[:80])
            continue
        title = fields.get("title", "")
        p = parse_release_name(title)
        ep = fields.get("episode") or fields.get("episodenumber") or p.episode
        item = {
            "guid": fields.get("guid") or fields.get("id"),
            "version": p.version,
            "is_batch": p.is_batch,
            "group": p.group,
            "title": p.title or title,
            "raw_title": title,
            "episode": int(ep) if str(ep or "").isdigit() else None,
            "season": int(fields["season"]) if str(fields.get("season", "")).isdigit() else p.season,
            "quality": fields.get("quality") or p.quality,
            "language": fields.get("language") or p.language,
            "anilist_id": int(fields["anilist_id"]) if str(fields.get("anilist_id", "")).isdigit() else None,
            "url": url,
            "size": int(size) if str(size or "").isdigit() and int(size) > 0 else None,
            "sha256": fields.get("sha256"),
            "file_name": fields.get("file_name") or os.path.basename(url.split("?", 1)[0]) or None,
            "released_at": fields.get("pubdate") or fields.get("published") or fields.get("updated"),
        }
        items.append(item)
    return items


class RssFeedSource(HttpFeedSource):
    """RSS 2.0 / Atom feed from a distributor that authorizes redistribution.

    Each <item> needs a direct-download <enclosure url="https://allowed-host/..."> (or Atom
    <link rel="enclosure" href=...>). The title is parsed like a release name
    ("Show Name - 05 [1080p]"); optional child tags <anilist_id>, <episode>, <season>,
    <quality>, <language>, <sha256> make matching exact.
    """
    type = "rss_feed"

    async def items(self) -> list:
        if self._cache and time.monotonic() - self._cache[0] < 60:
            return self._cache[1]
        session = await self.session()
        body = await _fetch_with_retry(
            lambda: fetch_bytes(session, self.url, self.allowlist, self.allow_http, self.headers()))
        items = parse_rss(body)
        self._cache = (time.monotonic(), items)
        return items


def build_adapter(doc: dict, cfg) -> SourceAdapter:
    if not doc.get("authorized") or not (doc.get("license_note") or "").strip():
        raise ValueError("Source is not marked as authorized")
    t = doc.get("type")
    conf = doc.get("config") or {}
    if t == "local_dir":
        return LocalDirSource(doc["_id"], conf, cfg.local_source_roots)
    if t == "http_feed":
        return HttpFeedSource(doc["_id"], conf, cfg.allowed_source_hosts, cfg.allow_http_sources)
    if t == "rss_feed":
        return RssFeedSource(doc["_id"], conf, cfg.allowed_source_hosts, cfg.allow_http_sources)
    raise ValueError(f"Unknown source type {t!r}")


class SourceRegistry:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self.inbox = TelegramInboxSource(db)
        self._adapters: Dict[str, SourceAdapter] = {}
        self._loaded_at = 0.0
        self._retry_at: Dict[str, object] = {}     # source name -> datetime (backoff until)

    async def adapters(self) -> List[SourceAdapter]:
        if time.monotonic() - self._loaded_at > 60:
            docs = await self.db.list_sources(enabled_only=True)
            fresh: Dict[str, SourceAdapter] = {}
            self._retry_at = {d["_id"]: d.get("retry_at") for d in docs if d.get("retry_at")}
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

    async def configured_count(self) -> int:
        """Number of enabled, authorized external sources (the /attach inbox is not counted)."""
        return len(await self.adapters()) - 1

    async def find(self, anime: dict, episode: int) -> Optional[SourceItem]:
        item, _ = await self.find_with_reason(anime, episode)
        return item

    async def find_with_reason(self, anime: dict, episode: int):
        """Return (item, None) or (None, reason) where reason is a models.SOURCE_* code."""
        errors = []
        adapters = await self.adapters()
        for ad in adapters:
            if self.in_backoff(ad.name):
                errors.append(f"{ad.name}: backing off after repeated errors")
                continue
            try:
                item = await ad.find(anime, episode)
                if ad is not self.inbox:
                    await self.mark_ok(ad.name)
                if item:
                    return item, None
            except SourceUnavailable as e:
                errors.append(f"{ad.name}: {e}")
                await self.mark_failed(ad.name, str(e))
        if errors:
            log.info("Sources unavailable for %s ep %s: %s", anime["_id"], episode, "; ".join(errors))
            return None, M.SOURCE_ERROR
        if len(adapters) <= 1:
            return None, M.SOURCE_NOT_CONFIGURED
        return None, M.SOURCE_NOT_FOUND

    async def feed_adapters(self) -> List[SourceAdapter]:
        return [a for a in await self.adapters() if hasattr(a, "releases")]

    def adapter_by_name(self, name: str) -> Optional[SourceAdapter]:
        if name == self.inbox.name:
            return self.inbox
        for ad in self._adapters.values():
            if ad.name == name:
                return ad
        return None

    def adapter_for(self, item: SourceItem) -> SourceAdapter:
        """Adapter that downloads/validates an item (generic fallback if the source was removed)."""
        return self.adapter_by_name(item.source) or GenericSource(item.source, {})

    # ------------------------------------------------------------ health/backoff
    def in_backoff(self, name: str) -> bool:
        until = self._retry_at.get(name)
        if not until:
            return False
        if getattr(until, "tzinfo", None) is None:
            from datetime import timezone
            until = until.replace(tzinfo=timezone.utc)
        return until > utcnow()

    async def mark_ok(self, name: str) -> None:
        self._retry_at.pop(name, None)
        await self.db.source_status(name, True)

    async def mark_failed(self, name: str, error: str) -> int:
        """Record a failure and start exponential backoff. Returns the consecutive failure count."""
        if name == self.inbox.name:
            return 0
        fails = await self.db.source_status(name, False, error)
        from datetime import timedelta
        until = utcnow() + timedelta(seconds=source_backoff_seconds(fails))
        self._retry_at[name] = until
        await self.db.sources.update_one({"_id": name}, {"$set": {"retry_at": until}})
        return fails

    async def close(self):
        for ad in self._adapters.values():
            await ad.close()
