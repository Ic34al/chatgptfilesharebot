"""Force-subscription checks. Fail closed: any doubt means "not verified"."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from pyrogram import enums
from pyrogram.errors import RPCError, UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .tg import call_with_flood, esc
from .timeutil import ensure_utc, utcnow

log = logging.getLogger(__name__)
OK_STATUSES = {enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.MEMBER}


@dataclass
class Missing:
    chat: object        # int id or @username
    title: str
    link: Optional[str]


class FsubChecker:
    def __init__(self, client, db, cfg, notifier=None):
        self.client = client
        self.db = db
        self.cfg = cfg
        self.notifier = notifier
        self._chat_cache: dict = {}

    async def is_member(self, chat, user_id: int) -> bool:
        """True only if Telegram positively confirms membership (or a fresh join request exists)."""
        try:
            member = await call_with_flood(self.client.get_chat_member, chat, user_id, max_waits=1, max_wait_s=30)
        except UserNotParticipant:
            return await self._has_fresh_join_request(chat, user_id)
        except RPCError as e:
            log.warning("FSUB check failed for chat %s: %s", chat, e)
            if self.notifier:
                await self.notifier.alert(f"⚠️ FSUB check failed for <code>{esc(chat)}</code>: {esc(e)}\n"
                                          "Make sure the bot is an admin in every FSUB channel.",
                                          key=f"fsub-err:{chat}")
            return False
        except Exception:
            log.exception("Unexpected FSUB error")
            return False
        status = getattr(member, "status", None)
        if status in OK_STATUSES:
            return True
        if status == enums.ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False):
            return True
        if status in (enums.ChatMemberStatus.LEFT, None):
            return await self._has_fresh_join_request(chat, user_id)
        return False  # BANNED or anything unexpected

    async def _has_fresh_join_request(self, chat, user_id) -> bool:
        if not self.cfg.fsub_join_request:
            return False
        chat_id = await self._chat_id(chat)
        if chat_id is None:
            return False
        doc = await self.db.get_join_request(user_id, chat_id)
        if not doc:
            return False
        if utcnow() - ensure_utc(doc["at"]) > timedelta(hours=self.cfg.fsub_join_request_ttl_hours):
            await self.db.clear_join_request(user_id, chat_id)   # stale: never grants access
            return False
        return True

    async def _chat(self, chat):
        if chat not in self._chat_cache:
            try:
                self._chat_cache[chat] = await self.client.get_chat(chat)
            except RPCError as e:
                log.warning("Cannot read FSUB chat %s: %s", chat, e)
                return None
        return self._chat_cache[chat]

    async def _chat_id(self, chat) -> Optional[int]:
        if isinstance(chat, int):
            return chat
        c = await self._chat(chat)
        return getattr(c, "id", None)

    async def invite_link(self, chat) -> Optional[str]:
        key = f"fsub_link:{chat}:{int(self.cfg.fsub_join_request)}"
        cached = await self.db.get_setting(key)
        if cached:
            return cached
        c = await self._chat(chat)
        link = None
        if c is not None and getattr(c, "username", None) and not self.cfg.fsub_join_request:
            link = f"https://t.me/{c.username}"
        else:
            try:
                inv = await self.client.create_chat_invite_link(
                    chat, name="ANIME SOUL FSUB", creates_join_request=self.cfg.fsub_join_request or None)
                link = inv.invite_link
            except RPCError as e:
                log.warning("Could not create invite link for %s: %s", chat, e)
                if isinstance(chat, str):
                    link = f"https://t.me/{chat.lstrip('@')}"
        if link:
            await self.db.set_setting(key, link)
        return link

    async def missing(self, user_id: int) -> List[Missing]:
        out = []
        for chat in self.cfg.fsub_channels:
            if await self.is_member(chat, user_id):
                continue
            c = await self._chat(chat)
            out.append(Missing(chat, getattr(c, "title", None) or str(chat), await self.invite_link(chat)))
        return out

    @staticmethod
    def keyboard(missing: List[Missing], retry_data: str) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(f"📢 Join {m.title}"[:60], url=m.link)] for m in missing if m.link]
        rows.append([InlineKeyboardButton("🔄 TRY AGAIN", callback_data=retry_data)])
        return InlineKeyboardMarkup(rows)
