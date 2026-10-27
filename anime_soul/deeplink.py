"""Deep links for the GET EPISODE button (https://t.me/<bot>?start=<token>)."""
from __future__ import annotations

import re

from .models import is_valid_token

_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def build_link(bot_username: str, token: str) -> str:
    username = (bot_username or "").lstrip("@")
    if not _USERNAME.fullmatch(username):
        raise ValueError("Invalid bot username")
    if not is_valid_token(token):
        raise ValueError("Invalid deep-link token")
    return f"https://t.me/{username}?start={token}"


def parse_start_payload(text: str) -> str | None:
    """Extract and validate the /start payload; returns None if absent/invalid."""
    parts = (text or "").split(maxsplit=1)
    if len(parts) < 2:
        return None
    token = parts[1].strip()
    return token if is_valid_token(token) else None
