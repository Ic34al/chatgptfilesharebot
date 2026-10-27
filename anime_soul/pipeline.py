"""Idempotent per-episode pipeline: find source → download → validate → upload → publish."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import re
from datetime import timedelta
from typing import Optional

from pyrogram.errors import (ChannelInvalid, ChannelPrivate, ChatAdminRequired, ChatWriteForbidden,
                             PeerIdInvalid)

from . import models as M
from .captions import DEFAULT_TEMPLATE, TemplateError, caption_values, render
from .config import Config
from .db import AnimeDB
from .netsafe import DownloadError, UnsafeURL, download, make_session
from .publisher import AmbiguousPublish, publish
from .sources import SourceItem, SourceRegistry
from .tg import Notifier, esc
from .timeutil import utcnow
from .uploader import UploadError, upload_episode
from .validate import InvalidFile, validate_video

log = logging.getLogger(__name__)
PERMISSION_ERRORS = (ChatAdminRequired, ChatWriteForbidden, ChannelPrivate, ChannelInvalid, PeerIdInvalid)


def backoff_seconds(attempt: int, base: int = 60, cap: int = 3600) -> int:
    return int(min(cap, base * (2 ** max(0, attempt))) * random.uniform(0.85, 1.15))


class Pipeline:
    def __init__(self, cfg: Config, db: AnimeDB, client, notifier: Notifier, sources: SourceRegistry):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.notifier = notifier
        self.sources = sources
        self._http = None
        self._bot_username: Optional[str] = None
        self._tasks: set[asyncio.Task] = set()

    async def bot_username(self) -> str:
        if not self._bot_username:
            me = getattr(self.client, "me", None) or await self.client.get_me()
            self._bot_username = me.username
        return self._bot_username

    async def dry_run(self) -> bool:
        value = await self.db.get_setting("dry_run", None)
        return self.cfg.dry_run if value is None else bool(value)

    # ------------------------------------------------------------- one episode
    async def process(self, doc: dict) -> str:
        """Advance one claimed episode as far as possible. Returns the final state."""
        key = doc["_id"]
        anime = await self.db.get_anime(doc["anime_id"])
        if not anime:
            await self.db.release(key, state=M.FAILED, last_error="Anime no longer tracked")
            return M.FAILED
        keep = asyncio.create_task(self._keepalive(key))
        try:
            return await self._advance(doc, anime)
        finally:
            keep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keep

    async def _keepalive(self, key):
        while True:
            await asyncio.sleep(max(30, self.cfg.job_lease_seconds // 3))
            await self.db.renew_lease(key, self.cfg.job_lease_seconds)

    async def _advance(self, doc: dict, anime: dict) -> str:
        key = doc["_id"]
        state = doc["state"]
        local_path = doc.get("local_path")
        try:
            # 1) find an authorized file
            if state == M.AIRING:
                item = await self.sources.find(anime, doc["episode"])
                if not item:
                    checks = doc.get("source_checks", 0) + 1
                    wait = min(self.cfg.source_check_interval_seconds * (1 + checks // 6), 1800)
                    await self.db.release(key, source_checks=checks,
                                          next_attempt_at=utcnow() + timedelta(seconds=wait))
                    return M.AIRING
                if await self.dry_run():
                    await self._dry_run_report(doc, anime, item)
                    await self.db.set_state(key, M.DRY_RUN_OK, candidate=item.to_doc())
                    await self.db.release(key)
                    return M.DRY_RUN_OK
                await self.db.set_state(key, M.AVAILABLE, candidate=item.to_doc())
                doc["candidate"] = item.to_doc()
                state = M.AVAILABLE

            item = SourceItem.from_doc(doc.get("candidate") or {})
            container = doc.get("container")

            # 2) get the file locally (skipped for files already on Telegram)
            if state in (M.AVAILABLE, M.DOWNLOADING):
                if await self.db.is_paused():
                    await self.db.release(key)
                    return state
                if item.kind == "telegram":
                    container = None
                else:
                    await self.db.set_state(key, M.DOWNLOADING)
                    local_path = await self._obtain(key, item)
                    report = await validate_video(local_path, min_bytes=self.cfg.min_file_size_bytes,
                                                  max_bytes=self.cfg.max_file_size_bytes,
                                                  expected_size=item.size, expected_sha256=item.sha256,
                                                  actual_sha256=doc.get("download_sha256"))
                    container = report.container
                await self.db.set_state(key, M.UPLOADING, local_path=local_path, container=container)
                state = M.UPLOADING

            # 3) upload to storage channel (once)
            if state == M.UPLOADING:
                fresh = await self.db.get_episode(key)
                if (not fresh.get("storage") and item.kind != "telegram"
                        and not (local_path and os.path.isfile(local_path))):
                    # e.g. dyno restarted and /tmp was wiped: fetch the file again.
                    await self.db.set_state(key, M.AVAILABLE, local_path=None)
                    await self.db.release(key, next_attempt_at=utcnow())
                    return M.AVAILABLE
                if not fresh.get("storage"):
                    storage = await upload_episode(
                        self.client, self.cfg.storage_channel_id, key, anime, doc["episode"], item, local_path,
                        timeout_s=self.cfg.upload_timeout_seconds, container=container,
                        keepalive=lambda: self.db.renew_lease(key, self.cfg.job_lease_seconds))
                    token = fresh.get("token") or M.new_episode_token()
                    await self.db.set_state(key, M.UPLOADED, storage=storage, token=token, attempts=0,
                                            last_error=None)
                else:
                    await self.db.set_state(key, M.UPLOADED)
                self._cleanup(item, local_path)
                await self.db.episodes.update_one({"_id": key}, {"$set": {"local_path": None}})
                state = M.UPLOADED

            # 4) publish (exactly once)
            if state == M.UPLOADED:
                doc = await self.db.get_episode(key)
                if await self.db.is_paused():
                    await self.db.release(key)
                    return M.UPLOADED
                msg_id = await publish(self.client, self.db, self.cfg, anime, doc, await self.bot_username())
                await self.db.set_state(key, M.PUBLISHED, post_message_id=msg_id, attempts=0, last_error=None)
                await self.db.release(key)
                log.info("Published %s", key)
                return M.PUBLISHED
            await self.db.release(key)
            return state
        except Exception as e:  # classify and schedule retry
            return await self._handle_failure(doc, anime, e)

    async def _obtain(self, key: str, item: SourceItem) -> str:
        if item.kind == "local":
            if not os.path.isfile(item.ref):
                raise DownloadError("Local source file disappeared")
            return item.ref
        if item.kind != "url":
            raise DownloadError(f"Unsupported item kind {item.kind}", transient=False)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", item.file_name)[-120:] or "episode.bin"
        dest = os.path.join(self.cfg.download_dir, key.replace(":", "_") + "_" + safe_name)
        if self._http is None or self._http.closed:
            self._http = make_session()
        adapter = self.sources.adapter_by_name(item.source)
        headers = adapter.headers() if adapter is not None and hasattr(adapter, "headers") else None
        res = await download(self._http, item.ref, dest, self.cfg.allowed_source_hosts,
                             allow_http=self.cfg.allow_http_sources, max_bytes=self.cfg.max_file_size_bytes,
                             timeout_s=self.cfg.download_timeout_seconds, headers=headers,
                             on_progress=lambda _n: self.db.renew_lease(key, self.cfg.job_lease_seconds))
        await self.db.episodes.update_one({"_id": key}, {"$set": {"download_sha256": res.sha256}})
        return res.path

    def _cleanup(self, item: SourceItem, path: Optional[str]) -> None:
        if not path or not item.owned:
            return
        real = os.path.realpath(path)
        if real.startswith(os.path.realpath(self.cfg.download_dir) + os.sep):
            with contextlib.suppress(FileNotFoundError):
                os.remove(real)

    async def _dry_run_report(self, doc, anime, item: SourceItem) -> None:
        try:
            template = await self.db.get_caption(anime["_id"]) or DEFAULT_TEMPLATE
            preview = render(template, caption_values(anime, {**doc, "candidate": item.to_doc()}, self.cfg,
                                                      item.size))
        except TemplateError as e:
            preview = f"(caption error: {esc(e)})"
        await self.notifier.alert(
            f"🧪 <b>DRY RUN</b> — would upload <code>{esc(item.file_name)}</code> from source "
            f"<b>{esc(item.source)}</b> for <code>{esc(doc['_id'])}</code>.\n\nCaption preview:\n\n{preview}")

    async def _handle_failure(self, doc, anime, exc: Exception) -> str:
        key = doc["_id"]
        fresh = await self.db.get_episode(key) or doc
        state = fresh.get("state", doc["state"])
        attempts = int(fresh.get("attempts", 0)) + 1
        stage = state
        msg = f"{type(exc).__name__}: {exc}"
        await self.db.record_error(key, stage, msg, attempts)
        log.warning("Episode %s failed at %s (attempt %s): %s", key, stage, attempts, msg)
        local_path = fresh.get("local_path")
        title = esc(anime.get("title_romaji", key))

        if isinstance(exc, AmbiguousPublish):
            await self.db.set_state(key, M.FAILED, last_error=str(exc))
            await self.db.release(key)
            await self.notifier.alert(f"⚠️ Publish of <b>{title}</b> could not be confirmed.\n{esc(exc)}")
            return M.FAILED
        if isinstance(exc, (InvalidFile, UnsafeURL)) or (isinstance(exc, DownloadError) and not exc.transient):
            # Reject this candidate; go back to waiting for a good authorized file.
            item = SourceItem.from_doc(fresh.get("candidate") or {}) if fresh.get("candidate") else None
            if item:
                self._cleanup(item, local_path)
            nxt = M.AIRING if attempts < self.cfg.max_retries else M.FAILED
            await self.db.set_state(key, nxt, candidate=None, local_path=None, attempts=attempts, last_error=msg,
                                    next_attempt_at=utcnow() + timedelta(seconds=backoff_seconds(attempts)))
            await self.db.release(key)
            await self.notifier.alert(f"🚫 Rejected file for <b>{title}</b> ep {doc['episode']}: {esc(exc)}",
                                      key=f"reject:{key}")
            return nxt
        if isinstance(exc, PERMISSION_ERRORS):
            await self.notifier.alert(f"🔒 Telegram permission problem while processing <b>{title}</b>: "
                                      f"{esc(exc)}\nMake sure the bot is an admin in the storage and post "
                                      f"channels.", key="perm")
        if attempts >= self.cfg.max_retries or (isinstance(exc, UploadError) and not exc.transient):
            await self.db.set_state(key, M.FAILED, attempts=attempts, last_error=msg)
            await self.db.release(key)
            await self.notifier.alert(f"❌ <b>{title}</b> ep {doc['episode']} failed after {attempts} attempts: "
                                      f"{esc(msg)}\nUse /retry {esc(key)}")
            return M.FAILED
        # Transient: keep state (download restarts from scratch; upload is skipped if already stored).
        back_state = state if state in M.WORKABLE_STATES else M.AIRING
        if back_state == M.DOWNLOADING:
            back_state = M.AVAILABLE
        await self.db.release(key, state=back_state, attempts=attempts, last_error=msg,
                              next_attempt_at=utcnow() + timedelta(seconds=backoff_seconds(attempts)))
        return back_state

    # ----------------------------------------------------------------- worker
    async def run(self, stop: asyncio.Event, idle_poll_s: int = 15) -> None:
        log.info("Pipeline worker started (concurrency %s)", self.cfg.max_concurrent_jobs)
        os.makedirs(self.cfg.download_dir, exist_ok=True)
        self._clean_stale_parts()
        sem = asyncio.Semaphore(self.cfg.max_concurrent_jobs)
        backoff = 5
        while not stop.is_set():
            try:
                await self.db.heartbeat("pipeline")
                claimed = 0
                if not await self.db.is_paused():
                    while sem._value > 0 and not stop.is_set():  # noqa: SLF001 - simple capacity check
                        doc = await self.db.claim_next(self.cfg.job_lease_seconds)
                        if not doc:
                            break
                        claimed += 1
                        await sem.acquire()
                        t = asyncio.create_task(self._run_one(doc, sem))
                        self._tasks.add(t)
                        t.add_done_callback(self._tasks.discard)
                backoff = 5
                wait = 1 if claimed else idle_poll_s
            except Exception:
                log.exception("Pipeline loop error")
                wait = backoff
                backoff = min(backoff * 2, 300)
            await self._wait(stop, wait)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    async def _run_one(self, doc, sem):
        try:
            await self.process(doc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unhandled error processing %s", doc.get("_id"))
        finally:
            sem.release()

    async def shutdown(self, grace_s: int = 20) -> None:
        if self._tasks:
            log.info("Waiting up to %ss for %s running job(s)", grace_s, len(self._tasks))
            done, pending = await asyncio.wait(self._tasks, timeout=grace_s)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        released = await self.db.release_all_mine()
        if released:
            log.info("Released %s lease(s) for pickup after restart", released)
        if self._http and not self._http.closed:
            await self._http.close()

    def _clean_stale_parts(self):
        with contextlib.suppress(FileNotFoundError):
            for f in os.listdir(self.cfg.download_dir):
                if f.endswith(".part"):
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(self.cfg.download_dir, f))
