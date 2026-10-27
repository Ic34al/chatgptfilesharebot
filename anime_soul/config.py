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


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


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
    return tuple(int(v) for v in raw.replace(" ", ",").split(",") if v.strip())


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
    default_quality: str = "1080p"
    default_language: str = "Japanese (English Sub)"
    protect_content: bool = False
    public_file_links: bool = True
    log_level: str = "INFO"
    log_json: bool = False
    jikan_fallback: bool = True

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
    role = os.getenv("ROLE", "all").strip().lower() or "all"
    if role not in {"all", "bot", "pipeline"}:
        raise ValueError("ROLE must be one of: all, bot, pipeline")
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
        dry_run=_bool("DRY_RUN", False),
        auto_track_sequels=_bool("AUTO_TRACK_SEQUELS", True),
        default_quality=os.getenv("DEFAULT_QUALITY", "1080p"),
        default_language=os.getenv("DEFAULT_LANGUAGE", "Japanese (English Sub)"),
        protect_content=_bool("PROTECT_CONTENT", False),
        public_file_links=_bool("PUBLIC_FILE_LINKS", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_json=_bool("LOG_JSON", False),
        jikan_fallback=_bool("JIKAN_FALLBACK", True),
    )
