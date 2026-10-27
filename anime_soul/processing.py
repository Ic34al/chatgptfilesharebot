"""FFprobe validation and FFmpeg processing (remux / transcode) for episode files.

Design rules
------------
* The source file is never modified or deleted here. Outputs go to a per-job
  temporary directory; the pipeline deletes files only after the Telegram
  upload has been verified.
* No re-encoding unless it is needed or explicitly requested (PROCESS_MODE).
* Every FFmpeg/FFprobe call has a timeout and is killed on timeout/cancel.
* Disk space is checked before writing anything.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable, List, Optional

log = logging.getLogger(__name__)

# Codecs Telegram plays inline when wrapped in MP4 (send_video + supports_streaming).
MP4_VIDEO_CODECS = {"h264", "hevc"}
MP4_AUDIO_CODECS = {"aac", "mp3"}
MP4_TEXT_SUBS = {"mov_text"}
SUPPORTED_FORMATS = ("matroska", "webm", "mov", "mp4", "m4a", "3gp", "3g2", "mj2", "avi", "mpegts")


class ProcessingError(Exception):
    def __init__(self, msg: str, transient: bool = True):
        super().__init__(msg)
        self.transient = transient


class FFmpegMissing(ProcessingError):
    def __init__(self, tool: str):
        super().__init__(f"FFMPEG_NOT_AVAILABLE: '{tool}' is not installed. On Heroku add the buildpack "
                         "https://github.com/heroku/heroku-buildpack-activestorage-preview and redeploy.",
                         transient=True)


class MediaRejected(ProcessingError):
    """The file is not an acceptable episode (wrong streams, duration, format...)."""

    def __init__(self, msg: str):
        super().__init__(msg, transient=False)


@dataclass
class MediaInfo:
    format_name: str
    duration: float
    size: int
    video_codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    audio_codecs: List[str] = field(default_factory=list)
    subtitle_codecs: List[str] = field(default_factory=list)
    video_streams: int = 0

    @property
    def quality(self) -> Optional[str]:
        if not self.height:
            return None
        for h in (2160, 1440, 1080, 720, 576, 480, 360, 240):
            if self.height >= h * 0.9:
                return f"{h}p"
        return f"{self.height}p"

    @property
    def is_mp4(self) -> bool:
        return "mp4" in self.format_name.split(",") or "mov" in self.format_name.split(",")

    def to_doc(self) -> dict:
        d = asdict(self)
        d["quality"] = self.quality
        return d


@dataclass
class ProcessResult:
    path: str
    action: str                  # none | remux | transcode
    container: str               # mp4 | matroska | ...
    info: MediaInfo
    created: bool                # True if `path` is a new temp file the pipeline may delete

    def to_doc(self) -> dict:
        return {"path": self.path, "action": self.action, "container": self.container,
                "created": self.created, "media": self.info.to_doc()}


def find_tool(name: str) -> Optional[str]:
    return shutil.which(name)


def tools_available() -> dict:
    return {"ffmpeg": bool(find_tool("ffmpeg")), "ffprobe": bool(find_tool("ffprobe"))}


async def _run(cmd: List[str], timeout: int, on_line: Optional[Callable[[str], Awaitable[None]]] = None):
    """Run a subprocess with a hard timeout. Returns (returncode, stdout, stderr_tail)."""
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out_chunks: List[bytes] = []
    err_tail = bytearray()

    async def read_out():
        assert proc.stdout
        async for line in proc.stdout:
            if on_line:
                await on_line(line.decode(errors="ignore").strip())
            else:
                out_chunks.append(line)

    async def read_err():
        assert proc.stderr
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            err_tail.extend(chunk)
            del err_tail[:-4000]          # keep memory bounded

    try:
        await asyncio.wait_for(asyncio.gather(read_out(), read_err(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise ProcessingError(f"{os.path.basename(cmd[0])} timed out after {timeout}s")
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    return proc.returncode, b"".join(out_chunks), bytes(err_tail).decode(errors="ignore")


async def probe(path: str, timeout: int = 120) -> MediaInfo:
    exe = find_tool("ffprobe")
    if not exe:
        raise FFmpegMissing("ffprobe")
    if not os.path.isfile(path):
        raise MediaRejected("File does not exist")
    rc, out, err = await _run([exe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path], timeout)
    if rc != 0:
        raise MediaRejected(f"ffprobe could not read the file: {err.strip()[:200]}")
    try:
        data = json.loads(out or b"{}")
    except ValueError:
        raise MediaRejected("ffprobe returned invalid output")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"
             and not (s.get("disposition") or {}).get("attached_pic")]
    info = MediaInfo(
        format_name=str(fmt.get("format_name") or ""),
        duration=_float(fmt.get("duration")) or max((_float(s.get("duration")) or 0 for s in streams), default=0),
        size=int(fmt.get("size") or os.path.getsize(path)),
        video_streams=len(video),
        audio_codecs=[s.get("codec_name") or "?" for s in streams if s.get("codec_type") == "audio"],
        subtitle_codecs=[s.get("codec_name") or "?" for s in streams if s.get("codec_type") == "subtitle"],
    )
    if video:
        v = video[0]
        info.video_codec = v.get("codec_name")
        info.width, info.height = _int(v.get("width")), _int(v.get("height"))
    return info


def check_media(info: MediaInfo, cfg) -> None:
    """Reject files that are not a usable episode. Raises MediaRejected."""
    names = set(info.format_name.split(","))
    if not names & set(SUPPORTED_FORMATS):
        raise MediaRejected(f"Unsupported format '{info.format_name}'")
    if info.video_streams < 1:
        raise MediaRejected("No video stream")
    if cfg.require_audio and not info.audio_codecs:
        raise MediaRejected("No audio stream")
    if not info.duration or info.duration < cfg.min_duration_seconds:
        raise MediaRejected(f"Duration too short ({info.duration or 0:.0f}s < {cfg.min_duration_seconds}s)")
    if info.duration > cfg.max_duration_seconds:
        raise MediaRejected(f"Duration too long ({info.duration:.0f}s)")
    if info.size > cfg.max_file_size_bytes:
        raise MediaRejected(f"File too large ({info.size} bytes; limit {cfg.max_file_size_bytes})")


def plan(info: MediaInfo, cfg) -> str:
    """Decide what to do: none | remux | transcode."""
    mode = cfg.process_mode
    if mode in ("off", "copy"):
        return "none"
    if mode == "transcode":
        return "transcode"
    # auto: never re-encode; remux to MP4 only when it loses nothing.
    if cfg.max_output_height and info.height and info.height > cfg.max_output_height * 1.05:
        return "transcode"
    mp4_ok = info.video_codec in MP4_VIDEO_CODECS and all(a in MP4_AUDIO_CODECS for a in info.audio_codecs)
    if info.is_mp4:
        return "none"
    if mp4_ok and all(s in MP4_TEXT_SUBS for s in info.subtitle_codecs):
        return "remux"       # e.g. MKV with H.264/AAC and no subtitles → streamable MP4
    return "none"            # e.g. MKV with ASS subtitles: keep as-is, upload as a document


def ensure_disk(dir_path: str, needed_bytes: int, headroom_mb: int) -> None:
    os.makedirs(dir_path, exist_ok=True)
    free = shutil.disk_usage(dir_path).free
    need = needed_bytes + headroom_mb * 1024 * 1024
    if free < need:
        raise ProcessingError(f"Not enough disk space in {dir_path}: {free // 2**20} MB free, "
                              f"{need // 2**20} MB needed", transient=True)


def _build_cmd(exe: str, src: str, dst: str, action: str, info: MediaInfo, cfg) -> List[str]:
    cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
           "-progress", "pipe:1", "-nostats", "-map", "0:v:0", "-map", "0:a?"]
    if action == "remux":
        cmd += ["-map", "0:s?", "-c", "copy"]
    else:
        vf = []
        if cfg.max_output_height and info.height and info.height > cfg.max_output_height:
            vf.append(f"scale=-2:{int(cfg.max_output_height)}")
        cmd += ["-c:v", cfg.ffmpeg_video_codec, "-preset", cfg.ffmpeg_preset, "-crf", str(cfg.ffmpeg_crf),
                "-pix_fmt", "yuv420p", "-threads", str(cfg.ffmpeg_threads),
                "-c:a", cfg.ffmpeg_audio_codec, "-b:a", cfg.ffmpeg_audio_bitrate]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        if dst.endswith(".mkv"):
            cmd += ["-map", "0:s?", "-c:s", "copy"]
        else:
            cmd += ["-sn"]       # MP4 can't hold ASS/PGS subtitles
    if dst.endswith(".mp4"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(dst)
    return cmd


async def process(src: str, work_dir: str, cfg, *, base_name: str = "episode",
                  keepalive: Optional[Callable[[], Awaitable[None]]] = None) -> ProcessResult:
    """Validate ``src`` and produce the file to upload. ``src`` is never touched."""
    if cfg.process_mode == "off":
        # Validation only when ffprobe happens to be installed; never modifies the file.
        try:
            info = await probe(src)
        except FFmpegMissing:
            info = None
        if info:
            check_media(info, cfg)
        return ProcessResult(src, "none", _container(info, src), info or _blank(src), created=False)

    info = await _maybe_probe(src, cfg)
    if info is None:   # ffprobe missing and not required
        return ProcessResult(src, "none", _container(None, src), _blank(src), created=False)
    check_media(info, cfg)
    action = plan(info, cfg)
    if action == "none":
        return ProcessResult(src, "none", _container(info, src), info, created=False)

    exe = find_tool("ffmpeg")
    if not exe:
        raise FFmpegMissing("ffmpeg")
    ext = ".mp4" if action == "remux" or cfg.output_container == "mp4" else ".mkv"
    os.makedirs(work_dir, exist_ok=True)
    ensure_disk(work_dir, info.size if action == "remux" else int(info.size * 1.2), cfg.disk_headroom_mb)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in base_name)[:100] or "episode"
    dst = os.path.join(work_dir, f"{safe}{ext}")
    if os.path.realpath(dst) == os.path.realpath(src):
        dst = os.path.join(work_dir, f"{safe}.out{ext}")
    part = dst + ".part" + ext          # keep the extension so ffmpeg picks the muxer
    last = {"t": 0.0}

    async def on_line(line: str):
        if line.startswith("out_time_us=") and keepalive:
            us = _float(line.split("=", 1)[1]) or 0
            if us / 1e6 - last["t"] >= 60:
                last["t"] = us / 1e6
                await keepalive()

    log.info("FFmpeg %s: %s → %s", action, os.path.basename(src), os.path.basename(dst))
    try:
        rc, _, err = await _run(_build_cmd(exe, src, part, action, info, cfg), cfg.ffmpeg_timeout_seconds, on_line)
        if rc != 0:
            raise ProcessingError(f"ffmpeg {action} failed: {err.strip()[-300:]}", transient=False)
        out_info = await probe(part)
        check_media(out_info, cfg)
        if abs(out_info.duration - info.duration) > max(5.0, info.duration * 0.02):
            raise ProcessingError(f"Output duration {out_info.duration:.0f}s differs from source "
                                  f"{info.duration:.0f}s", transient=False)
        os.replace(part, dst)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(part)
        raise
    return ProcessResult(dst, action, "mp4" if ext == ".mp4" else "matroska", out_info, created=True)


async def _maybe_probe(path: str, cfg) -> Optional[MediaInfo]:
    try:
        return await probe(path)
    except FFmpegMissing:
        if cfg.ffmpeg_required:
            raise
        log.warning("ffprobe missing and FFMPEG_REQUIRED=false: uploading without stream validation")
        return None


def _container(info: Optional[MediaInfo], path: str) -> str:
    if info and info.is_mp4:
        return "mp4"
    if info and "matroska" in info.format_name:
        return "matroska"
    return "mp4" if path.lower().endswith((".mp4", ".m4v")) else "matroska"


def _blank(path: str) -> MediaInfo:
    return MediaInfo(format_name="", duration=0.0, size=os.path.getsize(path) if os.path.isfile(path) else 0)


def _float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
