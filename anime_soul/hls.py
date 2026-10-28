"""Download an HLS (.m3u8) stream from an **authorized** provider and remux it into one file.

AutoAnime.zip ships the N_m3u8DL-RE binary (plus proxies) to rip streams from unlicensed sites. That is
not used. This module covers the legitimate need: a licensed provider that delivers episodes as HLS. It
only accepts providers listed in ALLOWED_SOURCE_HOSTS.

Safety rules (enforced, not optional):
* every playlist and segment URL goes through netsafe (allowlist, public IPs only, redirects re-checked);
* encrypted or DRM-protected streams (any ``#EXT-X-KEY`` / ``#EXT-X-SESSION-KEY`` with METHOD other
  than NONE) are refused — this never decrypts or bypasses protection;
* total size is capped at MAX_FILE_SIZE_MB, segment count is capped, live (no ENDLIST) playlists are refused;
* FFmpeg only reads the downloaded local files (``-protocol_whitelist file``), never the network.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin

from .netsafe import DownloadError, download, fetch_bytes

log = logging.getLogger(__name__)

MAX_SEGMENTS = 5000
PLAYLIST_MAX_BYTES = 4 * 1024 * 1024
_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def is_hls_url(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith((".m3u8", ".m3u"))


def _attrs(line: str) -> dict:
    body = line.split(":", 1)[1] if ":" in line else ""
    return {k: v.strip('"') for k, v in _ATTR.findall(body)}


@dataclass
class Variant:
    url: str
    bandwidth: int
    height: Optional[int]


@dataclass
class MediaPlaylist:
    init: Optional[str]
    segments: List[str]


def parse_master(text: str, base: str) -> List[Variant]:
    out, lines = [], [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            a = _attrs(ln)
            nxt = next((x for x in lines[i + 1:] if x and not x.startswith("#")), None)
            if not nxt:
                continue
            res = a.get("RESOLUTION", "")
            h = int(res.split("x")[1]) if "x" in res and res.split("x")[1].isdigit() else None
            out.append(Variant(urljoin(base, nxt), int(a.get("BANDWIDTH", "0") or 0), h))
    return out


def check_protection(text: str) -> None:
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith(("#EXT-X-KEY", "#EXT-X-SESSION-KEY")):
            if _attrs(ln).get("METHOD", "NONE").upper() != "NONE":
                raise DownloadError("Stream is encrypted/DRM-protected; ANIME SOUL never decrypts streams. "
                                    "Ask the provider for an unencrypted download.", transient=False)


def parse_media(text: str, base: str) -> MediaPlaylist:
    if not text.lstrip().startswith("#EXTM3U"):
        raise DownloadError("Not an HLS playlist", transient=False)
    check_protection(text)
    if "#EXT-X-ENDLIST" not in text:
        raise DownloadError("Live/unfinished HLS playlist (no #EXT-X-ENDLIST); retry when the episode is complete")
    init, segs = None, []
    for ln in (x.strip() for x in text.splitlines()):
        if ln.startswith("#EXT-X-MAP"):
            uri = _attrs(ln).get("URI")
            if uri:
                init = urljoin(base, uri)
        elif ln and not ln.startswith("#"):
            segs.append(urljoin(base, ln))
    if not segs:
        raise DownloadError("HLS playlist has no segments", transient=False)
    if len(segs) > MAX_SEGMENTS:
        raise DownloadError(f"HLS playlist has too many segments ({len(segs)})", transient=False)
    return MediaPlaylist(init, segs)


def pick_variant(variants: List[Variant], quality: Optional[str]) -> Variant:
    want = int(quality.rstrip("p")) if quality and quality.rstrip("p").isdigit() else None
    if want:
        exact = [v for v in variants if v.height == want]
        if exact:
            return max(exact, key=lambda v: v.bandwidth)
        below = [v for v in variants if v.height and v.height <= want]
        if below:
            return max(below, key=lambda v: (v.height, v.bandwidth))
    return max(variants, key=lambda v: ((v.height or 0), v.bandwidth))


async def resolve(session, url: str, allowlist, *, allow_http=False, headers=None,
                  quality: Optional[str] = None) -> Tuple[str, MediaPlaylist]:
    text = (await fetch_bytes(session, url, allowlist, allow_http, headers, PLAYLIST_MAX_BYTES)).decode("utf-8", "replace")
    check_protection(text)
    if "#EXT-X-STREAM-INF" in text:
        variants = parse_master(text, url)
        if not variants:
            raise DownloadError("HLS master playlist lists no variants", transient=False)
        url = pick_variant(variants, quality).url
        text = (await fetch_bytes(session, url, allowlist, allow_http, headers, PLAYLIST_MAX_BYTES)).decode("utf-8", "replace")
    return url, parse_media(text, url)


async def download_hls(session, url: str, dest: str, allowlist, *, allow_http=False, max_bytes: int,
                       timeout_s: int, headers=None, quality: Optional[str] = None,
                       on_progress: Optional[Callable] = None, ffmpeg: Optional[str] = None) -> str:
    """Download every segment (bounded, sequential), then remux locally with FFmpeg. Returns dest."""
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise DownloadError("FFmpeg is required for HLS sources (add the FFmpeg buildpack)", transient=False)
    if not dest.lower().endswith((".mkv", ".mp4", ".m4v", ".ts")):
        dest = os.path.splitext(dest)[0] + ".mkv"       # e.g. "episode.m3u8" → "episode.mkv"
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    _, playlist = await resolve(session, url, allowlist, allow_http=allow_http, headers=headers, quality=quality)
    work = tempfile.mkdtemp(prefix="hls-", dir=os.path.dirname(os.path.abspath(dest)) or None)
    total = 0
    try:
        local_lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:3600", "#EXT-X-PLAYLIST-TYPE:VOD"]
        parts = ([("init.mp4", playlist.init)] if playlist.init else []) + \
                [(f"seg{i:05d}.ts" if not playlist.init else f"seg{i:05d}.m4s", s)
                 for i, s in enumerate(playlist.segments)]
        for n, (name, seg_url) in enumerate(parts):
            path = os.path.join(work, name)
            res = await download(session, seg_url, path, allowlist, allow_http=allow_http,
                                 max_bytes=max(1, max_bytes - total), timeout_s=timeout_s, headers=headers)
            total += res.size
            if total > max_bytes:
                raise DownloadError("HLS stream larger than MAX_FILE_SIZE_MB", transient=False)
            if on_progress:
                await on_progress(total)
            if name == "init.mp4":
                local_lines.append('#EXT-X-MAP:URI="init.mp4"')
            else:
                local_lines += ["#EXTINF:10.0,", name]
        local_lines.append("#EXT-X-ENDLIST")
        local = os.path.join(work, "local.m3u8")
        with open(local, "w") as f:
            f.write("\n".join(local_lines) + "\n")
        args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-protocol_whitelist", "file", "-allowed_extensions", "ALL", "-i", local,
                "-map", "0", "-c", "copy"]
        if dest.lower().endswith((".mp4", ".m4v")):
            args += ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]
        tmp_out = dest + ".remux" + os.path.splitext(dest)[1]
        proc = await asyncio.create_subprocess_exec(*args, tmp_out, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise DownloadError("FFmpeg remux of HLS stream timed out")
        if proc.returncode != 0 or not os.path.isfile(tmp_out):
            raise DownloadError(f"FFmpeg could not remux the HLS stream: {err.decode(errors='replace')[-300:]}",
                                transient=False)
        os.replace(tmp_out, dest)
        log.info("HLS download done: %d segment(s), %d bytes", len(playlist.segments), total)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for p in (dest + ".remux" + os.path.splitext(dest)[1],):
            if os.path.exists(p):
                os.remove(p)
