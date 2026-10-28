"""Features adapted from AutoAnime.zip: bans, added admins, auto-delete, /ping, /restart, mapped channels,
/manage, /search, /ongoing, bot command menu, START_PICS and the authorized HLS downloader."""
import os
import shutil
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pyrogram import enums
from pyrogram.errors import ChatWriteForbidden

from anime_soul import hls
from anime_soul import models as M
from anime_soul.handlers import admin, extras, user
from anime_soul.netsafe import DownloadError, DownloadResult, UnsafeURL, validate_url
from anime_soul.timeutil import utcnow
from tests.conftest import ADMIN, FSUB, POST, STORAGE, USER, FakeMessage, with_cfg

TOKEN = "epTESTTOKEN12345678"
OTHER = 3003
MAPPED = -1003000000077
FFMPEG = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


async def published(db, ep=5, token=TOKEN):
    await db.upsert_anime(21, {"title_romaji": "Sousou no Frieren", "title_english": "Frieren"})
    await db.upsert_schedule(21, ep, utcnow(), "anilist")
    await db.set_state(f"anilist:21:{ep}", M.PUBLISHED, token=token,
                       storage={"file_id": f"STORED{ep}", "chat_id": STORAGE, "message_id": 70 + ep, "file_size": 1})


def query(data, uid=USER):
    answers, edits = [], []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    async def edit_text(text, **kw):
        edits.append((text, kw))

    async def noop(*a, **k):
        return None
    q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), answer=answer,
                        message=SimpleNamespace(edit_text=edit_text, edit_reply_markup=noop, delete=noop))
    return q, answers, edits


# ------------------------------------------------------------------ bans
@pytest.mark.asyncio
async def test_ban_blocks_start_try_again_and_file_sharing(ctx, db, client, cfg):
    await published(db)
    client.members[(FSUB, USER)] = enums.ChatMemberStatus.MEMBER
    m = FakeMessage(f"/ban {USER} spam", user_id=ADMIN)
    await admin.guard(extras.cmd_ban)(ctx, client, m)
    assert "Banned" in m.replies[-1][0]

    start = FakeMessage(f"/start {TOKEN}")
    await user.start_handler(ctx, client, start)
    assert "banned" in start.replies[-1][0].lower()
    q, answers, _ = query(f"fsub:{TOKEN}")
    await user.try_again_handler(ctx, client, q)
    assert answers and "banned" in answers[-1].lower()
    ctx.cfg = with_cfg(cfg, public_file_links=True)
    up = FakeMessage("", document=SimpleNamespace(file_id="F1", file_unique_id="U1", file_size=1))
    await user.file_handler(ctx, client, up)
    assert "banned" in up.replies[-1][0].lower()
    assert client.sent("send_cached_media") == [] and client.sent("copy_message") == []

    await admin.guard(extras.cmd_unban)(ctx, client, FakeMessage(f"/unban {USER}", user_id=ADMIN))
    await user.start_handler(ctx, client, FakeMessage(f"/start {TOKEN}"))
    assert client.sent("send_cached_media")[0][2] == "STORED5"     # old link still works after unban


@pytest.mark.asyncio
async def test_admins_cannot_be_banned_and_users_cannot_ban(ctx, client, db):
    m = FakeMessage(f"/ban {ADMIN}", user_id=ADMIN)
    await admin.guard(extras.cmd_ban)(ctx, client, m)
    assert "can't be banned" in m.replies[-1][0]
    m = FakeMessage(f"/ban {OTHER}", user_id=USER)
    await admin.guard(extras.cmd_ban)(ctx, client, m)
    assert "only for ANIME SOUL administrators" in m.replies[-1][0]
    assert await db.get_ban(OTHER) is None


