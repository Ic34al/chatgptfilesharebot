"""REAL MongoDB integration tests (not mocked).

Skipped unless REAL_MONGODB_URI points at a disposable MongoDB server, e.g.
    REAL_MONGODB_URI=mongodb://127.0.0.1:27017 python -m pytest tests/test_integration_mongodb.py
Each test uses a fresh, randomly named database and drops it afterwards. Never point this at production.
"""
import asyncio
import os
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio

from anime_soul import db as dbmod
from anime_soul import models as M
from anime_soul.timeutil import utcnow

URI = os.getenv("REAL_MONGODB_URI")
pytestmark = pytest.mark.skipif(not URI, reason="REAL_MONGODB_URI not set (real MongoDB integration test)")


@pytest_asyncio.fixture
async def rdb():
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(URI, tz_aware=True, serverSelectionTimeoutMS=5000)
    name = f"animesoul_it_{uuid.uuid4().hex[:8]}"
    d = dbmod.AnimeDB(client[name])
    await d.ensure_indexes()
    yield d
    await client.drop_database(name)
    client.close()


@pytest.mark.asyncio
async def test_unique_episode_key_under_concurrency(rdb):
    t = utcnow() - timedelta(minutes=1)
    results = await asyncio.gather(*[rdb.upsert_schedule(21, 5, t, "anilist") for _ in range(20)])
    assert results.count("new") == 1 and await rdb.episodes.count_documents({}) == 1


@pytest.mark.asyncio
async def test_job_claim_is_exclusive_between_workers(rdb, monkeypatch):
    await rdb.upsert_schedule(21, 5, utcnow() - timedelta(minutes=1), "anilist")
    await rdb.mark_aired()
    claimed = []
    for worker in ("dyno-a", "dyno-b", "dyno-c"):
        monkeypatch.setattr(dbmod, "WORKER_ID", worker)
        doc = await rdb.claim_next(600)
        if doc:
            claimed.append(worker)
    assert claimed == ["dyno-a"]
    # Lease expiry (crashed dyno) lets another worker take over.
    await rdb.episodes.update_one({}, {"$set": {"lease_until": utcnow() - timedelta(seconds=1)}})
    monkeypatch.setattr(dbmod, "WORKER_ID", "dyno-b")
    assert (await rdb.claim_next(600))["lease_owner"] == "dyno-b"


@pytest.mark.asyncio
async def test_monitor_leader_lock_real_upsert_conflict(rdb):
    oks = await asyncio.gather(*[rdb.acquire_lock("monitor", 60, owner=f"w{i}") for i in range(10)])
    assert oks.count(True) == 1
    winner = f"w{oks.index(True)}"
    assert await rdb.lock_holder("monitor") == winner
    assert await rdb.acquire_lock("monitor", 60, owner=winner)
    await rdb.settings.update_one({"_id": "lock:monitor"}, {"$set": {"until": utcnow() - timedelta(seconds=1)}})
    assert await rdb.acquire_lock("monitor", 60, owner="new")


@pytest.mark.asyncio
async def test_state_transitions_and_history(rdb):
    await rdb.upsert_schedule(21, 5, utcnow() - timedelta(minutes=1), "anilist")
    key = M.episode_key(21, 5)
    for s in (M.AIRING, M.AVAILABLE, M.DOWNLOADING, M.PROCESSING, M.UPLOADING, M.UPLOADED, M.PUBLISHED):
        await rdb.set_state(key, s)
    doc = await rdb.get_episode(key)
    assert doc["state"] == M.PUBLISHED
    assert not await rdb.reset_for_retry(key)          # published episodes are never re-run
