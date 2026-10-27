"""Telegram helpers: FloodWait-aware calls and admin notifications."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Iterable

from pyrogram.errors import FloodWait, RPCError

log = logging.getLogger(__name__)


async def call_with_flood(fn: Callable[..., Awaitable[Any]], *args, max_waits: int = 3, max_wait_s: int = 900,
                          **kwargs) -> Any:
    """Call a Pyrogram coroutine, honouring FloodWait (retry-after) a bounded number of times."""
    for i in range(max_waits + 1):
        try:
            return await fn(*args, **kwargs)
        except FloodWait as e:
            wait = int(getattr(e, "value", 0) or 0)
            if i >= max_waits or wait > max_wait_s:
                raise
            log.warning("FloodWait %ss on %s", wait, getattr(fn, "__name__", fn))
            await asyncio.sleep(wait + 1)


class Notifier:
    """Sends alerts to admins with per-key cooldown so problems don't spam."""

    def __init__(self, client, admin_ids: Iterable[int], cooldown_s: int = 3600):
        self.client = client
        self.admin_ids = tuple(admin_ids)
        self.cooldown_s = cooldown_s
        self._last: Dict[str, float] = {}

    async def alert(self, text: str, key: str | None = None) -> None:
        if key:
            now = time.monotonic()
            if now - self._last.get(key, -1e9) < self.cooldown_s:
                return
            self._last[key] = now
        for admin in self.admin_ids:
            try:
                await call_with_flood(self.client.send_message, admin, text[:4000],
                                      disable_web_page_preview=True)
            except RPCError as e:
                log.warning("Could not notify admin %s: %s", admin, e)
            except Exception:  # never let alerting crash a worker
                log.exception("Admin notification failed")


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)