# ------------------------------------------------------------------ added admins
@pytest.mark.asyncio
async def test_addadmin_is_owner_only_and_persists(ctx, client, db):
    await extras.load_admins(ctx)
    m = FakeMessage(f"/addadmin {OTHER}", user_id=ADMIN)
    await admin.guard(extras.cmd_addadmin)(ctx, client, m)
    assert ctx.is_admin(OTHER) and not ctx.is_owner(OTHER)
    # an added admin can run admin commands but can't add more admins
    m = FakeMessage("/status", user_id=OTHER)
    await admin.guard(extras.cmd_admins)(ctx, client, m)
    assert "Added admins" in m.replies[-1][0]
    m = FakeMessage(f"/addadmin {USER}", user_id=OTHER)
    await admin.guard(extras.cmd_addadmin)(ctx, client, m)
    assert "Only the owner" in m.replies[-1][0] and not ctx.is_admin(USER)
    # survives a restart (reloaded from MongoDB)
    ctx.extra_admins = set()
    await extras.load_admins(ctx)
    assert ctx.is_admin(OTHER)
    m = FakeMessage(f"/rmadmin {ADMIN}", user_id=ADMIN)
    await admin.guard(extras.cmd_rmadmin)(ctx, client, m)
    assert "Config Vars" in m.replies[-1][0] and ctx.is_admin(ADMIN)
    await admin.guard(extras.cmd_rmadmin)(ctx, client, FakeMessage(f"/rmadmin {OTHER}", user_id=ADMIN))
    assert not ctx.is_admin(OTHER)


@pytest.mark.asyncio
async def test_manage_panel_and_callback_admin_only(ctx, client, db, cfg):
    m = FakeMessage("/manage", user_id=USER)
    await admin.guard(extras.cmd_manage)(ctx, client, m)
    assert "only for ANIME SOUL administrators" in m.replies[-1][0]
    q, answers, _ = query("mng:pause", uid=USER)
    await extras.manage_callback(ctx, client, q)
    assert "Only administrators" in answers[-1] and not await db.get_setting("paused", False)

    m = FakeMessage("/manage", user_id=ADMIN)
    await admin.guard(extras.cmd_manage)(ctx, client, m)
    assert "settings" in m.replies[-1][0]
    for action in ("pause", "autodel", "autodel", "mapped"):
        q, answers, edits = query(f"mng:{action}", uid=ADMIN)
        await extras.manage_callback(ctx, client, q)
        assert answers[-1] == "Saved" and edits
    assert await db.get_setting("paused") is True
    assert await db.get_setting("autodel_seconds") == 600
    assert await db.get_setting("mapped_posts_enabled") is False
    before = cfg.dry_run
    q, _, _ = query("mng:dry", uid=ADMIN)
    await extras.manage_callback(ctx, client, q)
    assert await db.get_setting("dry_run") is (not before)


# ------------------------------------------------------------------ auto-delete
@pytest.mark.asyncio
async def test_autodelete_after_delivery_and_sweep(ctx, client, db):
    await published(db)
    client.members[(FSUB, USER)] = enums.ChatMemberStatus.MEMBER
    m = FakeMessage("/autodel 30", user_id=ADMIN)
    await admin.guard(extras.cmd_autodel)(ctx, client, m)
    assert "60" in m.replies[-1][0]                                # below the minimum
    await admin.guard(extras.cmd_autodel)(ctx, client, FakeMessage("/autodel 600", user_id=ADMIN))
    await user.start_handler(ctx, client, FakeMessage(f"/start {TOKEN}"))
    assert client.sent("send_cached_media")
    assert any("will be deleted in 10 min" in c[2] for c in client.sent("send_message"))
    assert await extras.sweep_autodeletes(ctx, client) == 0         # not due yet
    # due later (also after a restart: the queue lives in MongoDB)
    assert await extras.sweep_autodeletes(ctx, client, now=utcnow() + timedelta(seconds=601)) == 1
    (_, chat, ids), = client.sent("delete_messages")
    assert chat == USER and len(ids) == 2
    assert await db.autodelete.count_documents({}) == 0
    # an already-deleted message doesn't wedge the queue
    await db.queue_autodelete(USER, [1], utcnow())
    client.fail["delete_messages"] = ChatWriteForbidden()
    assert await extras.sweep_autodeletes(ctx, client) == 1 and await db.autodelete.count_documents({}) == 0


@pytest.mark.asyncio
async def test_autodelete_exempts_admins_and_off_by_default(ctx, client, db):
    await published(db)
    client.members[(FSUB, USER)] = enums.ChatMemberStatus.MEMBER
    client.members[(FSUB, ADMIN)] = enums.ChatMemberStatus.MEMBER
    await user.start_handler(ctx, client, FakeMessage(f"/start {TOKEN}"))
    assert await db.autodelete.count_documents({}) == 0              # default off
    await db.set_setting("autodel_seconds", 300)
    await user.start_handler(ctx, client, FakeMessage(f"/start {TOKEN}", user_id=ADMIN))
    assert await db.autodelete.count_documents({}) == 0


