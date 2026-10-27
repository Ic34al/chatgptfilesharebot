"""Database reconnects, loop recovery, logging redaction and config parsing."""
import asyncio
import importlib
import logging

import pytest
from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

from anime_soul.config import load_config
from anime_soul.logging_setup import RedactingFilter, redact
from anime_soul.monitor import ReleaseMonitor
from anime_soul.pipeline import Pipeline
from anime_soul.tg import Notifier


@pytest.mark.asyncio
async def test_monitor_loop_survives_db_outage(cfg, db, client, monkeypatch):
    mon = ReleaseMonitor(cfg, db, Notifier(client, (), 0), anilist=object(), jikan=None)
    calls = {"n": 0}
    stop = asyncio.Event()

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServerSelectionTimeoutError("mongo down")
        stop.set()
        return {}
    monkeypatch.setattr(mon, "poll_once", flaky)
    async def fast_wait(stop_event, seconds):
        await asyncio.sleep(0)
    monkeypatch.setattr(mon, "_wait", fast_wait)
    await asyncio.wait_for(mon.run(stop), timeout=5)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_pipeline_loop_survives_db_errors_and_reconnects(cfg, db, client, ctx, monkeypatch):
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    stop = asyncio.Event()
    calls = {"n": 0}
    real = db.claim_next

    async def flaky(lease):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AutoReconnect("connection reset")
        stop.set()
        return await real(lease)
    monkeypatch.setattr(db, "claim_next", flaky)
    async def fast_wait(stop_event, seconds):
        await asyncio.sleep(0)
    monkeypatch.setattr(p, "_wait", fast_wait)
    await asyncio.wait_for(p.run(stop), timeout=5)
    assert calls["n"] >= 2


def test_secrets_are_redacted():
    s = redact("token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ mongodb+srv://bob:hunter2@cluster0/x")
    assert "AAHdq" not in s and "hunter2" not in s and "<bot-token>" in s
    rec = logging.LogRecord("x", logging.INFO, "", 0, "api_hash=%s", ("abcdef0123",), None)
    RedactingFilter().filter(rec)
    assert "abcdef0123" not in rec.getMessage()


def test_config_parsing(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "11, 22")
    monkeypatch.setenv("OWNER_ID", "33")
    monkeypatch.setenv("FSUB_CHANNELS", "-1001234567890,@animesoul,https://t.me/othertv")
    monkeypatch.setenv("STORAGE_CHANNEL_ID", "-1009876543210")
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "5000")
    c = load_config()
    assert c.all_admin_ids == (11, 22, 33)
    assert c.fsub_channels == (-1001234567890, "@animesoul", "@othertv")
    assert c.storage_channel_id == -1009876543210
    assert c.max_file_size_mb == 2000                     # capped at Telegram's bot limit
    assert "POST_CHANNEL_ID" in c.missing_for_role()
    monkeypatch.setenv("ROLE", "bogus")
    with pytest.raises(ValueError):
        load_config()


def test_new_channel_ids_supported_by_patch():
    importlib.import_module("anime_soul.runner")  # applies the patch
    from pyrogram import utils
    assert utils.get_peer_type(-1003123456789) == "channel"
