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
from . import processing
from .captions import human_size
from .netsafe import DownloadError, UnsafeURL, make_session
from .publisher import AmbiguousPublish, publish
from .sources import SourceItem, SourceRegistry
from .tg import Notifier, esc
from .timeutil import utcnow
from .uploader import UploadError, upload_episode
from .sources.base import TransferContext
from .validate import InvalidFile, sha256_file, validate_video

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
        try:
            # 1) find an authorized file
            if state == M.AIRING:
                item, reason = await self.sources.find_with_reason(anime, doc["episode"])
                if not item:
                    return await self._no_source(doc, anime, reason)
                if await self.dry_run():
                    await self._dry_run(doc, anime, item)
                    return M.DRY_RUN_OK
                await self.db.set_state(key, M.AVAILABLE, candidate=item.to_doc(), source_status=None,
                                        last_error=None)
                doc["candidate"] = item.to_doc()
                state = M.AVAILABLE

            # Safety: dry run may have been switched on after this job started. Nothing past this
            # point (download, upload, post) runs in dry run; the job waits until /dryrun off.
            if await self.dry_run() or await self.db.is_paused():
                await self.db.release(key, next_attempt_at=utcnow() + timedelta(
                    seconds=self.cfg.source_check_interval_seconds))
                return state

            item = SourceItem.from_doc(doc.get("candidate") or {})

            # 2) get the file locally (Telegram inbox files skip this unless PROCESS_TELEGRAM_FILES)
            if state in (M.AVAILABLE, M.DOWNLOADING):
                if item.kind == "telegram" and not self.cfg.process_telegram_files:
                    await self.db.set_state(key, M.UPLOADING, local_path=None, source_path=None, container=None)
                    state = M.UPLOADING
                else:
                    await self.db.set_state(key, M.DOWNLOADING, download_sha256=None)
                    src = await self._obtain(key, item)
                    await self.db.set_state(key, M.VALIDATING, source_path=src)
                    state = M.VALIDATING

            # 2b) validate the downloaded file before FFmpeg touches it (size, checksum, container,
            #     duration). A mismatch rejects this candidate (VALIDATION_FAILED) — it is never uploaded.
            if state == M.VALIDATING:
                fresh = await self.db.get_episode(key)
                src = fresh.get("source_path")
                if not (src and os.path.isfile(src)):
                    await self.db.set_state(key, M.AVAILABLE, source_path=None, local_path=None)
                    await self.db.release(key, next_attempt_at=utcnow())
                    return M.AVAILABLE
                adapter = self.sources.adapter_for(item)
                report = await adapter.validate(item, src, self._transfer_ctx(key),
                                                actual_sha256=fresh.get("download_sha256"))
                digest = fresh.get("download_sha256") or await asyncio.to_thread(sha256_file, src)
                await self.db.set_state(key, M.PROCESSING, download_sha256=digest,
                                        validation={"size": report.size, "container": report.container,
                                                    "duration": report.duration, "sha256": digest,
                                                    "expected_sha256": item.sha256, "expected_size": item.size})
                await self.db.event(key, "validated", size=report.size, container=report.container,
                                    checksum_verified=bool(item.sha256))
                state = M.PROCESSING

            # 3) FFprobe validation + optional remux/transcode (source kept until upload succeeds)
            if state == M.PROCESSING:
                fresh = await self.db.get_episode(key)
                src = fresh.get("source_path")
                if not (src and os.path.isfile(src)):
                    # Dyno restarted and /tmp was wiped (or the library file moved): fetch again.
                    await self.db.set_state(key, M.AVAILABLE, source_path=None, local_path=None)
                    await self.db.release(key, next_attempt_at=utcnow())
                    return M.AVAILABLE
                result = await processing.process(
                    src, self._job_dir(key), self.cfg, base_name=os.path.splitext(item.file_name)[0],
                    keepalive=lambda: self.db.renew_lease(key, self.cfg.job_lease_seconds))
                await self.db.set_state(key, M.UPLOADING, local_path=result.path, container=result.container,
                                        media=result.info.to_doc(), processing=result.to_doc())
                state = M.UPLOADING

            # 4) upload to storage channel (once)
            if state == M.UPLOADING:
                fresh = await self.db.get_episode(key)
                local_path = fresh.get("local_path")
                if (not fresh.get("storage") and not (item.kind == "telegram" and not local_path)
                        and not (local_path and os.path.isfile(local_path))):
                    back = M.PROCESSING if fresh.get("source_path") and os.path.isfile(fresh["source_path"]) \
                        else M.AVAILABLE
                    await self.db.set_state(key, back, local_path=None)
                    await self.db.release(key, next_attempt_at=utcnow())
                    return back
                if not fresh.get("storage"):
                    prior = await self.db.get_upload(key)
                    if prior and prior.get("status") == "stored" and prior.get("file_id"):
                        # Telegram already confirmed this upload (crash before the state was saved):
                        # reuse it instead of uploading a duplicate.
                        fresh["storage"] = {"chat_id": prior.get("storage_chat_id"), "message_id": prior["message_id"],
                                            "file_id": prior["file_id"], "file_unique_id": prior.get("file_unique_id"),
                                            "file_size": prior.get("file_size"), "media_type": prior.get("media_type"),
                                            "quality": prior.get("quality"), "source": item.source}
                        await self.db.set_state(key, M.UPLOADED, storage=fresh["storage"],
                                                token=fresh.get("token") or M.new_episode_token())
                if not fresh.get("storage"):
                    storage = await upload_episode(
                        self.client, self.cfg.storage_channel_id, key, anime, doc["episode"], item,
                        local_path if item.kind != "telegram" or local_path else None,
                        timeout_s=self.cfg.upload_timeout_seconds, container=fresh.get("container"),
                        media=fresh.get("media"),
                        keepalive=lambda: self.db.renew_lease(key, self.cfg.job_lease_seconds))
                    token = fresh.get("token") or M.new_episode_token()
                    # Telegram returned the stored message (verified media + size) — record it first.
                    await self.db.record_upload(key, storage, quality=storage.get("quality") or item.quality,
                                                language=item.language, sha256=fresh.get("download_sha256"),
                                                source_id=item.source_id)
                    await self.db.set_state(key, M.UPLOADED, storage=storage, token=token, attempts=0,
                                            last_error=None)
                elif fresh.get("state") != M.UPLOADED:
                    await self.db.set_state(key, M.UPLOADED)
                # Only now — after Telegram confirmed the upload — remove temporary files.
                self._cleanup_job(key, item, fresh)
                await self.db.episodes.update_one({"_id": key}, {"$set": {"local_path": None, "source_path": None}})
                state = M.UPLOADED

            # 5) publish (exactly once)
            if state == M.UPLOADED:
                doc = await self.db.get_episode(key)
                msg_id = await publish(self.client, self.db, self.cfg, anime, doc, await self.bot_username())
                # msg_id is None when the post already exists (e.g. after /reupload): keep the old post.
                await self.db.set_state(key, M.PUBLISHED, post_message_id=msg_id or doc.get("post_message_id"),
                                        attempts=0, last_error=None,
                                        failure_code=None)
                await self.db.mark_upload_status(key, "published")
                await self.db.release(key)
                log.info("Published %s", key)
                return M.PUBLISHED
            await self.db.release(key)
            return state
        except Exception as e:  # classify and schedule retry
            return await self._handle_failure(doc, anime, e)

    async def _no_source(self, doc, anime, reason) -> str:
        key = doc["_id"]
        checks = doc.get("source_checks", 0) + 1
        wait = min(self.cfg.source_check_interval_seconds * (1 + checks // 6), 1800)
        fields = {"source_checks": checks, "source_status": reason,
                  "next_attempt_at": utcnow() + timedelta(seconds=wait)}
        if reason == M.SOURCE_NOT_CONFIGURED:
            fields["last_error"] = (f"{M.SOURCE_NOT_CONFIGURED}: no authorized source is enabled. Add one with "
                                    "/sources add_rss|add_feed|add_dir, or /attach a file you are allowed to share.")
            await self.notifier.alert(
                f"⚠️ <b>{M.SOURCE_NOT_CONFIGURED}</b>: <b>{esc(anime.get('title_romaji', key))}</b> episode "
                f"{doc['episode']} has aired, but no authorized video source is configured. It stays queued; "
                "nothing was downloaded or posted. Add a licensed source with /sources.",
                key="source-not-configured")
        await self.db.release(key, **fields)
        return M.AIRING

    def _job_dir(self, key: str) -> str:
        return os.path.join(self.cfg.download_dir, key.replace(":", "_"))

    def _transfer_ctx(self, key: str) -> TransferContext:
        def http():
            if self._http is None or self._http.closed:
                self._http = make_session()
            return self._http
        return TransferContext(cfg=self.cfg, http=http, client=self.client,
                               keepalive=lambda: self.db.renew_lease(key, self.cfg.job_lease_seconds))

    async def _obtain(self, key: str, item: SourceItem) -> str:
        """Download through the item's source adapter. Returns the local path."""
        if item.kind == "local":
            if not os.path.isfile(item.ref):
                raise DownloadError("Local source file disappeared")
            return item.ref
        job_dir = self._job_dir(key)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", item.file_name)[-120:] or "episode.bin"
        dest = os.path.join(job_dir, "src_" + safe_name)
        processing.ensure_disk(job_dir, int(item.size or 0), self.cfg.disk_headroom_mb)
        os.makedirs(job_dir, exist_ok=True)
        out = await self.sources.adapter_for(item).download(item, dest, self._transfer_ctx(key))
        if out.sha256:
            await self.db.episodes.update_one({"_id": key}, {"$set": {"download_sha256": out.sha256}})
        await self.db.event(key, "downloaded", size=out.size, source_id=item.source_id)
        return out.path

    def _safe_remove(self, path: Optional[str]) -> None:
        if not path:
            return
        real = os.path.realpath(path)
        if real.startswith(os.path.realpath(self.cfg.download_dir) + os.sep):
            with contextlib.suppress(FileNotFoundError, IsADirectoryError):
                os.remove(real)

    def _cleanup_job(self, key: str, item: Optional[SourceItem], doc: Optional[dict]) -> None:
        """Delete temp files for a job. Library files (owned=False) are never deleted."""
        doc = doc or {}
        proc = doc.get("processing") or {}
        if proc.get("created"):
            self._safe_remove(proc.get("path"))
        local = doc.get("local_path")
        src = doc.get("source_path")
        if item is None or item.owned:
            self._safe_remove(src)
            if local and local != (item.ref if item else None):
                self._safe_remove(local)
        job_dir = os.path.realpath(self._job_dir(key))
        if os.path.isdir(job_dir):
            with contextlib.suppress(OSError):
                os.rmdir(job_dir)       # only if empty

    # Back-compat helper used by older code paths/tests.
    def _cleanup(self, item: SourceItem, path: Optional[str]) -> None:
        if path and item.owned:
            self._safe_remove(path)

    async def _dry_run(self, doc, anime, item: SourceItem) -> None:
        """Report what would happen. Never uploads or posts. Optionally tests download + FFmpeg."""
        key = doc["_id"]
        report = ""
        can_fetch = item.kind != "telegram" or self.cfg.process_telegram_files
        if self.cfg.dry_run_process and can_fetch:
            src = None
            result = None
            try:
                src = await self._obtain(key, item)
                await validate_video(src, min_bytes=self.cfg.min_file_size_bytes,
                                     max_bytes=self.cfg.max_file_size_bytes, expected_size=item.size,
                                     expected_sha256=item.sha256)
                result = await processing.process(src, self._job_dir(key), self.cfg,
                                                  base_name=os.path.splitext(item.file_name)[0])
                i = result.info
                report = (f"\n\n✅ Download + FFmpeg check passed: action=<b>{result.action}</b>, "
                          f"{esc(i.video_codec)} {i.width}x{i.height}, audio={esc(','.join(i.audio_codecs))}, "
                          f"{i.duration:.0f}s, {human_size(os.path.getsize(result.path))}.")
            except Exception as e:  # report, never upload
                report = f"\n\n❌ Dry-run processing failed: {esc(type(e).__name__)}: {esc(str(e)[:300])}"
            finally:
                fake = {"processing": result.to_doc() if result else {}, "source_path": src,
                        "local_path": result.path if result else None}
                self._cleanup_job(key, item, fake)
        await self._dry_run_report(doc, anime, item, report)
        await self.db.set_state(key, M.DRY_RUN_OK, candidate=item.to_doc())
        await self.db.release(key)

    async def _dry_run_report(self, doc, anime, item: SourceItem, extra: str = "") -> None:
        try:
            template = await self.db.get_caption(anime["_id"]) or DEFAULT_TEMPLATE
            preview = render(template, caption_values(anime, {**doc, "candidate": item.to_doc()}, self.cfg,
                                                      item.size))
        except TemplateError as e:
            preview = f"(caption error: {esc(e)})"
        await self.notifier.alert(
            f"🧪 <b>DRY RUN</b> — would upload <code>{esc(item.file_name)}</code> from source "
            f"<b>{esc(item.source)}</b> for <code>{esc(doc['_id'])}</code>. Nothing was uploaded or posted."
            f"{extra}\n\nCaption preview:\n\n{preview}")

    async def _handle_failure(self, doc, anime, exc: Exception) -> str:
        key = doc["_id"]
        fresh = await self.db.get_episode(key) or doc
        state = fresh.get("state", doc["state"])
        attempts = int(fresh.get("attempts", 0)) + 1
        stage = state
        msg = f"{type(exc).__name__}: {exc}"
        await self.db.record_error(key, stage, msg, attempts)
        code = M.VALIDATION_FAILED if isinstance(exc, InvalidFile) else \
            M.PUBLISH_FAILED if isinstance(exc, AmbiguousPublish) else M.failure_code_for(stage)
        await self.db.episodes.update_one({"_id": key}, {"$set": {
            "failure_code": code, "failed_stage": stage, "last_failure_at": utcnow()}})
        await self.db.event(key, "error", failure_code=code, stage=stage, attempt=attempts, error=msg[:300])
        log.warning("Episode %s failed at %s (attempt %s): %s", key, stage, attempts, msg)
        title = esc(anime.get("title_romaji", key))

        if isinstance(exc, AmbiguousPublish):
            await self.db.set_state(key, M.FAILED, last_error=str(exc))
            await self.db.release(key)
            await self.notifier.alert(f"⚠️ Publish of <b>{title}</b> could not be confirmed.\n{esc(exc)}")
            return M.FAILED
        if isinstance(exc, (InvalidFile, UnsafeURL)) or (isinstance(exc, DownloadError) and not exc.transient) \
                or (isinstance(exc, processing.ProcessingError) and not exc.transient):
            # Reject this candidate; go back to waiting for a good authorized file.
            item = SourceItem.from_doc(fresh.get("candidate") or {}) if fresh.get("candidate") else None
            self._cleanup_job(key, item, fresh)
            nxt = M.AIRING if attempts < self.cfg.max_retries else M.FAILED
            await self.db.set_state(key, nxt, candidate=None, local_path=None, source_path=None, processing=None,
                                    attempts=attempts, last_error=msg,
                                    next_attempt_at=utcnow() + timedelta(seconds=backoff_seconds(attempts)))
            await self.db.release(key)
            await self.notifier.alert(f"🚫 Rejected file for <b>{title}</b> ep {doc['episode']}: {esc(exc)}",
                                      key=f"reject:{key}")
            return nxt
        if isinstance(exc, processing.FFmpegMissing):
            await self.notifier.alert(f"🎞 {esc(exc)}", key="ffmpeg-missing")
        if isinstance(exc, PERMISSION_ERRORS):
            await self.notifier.alert(f"🔒 Telegram permission problem while processing <b>{title}</b>: "
                                      f"{esc(exc)}\nMake sure the bot is an admin in the storage and post "
                                      f"channels.", key="perm")
        if attempts >= self.cfg.max_retries or (isinstance(exc, UploadError) and not exc.transient):
            if not fresh.get("storage"):
                item = SourceItem.from_doc(fresh["candidate"]) if fresh.get("candidate") else None
                self._cleanup_job(key, item, fresh)
                await self.db.episodes.update_one({"_id": key}, {"$set": {"local_path": None, "source_path": None}})
            await self.db.set_state(key, M.FAILED, attempts=attempts, last_error=msg)
            await self.db.release(key)
            await self.notifier.alert(f"❌ <b>{code}</b> — <b>{title}</b> ep {doc['episode']} failed after {attempts} attempts: "
                                      f"{esc(msg)}\nUse /retry {esc(key)}")
            return M.FAILED
        # Transient: keep state (download restarts from scratch; upload is skipped if already stored).
        back_state = state if state in M.WORKABLE_STATES else M.AIRING
        if back_state == M.DOWNLOADING:
            back_state = M.AVAILABLE
        if back_state in (M.VALIDATING, M.PROCESSING) and not (fresh.get("source_path")
                                                                and os.path.isfile(fresh["source_path"])):
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
        """Remove half-written downloads/encodes left by a crash (finished files are kept for resume)."""
        for root, _dirs, files in os.walk(self.cfg.download_dir):
            for f in files:
                if f.endswith(".part") or ".part." in f:
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(root, f))