# ------------------------------------------------------------------ ping / restart / stats / menu
@pytest.mark.asyncio
async def test_ping_restart_and_announce(ctx, client, db):
    import asyncio
    m = FakeMessage("/ping")
    await extras.cmd_ping(ctx, client, m)
    assert m.replies[-1][0].startswith("🏓 Pong!")
    ctx.stop_event = asyncio.Event()
    m = FakeMessage("/restart", user_id=ADMIN)
    await admin.guard(extras.cmd_restart)(ctx, client, m)
    assert ctx.restart_requested and ctx.stop_event.is_set()
    await extras.announce_restart(ctx, client)
    (_, chat, mid, text, _), = client.sent("edit_message_text")
    assert chat == ADMIN and "restarted" in text
    await extras.announce_restart(ctx, client)                      # only once
    assert len(client.sent("edit_message_text")) == 1


@pytest.mark.asyncio
async def test_restart_is_admin_only(ctx, client):
    m = FakeMessage("/restart", user_id=USER)
    await admin.guard(extras.cmd_restart)(ctx, client, m)
    assert not ctx.restart_requested


@pytest.mark.asyncio
async def test_stats_keeps_original_first_line_and_adds_details(ctx, client, db):
    await published(db)
    m = FakeMessage("/stats", user_id=ADMIN)
    await admin.guard(admin.cmd_stats)(ctx, client, m)
    text = m.replies[-1][0]
    assert text.startswith("📊 Stored files: `0`") and "Episodes published: `1`" in text and "Disk" in text


@pytest.mark.asyncio
async def test_bot_command_menu_and_help(ctx, client):
    await extras.set_bot_commands(ctx, client)
    calls = client.sent("set_bot_commands")
    assert len(calls) == 2
    public = [c.command for c in calls[0][1]]
    assert "search" in public and "ban" not in public
    assert "ban" in [c.command for c in calls[1][1]] and calls[1][2].chat_id == ADMIN
    client.fail["set_bot_commands"] = RuntimeError("no")
    await extras.set_bot_commands(ctx, client)                      # never raises
    assert "/autodel" in admin.ADMIN_HELP and len(admin.ADMIN_HELP) < 4096


@pytest.mark.asyncio
async def test_start_pics_optional(ctx, client, cfg):
    m = FakeMessage("/start")
    await user.start_handler(ctx, client, m)
    assert m.replies[0][0] == user.WELCOME                          # unchanged default
    ctx.cfg = with_cfg(cfg, start_pics=("https://img.example/a.jpg",))
    m = FakeMessage("/start")
    await user.start_handler(ctx, client, m)
    assert m.replies[0][0] == ("photo", "https://img.example/a.jpg") and m.replies[0][1]["caption"] == user.WELCOME


# ------------------------------------------------------------------ /search and /ongoing
@pytest.mark.asyncio
async def test_search_returns_deep_link_buttons(ctx, client, db):
    for ep in range(1, 27):
        await published(db, ep, token=f"epTOK{ep:014d}")
    await db.upsert_schedule(21, 27, utcnow(), "anilist")           # not published: no button
    m = FakeMessage("/search frieren")
    await extras.cmd_search(ctx, client, m)
    text, kw = m.replies[-1]
    rows = kw["reply_markup"].inline_keyboard
    first = rows[0][0]
    assert "26 episode" in text and first.text == "EP 1"
    assert first.url == "https://t.me/AnimeSoulFileBot?start=epTOK00000000000001"
    assert rows[-1][-1].callback_data == "srch:21:1"                  # pagination
    q, _, edits = query("srch:21:1")
    await extras.search_callback(ctx, client, q)
    assert [b.text for b in edits[-1][1]["reply_markup"].inline_keyboard[0]] == ["EP 25", "EP 26"]
    m = FakeMessage("/search .*")                                    # regex is escaped
    await extras.cmd_search(ctx, client, m)
    assert "Nothing found" in m.replies[-1][0]


@pytest.mark.asyncio
async def test_search_and_ongoing_respect_bans(ctx, client, db):
    await published(db)
    await db.ban_user(USER, "x", ADMIN)
    m = FakeMessage("/search frieren")
    await extras.cmd_search(ctx, client, m)
    assert m.replies[-1][0] == extras.BANNED_TEXT
    m = FakeMessage("/ongoing")
    await extras.cmd_ongoing(ctx, client, m)
    assert m.replies[-1][0] == extras.BANNED_TEXT
    q, answers, _ = query("srch:21:0")
    await extras.search_callback(ctx, client, q)
    assert answers[-1] == extras.BANNED_TEXT


