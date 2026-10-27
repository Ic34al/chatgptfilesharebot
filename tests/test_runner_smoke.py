"""Start the real entry point with mocked Telegram/MongoDB, then stop it with SIGTERM like Heroku does."""
import asyncio
import os
import signal

import pytest
from mongomock_motor import AsyncMongoMockClient

from tests.conftest import FakeClient


class RunnableClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.started = self.stopped = False
        self.handlers = []

    def add_handler(self, h, group=0):
        self.handlers.append(h)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


@pytest.mark.parametrize("role", ["all", "bot", "pipeline"])
async def test_run_starts_and_shuts_down_gracefully(monkeypatch, role, tmp_path):
    import anime_soul.runner as runner
    for k, v in {"API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "1:x", "MONGODB_URI": "mongodb://x",
                 "STORAGE_CHANNEL_ID": "-1003000000001", "POST_CHANNEL_ID": "-1003000000002",
                 "OWNER_ID": "1001", "ROLE": role, "DOWNLOAD_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    mongo = AsyncMongoMockClient(tz_aware=True)
    monkeypatch.setattr(runner, "AsyncIOMotorClient", lambda *a, **k: mongo)
    clients = []

    def build(cfg, r):
        c = RunnableClient()
        clients.append(c)
        return c
    monkeypatch.setattr(runner, "build_client", build)

    class NoNet:
        async def airing_window(self, *a):
            return []

        async def anime_info(self, *a):
            return {}

        async def close(self):
            pass
    monkeypatch.setattr(runner, "AniListProvider", NoNet)

    async def stopper():
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)
    t = asyncio.create_task(stopper())
    await asyncio.wait_for(runner.run(), timeout=10)
    await t
    c = clients[0]
    assert c.started and c.stopped
    assert bool(c.handlers) == (role in ("all", "bot"))
    hb = await mongo["file_share_bot"]["settings"].find_one({"_id": "heartbeat:pipeline"})
    assert bool(hb) == (role in ("all", "pipeline"))
