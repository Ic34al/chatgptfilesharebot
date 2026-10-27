"""Environment-driven configuration for ANIME SOUL.

Every secret and every channel ID comes from environment variables (Heroku
Config Vars). Nothing sensitive is hardcoded here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

ChatRef = Union[int, str]


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


class ConfigError(ValueError):
    """A config var has an invalid value. The message names the variable, never its value."""


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a whole number")


def _chat_ref(value: str) -> ChatRef:
    value = value.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    if value.startswith("https://t.me/"):
        value = value[len("https://t.me/"):]
    return value if value.startswith("@") else "@" + value


def _chat_list(name: str) -> Tuple[ChatRef, ...]:
    raw = os.getenv(name, "")
    return tuple(_chat_ref(v) for v in raw.replace(" ", ",").split(",") if v.strip())


def _int_list(name: str) -> Tuple[int, ...]:
    raw = os.getenv(name, "")
    try:
        return tuple(int(v) for v in raw.replace(" ", ",").split(",") if v.strip())
    except ValueError:
        raise ConfigError(f"{name} must be comma-separated numeric Telegram IDs")


def _choice(name: str, default: str, allowed: Tuple[str, ...]) -> str:
    value = (os.getenv(name) or default).strip().lower()
    if value not in allowed:
        raise ConfigError(f"{name} must be one of: {', '.join(allowed)}")
    return value


def _str_list(name: str) -> Tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(v.strip().lower() for v in raw.split(",") if v.strip())


@dataclass(frozen=True)
class Config:
    # --- Existing variables (unchanged names) ---
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    mongodb_uri: str = ""
    database_name: str = "file_share_bot"
    owner_id: int = 0

    # --- ANIME SOUL additions ---
    admin_ids: Tuple[int, ...] = ()
    storage_channel_id: Optional[ChatRef] = None
    post_channel_id: Optional[ChatRef] = None
    channel_name: str = "ANIME SOUL"
    fsub_channels: Tuple[ChatRef, ...] = ()
    fsub_join_request: bool = False
    fsub_join_request_ttl_hours: int = 72
    fsub_legacy_links: bool = False
    role: str = "all"  # all | bot | pipeline
    poll_interval_seconds: int = 600
    source_check_interval_seconds: int = 300
    lookahead_hours: int = 48
    lookback_hours: int = 72
    delay_alert_minutes: int = 180
    source_giveup_hours: int = 168
    max_retries: int = 8
    max_concurrent_jobs: int = 1
    job_lease_seconds: int = 900
    max_file_size_mb: int = 2000
    min_file_size_mb: int = 5
    download_timeout_seconds: int = 3600
    upload_timeout_seconds: int = 3600
    download_dir: str = "/tmp/anime_soul"
    allowed_source_hosts: Tuple[str, ...] = ()
    allow_http_sources: bool = False
    local_source_roots: Tuple[str, ...] = ()
    dry_run: bool = False
    auto_track_sequels: bool = True
    auto_discover_airing_anime: bool = True
    auto_discover_max_pages: int = 4        # 50 anime per page, most popular first (max 10)
    default_quality: str = "1080p"
    default_language: str = "Japanese (English Sub)"
    protect_content: bool = False
    public_file_links: bool = True
    log_level: str = "INFO"
    log_json: bool = False
    jikan_fallback: bool = True

    # --- Full pipeline additions (FFmpeg, feeds, test mode) ---
    process_mode: str = "auto"          # auto | copy | transcode | off
    ffmpeg_required: bool = True        # refuse to upload unvalidated files when ffprobe is missing
    ffmpeg_timeout_seconds: int = 5400
    ffmpeg_threads: int = 2
    ffmpeg_video_codec: str = "libx264"
    ffmpeg_preset: str = "veryfast"
    ffmpeg_crf: int = 23
    ffmpeg_audio_codec: str = "aac"
    ffmpeg_audio_bitrate: str = "128k"
    output_container: str = "mkv"       # container for transcodes: mkv (keeps subtitles) | mp4
    max_output_height: int = 0          # 0 = keep source resolution; e.g. 720 downscales larger files
    min_duration_seconds: int = 30
    max_duration_seconds: int = 4 * 3600
    require_audio: bool = True
    disk_headroom_mb: int = 300
    process_telegram_files: bool = False
    feed_detect_releases: bool = True
    dry_run_process: bool = False       # dry run also downloads + validates + processes (never uploads)
    test_channel_id: Optional[ChatRef] = None

    @property
    def all_admin_ids(self) -> Tuple[int, ...]:
        ids = set(self.admin_ids)
        if self.owner_id:
            ids.add(self.owner_id)
        return tuple(sorted(ids))

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def min_file_size_bytes(self) -> int:
        return self.min_file_size_mb * 1024 * 1024

    def missing_for_role(self) -> List[str]:
        """Return names of required variables that are missing."""
        missing = []
        for key, val in (("API_ID", self.api_id), ("API_HASH", self.api_hash),
                         ("BOT_TOKEN", self.bot_token), ("MONGODB_URI", self.mongodb_uri)):
            if not val:
                missing.append(key)
        if self.role in ("all", "pipeline"):
            if self.storage_channel_id is None:
                missing.append("STORAGE_CHANNEL_ID")
            if self.post_channel_id is None:
                missing.append("POST_CHANNEL_ID")
        return missing


def load_config() -> Config:
    storage = os.getenv("STORAGE_CHANNEL_ID", "").strip()
    post = os.getenv("POST_CHANNEL_ID", "").strip()
    test_chat = os.getenv("TEST_CHANNEL_ID", "").strip()
    role = os.getenv("ROLE", "all").strip().lower() or "all"
    if role not in {"all", "bot", "pipeline"}:
        raise ConfigError("ROLE must be one of: all, bot, pipeline")
    return Config(
        api_id=_int("API_ID", 0),
        api_hash=os.getenv("API_HASH", ""),
        bot_token=os.getenv("BOT_TOKEN", ""),
        mongodb_uri=os.getenv("MONGODB_URI", ""),
        database_name=os.getenv("DATABASE_NAME", "file_share_bot"),
        owner_id=_int("OWNER_ID", 0),
        admin_ids=_int_list("ADMIN_IDS"),
        storage_channel_id=_chat_ref(storage) if storage else None,
        post_channel_id=_chat_ref(post) if post else None,
        channel_name=os.getenv("CHANNEL_NAME", "ANIME SOUL"),
        fsub_channels=_chat_list("FSUB_CHANNELS"),
        fsub_join_request=_bool("FSUB_JOIN_REQUEST", False),
        fsub_join_request_ttl_hours=_int("FSUB_JOIN_REQUEST_TTL_HOURS", 72),
        fsub_legacy_links=_bool("FSUB_LEGACY_LINKS", False),
        role=role,
        poll_interval_seconds=max(60, _int("POLL_INTERVAL_SECONDS", 600)),
        source_check_interval_seconds=max(30, _int("SOURCE_CHECK_INTERVAL_SECONDS", 300)),
        lookahead_hours=_int("LOOKAHEAD_HOURS", 48),
        lookback_hours=_int("LOOKBACK_HOURS", 72),
        delay_alert_minutes=_int("DELAY_ALERT_MINUTES", 180),
        source_giveup_hours=_int("SOURCE_GIVEUP_HOURS", 168),
        max_retries=_int("MAX_RETRIES", 8),
        max_concurrent_jobs=max(1, _int("MAX_CONCURRENT_JOBS", 1)),
        job_lease_seconds=max(120, _int("JOB_LEASE_SECONDS", 900)),
        max_file_size_mb=min(2000, _int("MAX_FILE_SIZE_MB", 2000)),
        min_file_size_mb=_int("MIN_FILE_SIZE_MB", 5),
        download_timeout_seconds=_int("DOWNLOAD_TIMEOUT_SECONDS", 3600),
        upload_timeout_seconds=_int("UPLOAD_TIMEOUT_SECONDS", 3600),
        download_dir=os.getenv("DOWNLOAD_DIR", "/tmp/anime_soul"),
        allowed_source_hosts=_str_list("ALLOWED_SOURCE_HOSTS"),
        allow_http_sources=_bool("ALLOW_HTTP_SOURCES", False),
        local_source_roots=tuple(v.strip() for v in os.getenv("LOCAL_SOURCE_ROOTS", "").split(",") if v.strip()),
        dry_run=_bool("DRY_RUN", True),   # safe default: nothing is uploaded/posted until DRY_RUN=false
        auto_track_sequels=_bool("AUTO_TRACK_SEQUELS", True),
        auto_discover_airing_anime=_bool(
            "AUTO_DISCOVER_AIRING_ANIME",
            True,
        ),
        auto_discover_max_pages=min(10, max(1, _int("AUTO_DISCOVER_MAX_PAGES", 4))),
        default_quality=os.getenv("DEFAULT_QUALITY", "1080p"),
        default_language=os.getenv("DEFAULT_LANGUAGE", "Japanese (English Sub)"),
        protect_content=_bool("PROTECT_CONTENT", False),
        public_file_links=_bool("PUBLIC_FILE_LINKS", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_json=_bool("LOG_JSON", False),
        jikan_fallback=_bool("JIKAN_FALLBACK", True),
        process_mode=_choice("PROCESS_MODE", "auto", ("auto", "copy", "transcode", "off")),
        ffmpeg_required=_bool("FFMPEG_REQUIRED", True),
        ffmpeg_timeout_seconds=max(60, _int("FFMPEG_TIMEOUT_SECONDS", 5400)),
        ffmpeg_threads=max(1, _int("FFMPEG_THREADS", 2)),
        ffmpeg_video_codec=os.getenv("FFMPEG_VIDEO_CODEC", "libx264").strip() or "libx264",
        ffmpeg_preset=os.getenv("FFMPEG_PRESET", "veryfast").strip() or "veryfast",
        ffmpeg_crf=min(51, max(0, _int("FFMPEG_CRF", 23))),
        ffmpeg_audio_codec=os.getenv("FFMPEG_AUDIO_CODEC", "aac").strip() or "aac",
        ffmpeg_audio_bitrate=os.getenv("FFMPEG_AUDIO_BITRATE", "128k").strip() or "128k",
        output_container=_choice("OUTPUT_CONTAINER", "mkv", ("mkv", "mp4")),
        max_output_height=max(0, _int("MAX_OUTPUT_HEIGHT", 0)),
        min_duration_seconds=max(1, _int("MIN_DURATION_SECONDS", 30)),
        max_duration_seconds=max(60, _int("MAX_DURATION_SECONDS", 4 * 3600)),
        require_audio=_bool("REQUIRE_AUDIO", True),
        disk_headroom_mb=max(0, _int("DISK_HEADROOM_MB", 300)),
        process_telegram_files=_bool("PROCESS_TELEGRAM_FILES", False),
        feed_detect_releases=_bool("FEED_DETECT_RELEASES", True),
        dry_run_process=_bool("DRY_RUN_PROCESS", False),
        test_channel_id=_chat_ref(test_chat) if test_chat else None,
    )