@pytest.mark.asyncio
async def test_ongoing_shows_schedule(ctx, client, db):
    await published(db)
    m = FakeMessage("/ongoing")
    await extras.cmd_ongoing(ctx, client, m)
    assert m.replies and m.replies[-1][1].get("parse_mode") == enums.ParseMode.HTML


# ------------------------------------------------------------------ mapped channels
@pytest.mark.asyncio
async def test_setmap_requires_bot_admin(ctx, client, db):
    await published(db)
    m = FakeMessage(f"/setmap {MAPPED} 21", user_id=ADMIN)
    client.members[(MAPPED, "me")] = enums.ChatMemberStatus.MEMBER
    await admin.guard(extras.cmd_setmap)(ctx, client, m)
    assert "administrator" in m.replies[-1][0] and await db.channels_for_anime(21) == []
    client.members[(MAPPED, "me")] = enums.ChatMemberStatus.ADMINISTRATOR
    await admin.guard(extras.cmd_setmap)(ctx, client, m)
    assert await db.channels_for_anime(21) == [MAPPED]
    m = FakeMessage(f"/setmap {POST} 21", user_id=ADMIN)
    await admin.guard(extras.cmd_setmap)(ctx, client, m)
    assert "already" in m.replies[-1][0]
    await admin.guard(extras.cmd_unmap)(ctx, client, FakeMessage(f"/unmap {MAPPED} 21", user_id=ADMIN))
    assert await db.channels_for_anime(21) == []


@pytest.mark.asyncio
async def test_post_to_mapped_once_and_failure_is_isolated(ctx, client, db, cfg):
    await published(db)
    await db.map_channel(MAPPED, 21)
    anime, ep = await db.get_anime(21), await db.get_episode("anilist:21:5")
    client.fail["send_photo"] = ChatWriteForbidden()
    client.fail["send_message"] = [ChatWriteForbidden()]
    n = await extras.post_to_mapped(db, client, cfg, anime, ep, "AnimeSoulFileBot", ctx.notifier)
    assert n == 0                                                    # failure recorded, no exception
    assert await db.mapped_posts.count_documents({}) == 0             # failed claim released for a retry
    client.fail.clear()
    assert await extras.post_to_mapped(db, client, cfg, anime, ep, "AnimeSoulFileBot") == 1
    assert await extras.post_to_mapped(db, client, cfg, anime, ep, "AnimeSoulFileBot") == 0
    posted = [c for c in client.calls if c[1] == MAPPED and c[0] in ("send_photo", "send_message")]
    kb = posted[-1][-1]["reply_markup"]
    assert kb.inline_keyboard[0][0].url == f"https://t.me/AnimeSoulFileBot?start={TOKEN}"
    await db.set_setting("mapped_posts_enabled", False)
    await db.mapped_posts.delete_many({})
    assert await extras.post_to_mapped(db, client, cfg, anime, ep, "AnimeSoulFileBot") == 0


# ------------------------------------------------------------------ registration
def test_register_adds_handlers(ctx):
    class App:
        def __init__(self):
            self.h = []

        def add_handler(self, h, group=0):
            self.h.append(h)
    app = App()
    extras.register(app, ctx)
    assert len(app.h) == len(extras.ADMIN_EXTRA) + len(extras.OWNER_EXTRA) + len(extras.PUBLIC_EXTRA) + 2


