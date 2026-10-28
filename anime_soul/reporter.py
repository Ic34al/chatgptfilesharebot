"""Optional per-episode progress messages in a log channel (LOG_CHANNEL_ID).

Adapted from the idea of ``Reporter`` in kaif-00z/AutoAnimeBot libs/logger.py (GPL-3.0): one message
per release, edited as the job moves on (Downloading → Encoding → Uploading → DONE), plus error
reports. Independent Pyrogram implementation; no code copied. Off unless LOG_CHANNEL_ID is set.
The message ID is stored on the episode (``report_message_id``), so edits work after a restart.
"""
from __future__ import annotations

import logging

from pyrogram.errors import RPCError

from . import models as M
from .logging_setup import redact
from .tg import call_with_flood, esc

log = logging.getLogger(__name__)

STATUS_TEXT = {
    M.AVAILABLE: "🔎 File found — queued",
    M.DOWNLOADING: "⬇️ Downloading…",
    M.VALIDATING: "🧪 Validating…",
    M.PROCESSING: "🎞 Processing (FFmpeg)…",
    M.UPLOADING: "⬆️ Uploading to storage…",
    M.UPLOADED: "📦 Stored — publishing…",
    M.PUBLISHED: "✅ DONE — published",
    M.FAILED: "❌ Failed",
}


class JobReporter:
    def __init__(self, client, chat_id, db):
        self.client = client
        self.chat_id = chat_id
        self.db = db

    async def on_state(self, key: str, state: str, fields: dict) -> None:
        if state not in STATUS_TEXT:
            return
        try:
            ep = await self.db.get_episode(key) or {}
            anime = await self.db.get_anime(int(ep.get("anime_id") or M.parse_episode_key(key)[0])) or {}
            title = anime.get("title_english") or anime.get("title_romaji") or key
            cand = ep.get("candidate") or {}
            lines = [f"<b>{esc(title)}</b> — episode {esc(ep.get('episode', '?'))}",
                     f"File: <code>{esc(cand.get('name') or '—')}</code> ({esc(cand.get('source') or '—')})",
                     f"STATUS: {STATUS_TEXT[state]}"]
            err = fields.get("last_error")
            if state == M.FAILED and err:
                lines.append(f"Error: <code>{esc(redact(str(err))[:300])}</code>")
            text = "\n".join(lines)
            mid = ep.get("report_message_id")
            if mid:
                try:
                    await call_with_flood(self.client.edit_message_text, self.chat_id, mid, text,
                                          disable_web_page_preview=True)
                    return
                except RPCError as e:  # deleted or too old: send a new one
                    if "MESSAGE_NOT_MODIFIED" in str(e):
                        return
            msg = await call_with_flood(self.client.send_message, self.chat_id, text, disable_web_page_preview=True)
            await self.db.episodes.update_one({"_id": key}, {"$set": {"report_message_id": msg.id}})
        except Exception as e:  # noqa: BLE001 - progress reporting must never affect a job
            log.debug("Progress report for %s failed: %s", key, e)
