"""Original entry module, kept for compatibility (``from bot import run``).

The original handlers (/start deep links, /help, file → share link, /stats)
now live in ``anime_soul/handlers/user.py`` and ``anime_soul/handlers/admin.py``
with the same behaviour, plus FSUB and ANIME SOUL episode delivery.
"""
from anime_soul.runner import run

__all__ = ["run"]