# ------------------------------------------------------------------ HLS (authorized providers only)
MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
mid/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
https://cdn.partner.example/hi/index.m3u8
"""


def media_pl(n=3, key=None, endlist=True, host=""):
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-PLAYLIST-TYPE:VOD"]
    if key:
        lines.append(key)
    for i in range(n):
        lines += ["#EXTINF:2.0,", f"{host}seg{i}.ts"]
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def test_hls_parse_and_pick_variant():
    v = hls.parse_master(MASTER, "https://cdn.partner.example/show/master.m3u8")
    assert [x.height for x in v] == [360, 720, 1080]
    assert v[0].url == "https://cdn.partner.example/show/low/index.m3u8"
    assert hls.pick_variant(v, "720p").height == 720
    assert hls.pick_variant(v, "480p").height == 360
    assert hls.pick_variant(v, None).height == 1080
    assert hls.is_hls_url("https://x/a.m3u8?sig=1") and not hls.is_hls_url("https://x/a.mkv")


def test_hls_refuses_encryption_live_and_oversized():
    hls.check_protection(media_pl(key="#EXT-X-KEY:METHOD=NONE"))
    for key in ('#EXT-X-KEY:METHOD=AES-128,URI="k.bin"', '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://x"',
                '#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="k"'):
        with pytest.raises(DownloadError, match="encrypted"):
            hls.parse_media(media_pl(key=key), "https://cdn.partner.example/a/")
    with pytest.raises(DownloadError, match="Live"):
        hls.parse_media(media_pl(endlist=False), "https://cdn.partner.example/a/")
    with pytest.raises(DownloadError, match="too many"):
        hls.parse_media(media_pl(hls.MAX_SEGMENTS + 1), "https://cdn.partner.example/a/")
    with pytest.raises(DownloadError, match="Not an HLS"):
        hls.parse_media("<html>", "https://cdn.partner.example/a/")


@pytest.fixture
def hls_server(monkeypatch, tmp_path):
    """Serves playlists/segments from a dict. URLs still go through the real allowlist check."""
    files, fetched = {}, []

    async def fetch_bytes(session, url, allowlist, allow_http=False, headers=None, max_bytes=0):
        validate_url(url, allowlist, allow_http)
        fetched.append(url)
        return files[url]

    async def download(session, url, dest, allowlist, *, allow_http=False, max_bytes, timeout_s, headers=None,
                       on_progress=None):
        validate_url(url, allowlist, allow_http)
        fetched.append(url)
        data = files[url]
        if len(data) > max_bytes:
            raise DownloadError("too large", transient=False)
        with open(dest, "wb") as f:
            f.write(data)
        return DownloadResult(dest, len(data), "x")
    monkeypatch.setattr(hls, "fetch_bytes", fetch_bytes)
    monkeypatch.setattr(hls, "download", download)
    return files, fetched


def make_segments(tmp_path, n=3):
    out = tmp_path / "gen"
    out.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "10", "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                    "-hls_segment_filename", str(out / "seg%d.ts"), str(out / "index.m3u8")], check=True)
    return out


@FFMPEG
@pytest.mark.asyncio
async def test_hls_download_and_remux_real_ffmpeg(hls_server, tmp_path, cfg):
    files, fetched = hls_server
    gen = make_segments(tmp_path)
    base = "https://cdn.partner.example/show/"
    files[base + "master.m3u8"] = MASTER.encode()
    files[base + "mid/index.m3u8"] = (gen / "index.m3u8").read_bytes()
    for seg in sorted(os.listdir(gen)):
        if seg.endswith(".ts"):
            files[base + "mid/" + seg] = (gen / seg).read_bytes()
    progress = []

    async def on_progress(n):
        progress.append(n)
    dest = await hls.download_hls(None, base + "master.m3u8", str(tmp_path / "out" / "ep5.m3u8"),
                                  cfg.allowed_source_hosts, max_bytes=50 * 1024 * 1024, timeout_s=60,
                                  quality="720p", on_progress=on_progress)
    assert dest.endswith("ep5.mkv") and os.path.getsize(dest) > 1000
    assert all("/mid/" in u or u.endswith("master.m3u8") for u in fetched)
    assert progress == sorted(progress) and len(progress) >= 3
    from anime_soul.validate import ffprobe_duration
    dur = await ffprobe_duration(dest)
    assert dur is None or 5 <= dur <= 7
    assert [p for p in os.listdir(tmp_path / "out") if p.startswith("hls-")] == []   # temp dir cleaned


@FFMPEG
@pytest.mark.asyncio
async def test_hls_segment_host_not_allowlisted_and_size_cap(hls_server, tmp_path, cfg):
    files, _ = hls_server
    base = "https://cdn.partner.example/a/"
    files[base + "index.m3u8"] = media_pl(2, host="https://evil.example/").encode()
    with pytest.raises(UnsafeURL):
        await hls.download_hls(None, base + "index.m3u8", str(tmp_path / "e.mkv"), cfg.allowed_source_hosts,
                               max_bytes=10_000, timeout_s=10)
    files[base + "big.m3u8"] = media_pl(3).encode()
    for i in range(3):
        files[f"{base}seg{i}.ts"] = b"\0" * 4000
    with pytest.raises(DownloadError, match="too large|larger"):
        await hls.download_hls(None, base + "big.m3u8", str(tmp_path / "b.mkv"), cfg.allowed_source_hosts,
                               max_bytes=10_000, timeout_s=10)
    assert not os.path.exists(tmp_path / "b.mkv")


@pytest.mark.asyncio
async def test_feed_hls_item_marked_and_routed(cfg, monkeypatch, tmp_path):
    import anime_soul.sources.adapters as mod
    from anime_soul.sources.adapters import HttpFeedSource

    async def fake_fetch(*a, **kw):
        return {"items": [{"anilist_id": 21, "episode": 5, "quality": "720p", "format": "hls",
                           "url": "https://cdn.partner.example/show/master", "size": 5}]}
    monkeypatch.setattr(mod, "fetch_json", fake_fetch)
    ad = HttpFeedSource("p", {"url": "https://cdn.partner.example/feed.json"}, cfg.allowed_source_hosts)
    item = await ad.find({"_id": 21, "title_romaji": "Frieren"}, 5)
    assert item.extra == {"format": "hls"} and item.size is None and item.file_name.endswith(".mkv")

    seen = {}

    async def fake_hls(session, url, dest, allowlist, **kw):
        seen.update(url=url, quality=kw["quality"])
        with open(dest, "wb") as f:
            f.write(b"x")
        return dest
    monkeypatch.setattr(hls, "download_hls", fake_hls)
    from anime_soul.sources.base import TransferContext
    tc = TransferContext(cfg=cfg, http=lambda: None)
    out = await ad.download(item, str(tmp_path / "ep.mkv"), tc)
    assert seen == {"url": "https://cdn.partner.example/show/master", "quality": "720p"} and out.size == 1


# ------------------------------------------------------------------ full pipeline + mapped channel
from tests.test_feeds_full_pipeline import rss_episode, run_until_idle  # noqa: E402
from tests.conftest import make_media  # noqa: E402


@pytest.fixture
def fake_dl(monkeypatch):
    """Same as test_feeds_full_pipeline.fake_download: the HTTP download copies a local synthetic clip."""
    import anime_soul.netsafe as ns
    clip = {"path": None}

    async def dl(session, url, dest, allowlist, **kw):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(clip["path"], dest)
        return ns.DownloadResult(dest, os.path.getsize(dest), None)
    monkeypatch.setattr(ns, "download", dl)
    return clip


@FFMPEG
@pytest.mark.asyncio
async def test_pipeline_publishes_to_main_and_mapped_channel_once(cfg, db, client, ctx, monkeypatch, tmp_path,
                                                                  fake_dl):
    from anime_soul.pipeline import Pipeline
    clip = fake_dl
    clip["path"] = str(make_media(tmp_path / "partner.mkv"))
    await rss_episode(cfg, db, ctx, monkeypatch, os.path.getsize(clip["path"]))
    await db.map_channel(MAPPED, 21)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await run_until_idle(p, db, cfg))[-1] == M.PUBLISHED
    ep = await db.get_episode("anilist:21:5")
    main = [c for c in client.calls if c[1] == POST]
    mapped = [c for c in client.calls if c[1] == MAPPED]
    assert len(main) == 1 and len(mapped) == 1
    assert mapped[0][3]["reply_markup"].inline_keyboard[0][0].url == f"https://t.me/AnimeSoulFileBot?start={ep['token']}"
    await db.reset_for_retry("anilist:21:5")
    await run_until_idle(p, db, cfg)
    assert len([c for c in client.calls if c[1] == MAPPED]) == 1


@FFMPEG
@pytest.mark.asyncio
async def test_pipeline_mapped_failure_does_not_fail_episode(cfg, db, client, ctx, monkeypatch, tmp_path,
                                                             fake_dl):
    from anime_soul.pipeline import Pipeline
    clip = fake_dl
    clip["path"] = str(make_media(tmp_path / "partner.mkv"))
    await rss_episode(cfg, db, ctx, monkeypatch, os.path.getsize(clip["path"]))
    await db.map_channel(MAPPED, 21)

    async def boom(*a, **k):
        raise RuntimeError("mapped boom")
    monkeypatch.setattr(extras, "build_post", boom)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await run_until_idle(p, db, cfg))[-1] == M.PUBLISHED
    assert len([c for c in client.calls if c[1] == POST]) == 1
