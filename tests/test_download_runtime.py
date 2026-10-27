"""Streaming downloads (limits, atomic writes), resolver safety and handler wiring."""
import os

import pytest
from aiohttp import web

import anime_soul.netsafe as ns


@pytest.fixture
async def server(aiohttp_unused_port=None):
    payload = b"\x1a\x45\xdf\xa3" + os.urandom(300_000)

    async def ok(_):
        return web.Response(body=payload)

    async def redirect(_):
        raise web.HTTPFound("https://evil.example/x.mkv")

    async def forbidden(_):
        return web.Response(status=403)

    app = web.Application()
    app.add_routes([web.get("/ok.mkv", ok), web.get("/redir", redirect), web.get("/403", forbidden)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", payload
    await runner.cleanup()


@pytest.fixture
def permissive(monkeypatch):
    """Local test server is on 127.0.0.1, so relax URL validation only inside these tests."""
    real = ns.validate_url

    def relaxed(url, allowlist, allow_http=False):
        if "127.0.0.1" in url:
            return url
        return real(url, allowlist, allow_http)
    monkeypatch.setattr(ns, "validate_url", relaxed)


async def test_download_success_atomic(server, permissive, tmp_path):
    import aiohttp
    base, payload = server
    async with aiohttp.ClientSession() as s:
        res = await ns.download(s, base + "/ok.mkv", str(tmp_path / "a.mkv"), ("x",), allow_http=True,
                                max_bytes=10_000_000, timeout_s=10)
    assert res.size == len(payload) and (tmp_path / "a.mkv").read_bytes() == payload
    assert not (tmp_path / "a.mkv.part").exists()


async def test_download_size_limit_and_cleanup(server, permissive, tmp_path):
    import aiohttp
    base, _ = server
    async with aiohttp.ClientSession() as s:
        with pytest.raises(ns.DownloadError) as e:
            await ns.download(s, base + "/ok.mkv", str(tmp_path / "b.mkv"), ("x",), allow_http=True,
                              max_bytes=1000, timeout_s=10)
    assert e.value.transient is False
    assert not (tmp_path / "b.mkv").exists() and not (tmp_path / "b.mkv.part").exists()


async def test_download_redirect_to_unlisted_host_blocked(server, permissive, tmp_path):
    import aiohttp
    base, _ = server
    async with aiohttp.ClientSession() as s:
        with pytest.raises(ns.UnsafeURL):
            await ns.download(s, base + "/redir", str(tmp_path / "c.mkv"), ("cdn.partner.example",),
                              allow_http=True, max_bytes=10_000_000, timeout_s=10)
        with pytest.raises(ns.DownloadError) as e:
            await ns.download(s, base + "/403", str(tmp_path / "d.mkv"), ("x",), allow_http=True,
                              max_bytes=10_000_000, timeout_s=10)
    assert e.value.transient is False


async def test_resolver_refuses_private_addresses():
    r = ns.PublicOnlyResolver()
    with pytest.raises(OSError):
        await r.resolve("localhost", 443)
    await r.close()


async def test_handlers_register_on_real_pyrogram_client(ctx):
    import asyncio
    import inspect
    from pyrogram import Client
    from anime_soul.handlers import admin, user
    app = Client("t", api_id=1, api_hash="x", bot_token="1:x", in_memory=True)
    admin.register(app, ctx)
    user.register(app, ctx)
    for _ in range(5):
        await asyncio.sleep(0)
    handlers = [h for group in app.dispatcher.groups.values() for h in group]
    assert len(handlers) == len(admin.COMMANDS) + 7
    assert all(inspect.iscoroutinefunction(h.callback) for h in handlers)
