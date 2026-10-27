"""MongoDB persistence for the ANIME SOUL pipeline.

The pipeline is idempotent: every episode has one document keyed by
``anilist:<anime_id>:<episode>`` and workers claim it with an atomic lease, so
restarts and multiple dynos never process the same episode twice at once.
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from . import models as M
from .timeutil import utcnow

log = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


class AnimeDB:
    def __init__(self, database):
        self.db = database
        self.anime = database["anime"]
        self.episodes = database["episodes"]
        self.posts = database["posts"]
        self.captions = database["caption_templates"]
        self.sources = database["sources"]
        self.settings = database["settings"]
        self.job_errors = database["job_errors"]
        self.users = database["users"]
        self.fsub_requests = database["fsub_requests"]
        self.inbox = database["inbox"]
        self.files = database["files"]  # legacy collection shared with database.FileDB

    # ------------------------------------------------------------------ setup
    async def ensure_indexes(self) -> None:
        await self.episodes.create_index([("anime_id", ASCENDING), ("episode", ASCENDING)], unique=True)
        await self.episodes.create_index([("state", ASCENDING), ("next_attempt_at", ASCENDING)])
        await self.episodes.create_index("token", unique=True, sparse=True)
        await self.anime.create_index("active")
        await self.job_errors.create_index("at", expireAfterSeconds=30 * 24 * 3600)
        await self.job_errors.create_index("episode_key")
        await self.fsub_requests.create_index([("user_id", ASCENDING), ("chat_id", ASCENDING)], unique=True)
        await self.fsub_requests.create_index("at", expireAfterSeconds=30 * 24 * 3600)
        await self.files.create_index("unique_id")

    # --------------------------------------------------------------- settings
    async def get_setting(self, key: str, default: Any = None) -> Any:
        doc = await self.settings.find_one({"_id": key})
        return default if doc is None else doc.get("value", default)

    async def set_setting(self, key: str, value: Any) -> None:
        await self.settings.update_one({"_id": key}, {"$set": {"value": value, "updated_at": utcnow()}}, upsert=True)

    async def acquire_lock(self, name: str, ttl_seconds: int, owner: str = None) -> bool:
        """Leader lock (one release monitor across all dynos). Re-entrant for the same owner."""
        owner = owner or WORKER_ID
        now = utcnow()
        until = now + timedelta(seconds=ttl_seconds)
        try:
            doc = await self.settings.find_one_and_update(
                {"_id": f"lock:{name}", "$or": [{"owner": owner}, {"until": {"$lt": now}}]},
                {"$set": {"owner": owner, "until": until, "updated_at": now}}, upsert=True,
                return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:     # someone else holds a live lock
            return False
        return bool(doc and doc.get("owner") == owner)

    async def release_lock(self, name: str, owner: str = None) -> None:
        await self.settings.delete_one({"_id": f"lock:{name}", "owner": owner or WORKER_ID})

    async def lock_holder(self, name: str):
        doc = await self.settings.find_one({"_id": f"lock:{name}"})
        if not doc or _aware(doc.get("until")) < utcnow():
            return None
        return doc.get("owner")

    async def is_paused(self) -> bool:
        return bool(await self.get_setting("paused", False))

    async def heartbeat(self, component: str) -> None:
        await self.set_setting(f"heartbeat:{component}", {"at": utcnow(), "worker": WORKER_ID})

    # ------------------------------------------------------------------ anime
    async def upsert_anime(self, anime_id: int, data: Dict[str, Any]) -> bool:
        """Insert or update a tracked anime. Returns True if newly added."""
        now = utcnow()
        res = await self.anime.update_one(
            {"_id": int(anime_id)},
            {"$set": {**data, "updated_at": now},
             "$setOnInsert": {"added_at": now, "active": True}},
            upsert=True,
        )
        return res.upserted_id is not None

    async def get_anime(self, anime_id: int) -> Optional[dict]:
        return await self.anime.find_one({"_id": int(anime_id)})

    async def list_anime(self, active_only: bool = True) -> List[dict]:
        q = {"active": True} if active_only else {}
        return await self.anime.find(q).sort("title_romaji", 1).to_list(length=1000)

    async def set_anime_active(self, anime_id: int, active: bool) -> bool:
        res = await self.anime.update_one({"_id": int(anime_id)}, {"$set": {"active": active, "updated_at": utcnow()}})
        return res.matched_count > 0

    # --------------------------------------------------------------- episodes
    async def upsert_schedule(self, anime_id: int, episode: int, airing_at, source: str,
                              estimated: bool = False) -> str:
        """Create or update an episode schedule. Returns 'new', 'rescheduled' or 'same'.

        Only episodes that have not started processing are rescheduled; an
        episode that is already uploaded/published is never reset.
        """
        key = M.episode_key(anime_id, episode)
        now = utcnow()
        doc = await self.episodes.find_one({"_id": key})
        if doc is None:
            try:
                await self.episodes.insert_one({
                    "_id": key, "anime_id": int(anime_id), "episode": int(episode),
                    "airing_at": airing_at, "schedule_source": source, "estimated": estimated,
                    "state": M.SCHEDULED, "attempts": 0, "next_attempt_at": airing_at,
                    "lease_owner": None, "lease_until": None, "delay_alerted": False,
                    "history": [{"at": now, "event": "scheduled", "airing_at": airing_at, "source": source}],
                    "created_at": now, "updated_at": now,
                })
                return "new"
            except DuplicateKeyError:
                return "same"
        old = doc.get("airing_at")
        if doc.get("state") not in (M.SCHEDULED, M.AIRING):
            return "same"
        # Do not let an estimate overwrite a confirmed time.
        if estimated and not doc.get("estimated", False):
            return "same"
        if old is not None and abs((_aware(old) - _aware(airing_at)).total_seconds()) < 60:
            if doc.get("estimated") and not estimated:
                await self.episodes.update_one({"_id": key}, {"$set": {"estimated": False, "schedule_source": source}})
            return "same"
        update = {"airing_at": airing_at, "schedule_source": source, "estimated": estimated,
                  "next_attempt_at": airing_at, "updated_at": now, "delay_alerted": False}
        if _aware(airing_at) > now and doc.get("state") == M.AIRING:
            update["state"] = M.SCHEDULED
        await self.episodes.update_one(
            {"_id": key},
            {"$set": update,
             "$push": {"history": {"$each": [{"at": now, "event": "rescheduled", "from": old, "to": airing_at,
                                               "source": source}], "$slice": -50}}},
        )
        return "rescheduled"

    async def get_episode(self, key: str) -> Optional[dict]:
        return await self.episodes.find_one({"_id": key})

    async def get_episode_by_token(self, token: str) -> Optional[dict]:
        if not M.is_valid_token(token) or not token.startswith(M.EPISODE_TOKEN_PREFIX):
            return None
        return await self.episodes.find_one({"token": token, "state": {"$in": [M.UPLOADED, M.PUBLISHED]}})

    async def mark_aired(self, now=None) -> List[dict]:
        """Move scheduled episodes whose air time has passed to AIRING."""
        now = now or utcnow()
        docs = await self.episodes.find({"state": M.SCHEDULED, "airing_at": {"$lte": now}}).to_list(length=500)
        moved = []
        for d in docs:
            res = await self.episodes.update_one(
                {"_id": d["_id"], "state": M.SCHEDULED},
                {"$set": {"state": M.AIRING, "next_attempt_at": now, "updated_at": now},
                 "$push": {"history": {"$each": [{"at": now, "event": "aired"}], "$slice": -50}}})
            if res.modified_count:
                moved.append(d)
        return moved

    async def claim_next(self, lease_seconds: int, now=None) -> Optional[dict]:
        """Atomically lease one workable episode. Returns None when idle."""
        now = now or utcnow()
        return await self.episodes.find_one_and_update(
            {"state": {"$in": list(M.WORKABLE_STATES)},
             "next_attempt_at": {"$lte": now},
             "$or": [{"lease_until": None}, {"lease_until": {"$lt": now}}]},
            {"$set": {"lease_owner": WORKER_ID, "lease_until": now + timedelta(seconds=lease_seconds),
                      "updated_at": now}},
            sort=[("airing_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    async def renew_lease(self, key: str, lease_seconds: int) -> bool:
        now = utcnow()
        res = await self.episodes.update_one(
            {"_id": key, "lease_owner": WORKER_ID},
            {"$set": {"lease_until": now + timedelta(seconds=lease_seconds)}})
        return res.matched_count > 0

    async def release(self, key: str, **fields) -> None:
        """Release our lease and optionally set fields (state changes, etc.)."""
        fields.update({"lease_owner": None, "lease_until": None, "updated_at": utcnow()})
        await self.episodes.update_one({"_id": key, "lease_owner": WORKER_ID}, {"$set": fields})

    async def release_all_mine(self) -> int:
        res = await self.episodes.update_many({"lease_owner": WORKER_ID},
                                              {"$set": {"lease_owner": None, "lease_until": None}})
        return res.modified_count

    async def set_state(self, key: str, state: str, **fields) -> None:
        now = utcnow()
        await self.episodes.update_one(
            {"_id": key},
            {"$set": {"state": state, "updated_at": now, **fields},
             "$push": {"history": {"$each": [{"at": now, "event": state}], "$slice": -50}}})

    async def record_error(self, key: str, stage: str, error: str, attempt: int) -> None:
        await self.job_errors.insert_one({"episode_key": key, "stage": stage, "error": error[:2000],
                                          "attempt": attempt, "at": utcnow()})

    async def count_by_state(self) -> Dict[str, int]:
        out = {s: 0 for s in M.ALL_STATES}
        async for row in self.episodes.aggregate([{"$group": {"_id": "$state", "n": {"$sum": 1}}}]):
            out[row["_id"]] = row["n"]
        return out

    async def list_pending(self, limit: int = 30) -> List[dict]:
        return await self.episodes.find(
            {"state": {"$nin": [M.PUBLISHED, M.SCHEDULED]}}).sort("airing_at", 1).to_list(length=limit)

    async def list_by_states(self, states, limit: int = 30) -> List[dict]:
        return await self.episodes.find({"state": {"$in": list(states)}}).sort("airing_at", 1).to_list(length=limit)

    async def list_upcoming(self, limit: int = 20) -> List[dict]:
        return await self.episodes.find({"state": M.SCHEDULED}).sort("airing_at", 1).to_list(length=limit)

    async def reset_for_retry(self, key: str, include_publish: bool = False) -> bool:
        doc = await self.get_episode(key)
        if not doc:
            return False
        state = doc["state"]
        if state == M.PUBLISHED:
            return False
        if doc.get("storage"):
            new_state = M.UPLOADED
        elif doc.get("candidate"):
            new_state = M.AVAILABLE
        else:
            new_state = M.AIRING
        if include_publish:
            await self.posts.delete_one({"_id": key, "state": "publishing"})
        await self.episodes.update_one({"_id": key}, {"$set": {
            "state": new_state, "attempts": 0, "next_attempt_at": utcnow(), "last_error": None,
            "lease_owner": None, "lease_until": None, "updated_at": utcnow()}})
        return True

    # ------------------------------------------------------------------ posts
    async def claim_post(self, key: str, chat_id) -> bool:
        """Insert a post claim. False if the episode was already (being) posted."""
        try:
            await self.posts.insert_one({"_id": key, "state": "publishing", "chat_id": chat_id,
                                         "claimed_at": utcnow(), "worker": WORKER_ID})
            return True
        except DuplicateKeyError:
            return False

    async def get_post(self, key: str) -> Optional[dict]:
        return await self.posts.find_one({"_id": key})

    async def finish_post(self, key: str, message_id: int) -> None:
        await self.posts.update_one({"_id": key}, {"$set": {"state": "published", "message_id": message_id,
                                                           "published_at": utcnow()}})

    async def abort_post(self, key: str) -> None:
        """Remove a claim when the send definitely failed (no message created)."""
        await self.posts.delete_one({"_id": key, "state": "publishing"})

    # --------------------------------------------------------------- captions
    async def get_caption(self, anime_id: Optional[int]) -> Optional[str]:
        if anime_id is not None:
            doc = await self.captions.find_one({"_id": str(int(anime_id))})
            if doc:
                return doc["template"]
        doc = await self.captions.find_one({"_id": "default"})
        return doc["template"] if doc else None

    async def set_caption(self, target: str, template: str) -> None:
        await self.captions.update_one({"_id": target}, {"$set": {"template": template, "updated_at": utcnow()}},
                                       upsert=True)

    async def delete_caption(self, target: str) -> bool:
        res = await self.captions.delete_one({"_id": target})
        return res.deleted_count > 0

    async def list_captions(self) -> List[dict]:
        return await self.captions.find({}).to_list(length=500)

    # ---------------------------------------------------------------- sources
    async def list_sources(self, enabled_only: bool = False) -> List[dict]:
        q = {"enabled": True, "authorized": True} if enabled_only else {}
        return await self.sources.find(q).to_list(length=100)

    async def upsert_source(self, name: str, data: Dict[str, Any]) -> None:
        await self.sources.update_one({"_id": name}, {"$set": {**data, "updated_at": utcnow()},
                                                      "$setOnInsert": {"created_at": utcnow()}}, upsert=True)

    async def set_source_enabled(self, name: str, enabled: bool) -> bool:
        res = await self.sources.update_one({"_id": name}, {"$set": {"enabled": enabled}})
        return res.matched_count > 0

    async def remove_source(self, name: str) -> bool:
        res = await self.sources.delete_one({"_id": name})
        return res.deleted_count > 0

    async def source_status(self, name: str, ok: bool, error: Optional[str] = None) -> None:
        await self.sources.update_one({"_id": name}, {"$set": {"last_check": utcnow(), "last_ok": ok,
                                                               "last_error": (error or "")[:500]}})

    # ------------------------------------------------------------------ inbox
    async def add_inbox_file(self, key: str, data: Dict[str, Any]) -> None:
        await self.inbox.update_one({"_id": key}, {"$set": {**data, "at": utcnow()}}, upsert=True)

    async def get_inbox_file(self, key: str) -> Optional[dict]:
        return await self.inbox.find_one({"_id": key})

    # ------------------------------------------------------------------ users
    async def touch_user(self, user_id: int) -> None:
        now = utcnow()
        await self.users.update_one({"_id": int(user_id)}, {"$set": {"last_seen": now, "blocked": False},
                                                             "$setOnInsert": {"first_seen": now}}, upsert=True)

    async def iter_user_ids(self) -> Iterable[int]:
        async for doc in self.users.find({"blocked": {"$ne": True}}, {"_id": 1}):
            yield doc["_id"]

    async def mark_blocked(self, user_id: int) -> None:
        await self.users.update_one({"_id": int(user_id)}, {"$set": {"blocked": True}})

    # ------------------------------------------------------------ fsub joins
    async def record_join_request(self, user_id: int, chat_id: int) -> None:
        await self.fsub_requests.update_one({"user_id": int(user_id), "chat_id": int(chat_id)},
                                            {"$set": {"at": utcnow()}}, upsert=True)

    async def get_join_request(self, user_id: int, chat_id: int) -> Optional[dict]:
        return await self.fsub_requests.find_one({"user_id": int(user_id), "chat_id": int(chat_id)})

    async def clear_join_request(self, user_id: int, chat_id: int) -> None:
        await self.fsub_requests.delete_one({"user_id": int(user_id), "chat_id": int(chat_id)})


def _aware(dt):
    from .timeutil import ensure_utc
    return ensure_utc(dt)
