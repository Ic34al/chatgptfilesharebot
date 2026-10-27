"""File integrity checks before anything is uploaded."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from typing import Optional


class InvalidFile(Exception):
    pass


@dataclass
class FileReport:
    path: str
    size: int
    container: str
    duration: Optional[float] = None


def sniff_container(head: bytes) -> Optional[str]:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    if len(head) >= 377 and head[0] == 0x47 and head[188] == 0x47 and head[376] == 0x47:
        return "mpegts"
    return None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


async def ffprobe_duration(path: str, timeout: int = 60) -> Optional[float]:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-show_entries", "format=duration", "-of", "json", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise InvalidFile("ffprobe timed out")
    if proc.returncode != 0:
        raise InvalidFile(f"ffprobe could not read the file: {err.decode(errors='ignore')[:200]}")
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        raise InvalidFile("ffprobe returned no duration")


async def validate_video(path: str, *, min_bytes: int, max_bytes: int, expected_size: Optional[int] = None,
                         expected_sha256: Optional[str] = None, actual_sha256: Optional[str] = None) -> FileReport:
    if not os.path.isfile(path):
        raise InvalidFile("File does not exist")
    size = os.path.getsize(path)
    if size < min_bytes:
        raise InvalidFile(f"File too small ({size} bytes) — likely incomplete or not a video")
    if size > max_bytes:
        raise InvalidFile(f"File too large ({size} bytes); Telegram bot limit is 2000 MB")
    if expected_size and int(expected_size) != size:
        raise InvalidFile(f"Size mismatch: expected {expected_size}, got {size}")
    with open(path, "rb") as fh:
        head = fh.read(512)
    container = sniff_container(head)
    if not container:
        raise InvalidFile("Unrecognised video container (not MP4/MKV/AVI/TS)")
    if expected_sha256:
        digest = actual_sha256 or await asyncio.to_thread(sha256_file, path)
        if digest.lower() != expected_sha256.lower():
            raise InvalidFile("SHA-256 checksum mismatch")
    duration = await ffprobe_duration(path)
    if duration is not None and duration < 30:
        raise InvalidFile(f"Video duration too short ({duration:.0f}s)")
    return FileReport(path, size, container, duration)
