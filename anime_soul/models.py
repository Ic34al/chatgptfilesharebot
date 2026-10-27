"""State constants and identifiers for the episode pipeline."""
from __future__ import annotations

import re
import secrets

# Episode lifecycle
SCHEDULED = "scheduled"        # known future air time
AIRING = "airing"              # air time passed, waiting for an authorized file
AVAILABLE = "available"        # an authorized source has a matching file
DOWNLOADING = "downloading"
UPLOADING = "uploading"
UPLOADED = "uploaded"          # stored in Telegram storage channel, not yet posted
PUBLISHED = "published"
FAILED = "failed"
DRY_RUN_OK = "dry_run_ok"      # dry run finished; nothing uploaded

ALL_STATES = (SCHEDULED, AIRING, AVAILABLE, DOWNLOADING, UPLOADING, UPLOADED,
              PUBLISHED, FAILED, DRY_RUN_OK)
# States that the pipeline worker may claim.
WORKABLE_STATES = (AIRING, AVAILABLE, DOWNLOADING, UPLOADING, UPLOADED)
TERMINAL_STATES = (PUBLISHED, FAILED, DRY_RUN_OK)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
EPISODE_TOKEN_PREFIX = "ep"


def episode_key(anime_id: int, episode: int) -> str:
    return f"anilist:{int(anime_id)}:{int(episode)}"


def parse_episode_key(key: str) -> tuple[int, int]:
    m = re.fullmatch(r"anilist:(\d{1,9}):(\d{1,5})", key or "")
    if not m:
        raise ValueError(f"Invalid episode key: {key!r}")
    return int(m.group(1)), int(m.group(2))


def new_episode_token() -> str:
    """Unguessable deep-link token (~96 bits). Fits Telegram's 64-char limit."""
    return f"{EPISODE_TOKEN_PREFIX}{secrets.token_urlsafe(12)}"


def new_file_token() -> str:
    return secrets.token_urlsafe(12)


def is_valid_token(token: str) -> bool:
    return isinstance(token, str) and bool(TOKEN_RE.fullmatch(token))
