"""Safe outbound HTTP for authorized sources.

* Only https (http only if ALLOW_HTTP_SOURCES) to hosts on the allowlist.
* DNS answers are filtered at connect time so private/loopback/link-local
  addresses are refused (blocks SSRF and DNS rebinding).
* Redirects are followed manually and each hop is re-validated.
* Downloads stream to a .part file with size and wall-clock limits.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver

log = logging.getLogger(__name__)


class UnsafeURL(ValueError):
    pass


class DownloadError(Exception):
    def __init__(self, msg: str, transient: bool = True):
        super().__init__(msg)
        self.transient = transient


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified)


def host_allowed(host: str, allowlist: Iterable[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    for allowed in allowlist:
        allowed = allowed.lower().lstrip(".")
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def validate_url(url: str, allowlist: Iterable[str], allow_http: bool = False) -> str:
    allowlist = tuple(allowlist)
    if not url or len(url) > 2048:
        raise UnsafeURL("URL missing or too long")
    parts = urlsplit(url)
    schemes = {"https", "http"} if allow_http else {"https"}
    if parts.scheme not in schemes:
        raise UnsafeURL(f"Scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password:
        raise UnsafeURL("Credentials in URL are not allowed")
    host = parts.hostname or ""
    if not host:
        raise UnsafeURL("URL has no host")
    try:
        ipaddress.ip_address(host)
        if not is_public_ip(host):
            raise UnsafeURL("Private or local IP addresses are not allowed")
    except ValueError:
        pass
    if not allowlist:
        raise UnsafeURL("ALLOWED_SOURCE_HOSTS is empty; refusing all remote sources")
    if not host_allowed(host, allowlist):
        raise UnsafeURL(f"Host {host!r} is not in ALLOWED_SOURCE_HOSTS")
    return url


class PublicOnlyResolver(AbstractResolver):
    """Resolver that drops non-public addresses (anti DNS-rebinding)."""

    def __init__(self):
        self._inner = ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self._inner.resolve(host, port, family)
        safe = [r for r in results if is_public_ip(r["host"])]
        if not safe:
            raise OSError(f"{host} resolves only to non-public addresses")
        return safe

    async def close(self):
        await self._inner.close()


def make_session(timeout_s: int = 60) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver(), limit=8, ttl_dns_cache=60)
    return aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=None, sock_connect=20,
                                                                                    sock_read=timeout_s),
                                 headers={"User-Agent": "AnimeSoulBot/1.0"})


async def _open(session, url, allowlist, allow_http, headers=None, max_redirects=3):
    for _ in range(max_redirects + 1):
        validate_url(url, allowlist, allow_http)
        resp = await session.get(url, allow_redirects=False, headers=headers or {})
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            resp.release()
            if not loc:
                raise DownloadError("Redirect without Location", transient=False)
            url = urljoin(url, loc)
            continue
        return resp
    raise DownloadError("Too many redirects", transient=False)


async def fetch_json(session, url: str, allowlist, allow_http=False, headers=None, max_bytes=2 * 1024 * 1024):
    resp = await _open(session, url, allowlist, allow_http, headers)
    try:
        if resp.status >= 500 or resp.status == 429:
            raise DownloadError(f"Feed HTTP {resp.status}")
        if resp.status != 200:
            raise DownloadError(f"Feed HTTP {resp.status}", transient=False)
        body = await resp.content.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise DownloadError("Feed too large", transient=False)
        import json
        return json.loads(body.decode("utf-8"))
    finally:
        resp.release()


@dataclass
class DownloadResult:
    path: str
    size: int
    sha256: str


async def download(session, url: str, dest: str, allowlist, *, allow_http=False, max_bytes: int,
                   timeout_s: int, headers=None, on_progress=None) -> DownloadResult:
    """Stream ``url`` to ``dest`` atomically (``dest.part`` then rename)."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    digest = hashlib.sha256()
    size = 0

    async def _run():
        nonlocal size
        resp = await _open(session, url, allowlist, allow_http, headers)
        try:
            if resp.status == 404:
                raise DownloadError("File not found (404)", transient=True)
            if resp.status in (401, 403):
                raise DownloadError(f"Access denied ({resp.status}); check source credentials", transient=False)
            if resp.status >= 400:
                raise DownloadError(f"HTTP {resp.status}", transient=resp.status >= 500 or resp.status == 429)
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise DownloadError(f"File larger than limit ({int(declared)} bytes)", transient=False)
            with open(tmp, "wb") as fh:
                last_report = 0
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise DownloadError("File exceeded size limit while downloading", transient=False)
                    fh.write(chunk)
                    digest.update(chunk)
                    if on_progress and size - last_report >= 50 * 1024 * 1024:
                        last_report = size
                        await on_progress(size)
            if declared and declared.isdigit() and int(declared) != size:
                raise DownloadError("Download incomplete (size mismatch)")
        finally:
            resp.release()

    try:
        await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _silent_remove(tmp)
        raise DownloadError("Download timed out")
    except (aiohttp.ClientError, OSError) as e:
        _silent_remove(tmp)
        raise DownloadError(f"Network error: {e}")
    except BaseException:
        _silent_remove(tmp)
        raise
    os.replace(tmp, dest)
    return DownloadResult(dest, size, digest.hexdigest())


def _silent_remove(path: Optional[str]) -> None:
    if path:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
