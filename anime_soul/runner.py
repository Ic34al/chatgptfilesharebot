"""Process entry point with roles and graceful shutdown.

ROLE=all       one dyno: bot handlers + monitor + pipeline (default, cheapest)
ROLE=bot       handlers only (pair with an `uploader` dyno running ROLE=pipeline)
ROLE=pipeline  monitor + uploader only; does not receive updates
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

import pyrogram.utils as pyro_utils
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client

from database import FileDB

from . import processing
from .config import Config, ConfigError, load_config
from .context import Ctx
from .db import AnimeDB
from .fsub import FsubChecker
from .logging_setup import setup_logging
from .monitor import ReleaseMonitor
from .pipeline import Pipeline
from .schedule import AniListProvider
from .sources import SourceRegistry
from .tg import Notifier

log = logging.getLogger("anime_soul")

# Pyrogram 2.0.106 rejects newer (-100…) channel ids with "Peer id invalid". Widen the range.
pyro_utils.MIN_CHANNEL_ID = -1009999999999999


def build_client(cfg: Config, role: str) -> Client:
    name = "file_share_bot" if role != "pipeline" else "anime_soul_pipeline"
    return Client(name, api_id=cfg.api_id, api_hash=cfg.api_hash, bot_token=cfg.bot_token,
                  workers=32, sleep_threshold=60, in_memory=(role == "pipeline"),
                  no_updates=(role == "pipeline"))


async def preflight(client: Client, cfg: Config, notifier: Notifier, sources=None) -> None:
    """Warn early about channels, FFmpeg and sources. Never prints secrets."""
    tools = processing.tools_available()
    if cfg.process_mode != "off" and not all(tools.values()):
        missing = ", ".join(k for k, v in tools.items() if not v)
        level = "Uploads are blocked until it is installed" if cfg.ffmpeg_required else \
            "Files will be uploaded without stream validation (FFMPEG_REQUIRED=false)"
        await notifier.alert(f"🎞 FFmpeg tools missing: {missing}. {level}. Heroku: add buildpack "
                             "https://github.com/heroku/heroku-buildpack-activestorage-preview", key="ffmpeg-missing")
    if sources is not None:
        with contextlib.suppress(Exception):
            if await sources.configured_count() == 0:
                await notifier.alert("⚠️ SOURCE_NOT_CONFIGURED: no authorized video source is enabled. Episodes "
                                     "will be detected and queued, but nothing can be downloaded or posted until "
                                     "you add one with /sources (or /attach files you may share).",
                                     key="source-not-configured")
    me = await client.get_me()
    for label, chat in (("storage", cfg.storage_channel_id), ("post", cfg.post_channel_id)):
        if chat is None:
            continue
        try:
            member = await client.get_chat_member(chat, me.id)
            status = getattr(member.status, "name", str(member.status))
            if status not in ("ADMINISTRATOR", "OWNER"):
                await notifier.alert(f"⚠️ The bot is not an admin in the {label} channel ({chat}).")
        except Exception as e:
            await notifier.alert(f"⚠️ Cannot access the {label} channel ({chat}): {e}")
    for chat in cfg.fsub_channels:
        try:
            await client.get_chat(chat)
        except Exception as e:
            await notifier.alert(f"⚠️ Cannot access FSUB channel {chat}: {e}. Add the bot as admin.")


async def run(role_override: str | None = None) -> None:
    try:
        cfg = load_config()
    except ConfigError as e:
        logging.basicConfig(level="INFO")
        log.error("Invalid config: %s", e)
        sys.exit(2)
    if role_override:
        cfg = Config(**{**cfg.__dict__, "role": role_override})
    setup_logging(cfg.log_level, cfg.log_json)
    missing = cfg.missing_for_role()
    if missing:
        log.error("Missing required config vars: %s", ", ".join(missing))
        sys.exit(2)

    mongo = AsyncIOMotorClient(cfg.mongodb_uri, maxPoolSize=50, minPoolSize=1, serverSelectionTimeoutMS=10000,
                               retryWrites=True, tz_aware=True)
    filedb = FileDB(cfg.mongodb_uri, cfg.database_name)
    await filedb.connect(mongo)
    db = AnimeDB(mongo[cfg.database_name])
    try:
        log.info("MongoDB reachable (%.0f ms)", await db.ping())
    except Exception as e:  # never log the URI (it contains the password)
        log.error("Cannot reach MongoDB (%s). Check MONGODB_URI and MongoDB Atlas → Network Access "
                  "(Heroku needs 0.0.0.0/0).", type(e).__name__)
        sys.exit(3)
    await db.ensure_indexes()

    client = build_client(cfg, cfg.role)
    notifier = Notifier(client, cfg.all_admin_ids)
    sources = SourceRegistry(db, cfg)
    anilist = AniListProvider()
    fsub = FsubChecker(client, db, cfg, notifier)
    ctx = Ctx(cfg=cfg, db=db, filedb=filedb, notifier=notifier, fsub=fsub, sources=sources, anilist=anilist)

    stop = asyncio.Event()
    ctx.stop_event = stop
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    tasks = []
    pipeline = None
    if cfg.role in ("all", "pipeline"):
        ctx.monitor = ReleaseMonitor(cfg, db, notifier, anilist=anilist, sources=sources)
        pipeline = Pipeline(cfg, db, client, notifier, sources)
        ctx.pipeline = pipeline
    if cfg.role in ("all", "bot"):
        from .handlers import admin, extras, source_ingest, user
        admin.register(client, ctx)   # admin first so /attach captions aren't treated as share uploads
        extras.register(client, ctx)  # bans, /manage, /autodel, /search, /ongoing … (AutoAnime.zip features)
        user.register(client, ctx)
        source_ingest.register(client, ctx)   # provider channels (separate handler group)
        await extras.load_admins(ctx)

    await client.start()
    me = await client.get_me()
    log.info("Started as @%s (role=%s, dry_run=%s)", me.username, cfg.role, cfg.dry_run)
    if cfg.role in ("all", "pipeline"):
        await preflight(client, cfg, notifier, sources)
        tasks.append(asyncio.create_task(ctx.monitor.run(stop), name="monitor"))
        tasks.append(asyncio.create_task(pipeline.run(stop), name="pipeline"))
    if cfg.role in ("all", "bot"):
        from .handlers import extras
        tasks.append(asyncio.create_task(extras.autodelete_loop(ctx, client, stop), name="autodelete"))
        await extras.set_bot_commands(ctx, client)
        await extras.announce_restart(ctx, client)

    await stop.wait()
    log.info("Shutdown requested; stopping gracefully")
    if pipeline:
        await pipeline.shutdown(grace_s=20)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for t in list(ctx.background):
        t.cancel()
    with contextlib.suppress(Exception):
        await client.stop()
    await anilist.close()
    if ctx.monitor and ctx.monitor.jikan:
        await ctx.monitor.jikan.close()
    await sources.close()
    mongo.close()
    log.info("Stopped cleanly")
    if ctx.restart_requested:   # /restart: re-run the same command in this dyno (leases already released)
        log.info("Restarting (requested with /restart)")
        os.execv(sys.executable, [sys.executable] + sys.argv)
