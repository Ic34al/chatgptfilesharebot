"""RSS/Atom authorized feeds, feed-based release detection, monitor lock, and the full
detect → acquire → FFmpeg → upload → publish → deliver flow (Telegram, MongoDB and HTTP mocked)."""
import os
import shutil
from datetime import timedelta
from email.utils import format_datetime

import pytest
from pyrogram import enums

import anime_soul.netsafe as ns
import anime_soul.pipeline as pipeline_mod
import anime_soul.sources.adapters as adapters
from anime_soul import models as M
from anime_soul.handlers import admin, user
from anime_soul.monitor import ReleaseMonitor
from anime_soul.pipeline import Pipeline
from anime_soul.sources import SourceRegistry
from anime_soul.sources.adapters import RssFeedSource, parse_rss
from anime_soul.sources.base import SourceUnavailable
from anime_soul.timeutil import utcnow
from tests.conftest import ADMIN, FSUB, POST, STORAGE, USER, FakeMessage, make_media, with_cfg

FFMPEG = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
ANIME = {"title_romaji": "Sousou no Frieren", "title_english": "Frieren", "season_number": 2,
         "quality": "1080p", "use_cover": False}


def rss(*items, atom=False):
    if atom:
        body = "".join(
            f'<entry><title>{t}</title><updated>{d}</updated>'
            f'<link rel="enclosure" href="{u}" length="{n}" type="video/x-matroska"/></entry>'
            for t, u, n, d in items)
        return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'.encode()
    body = "".join(
        f"<item><title>{t}</title><pubDate>{d}</pubDate>"
        f'<enclosure url="{u}" length="{n}" type="video/x-matroska"/></item>' for t, u, n, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Partner</title>{body}</channel></rss>'.encode()


def now_rfc(minutes_ago=5):
    return format_datetime(utcnow() - timedelta(minutes=minutes_ago))


# ------------------------------------------------------------------ RSS parsing
def test_parse_rss_and_atom():
    body = rss(("Frieren S02E05 [1080p]", "https://cdn.partner.example/f5.mkv", 1234, now_rfc()))
    [it] = parse_rss(body)
    assert it["title"] == "Frieren" and it["episode"] == 5 and it["quality"] == "1080p"
    assert it["size"] == 1234 and it["file_name"] == "f5.mkv" and it["season"] == 2
    [a] = parse_rss(rss(("Frieren - 06 [720p]", "https://cdn.partner.example/f6.mkv", 9, "2026-10-27T10:00:00Z"),
                        atom=True))
    assert a["episode"] == 6 and a["url"].endswith("f6.mkv") and a["released_at"].startswith("2026-10-27")


def test_parse_rss_ignores_torrents_and_magnets():
    body = rss(("Frieren - 05 [1080p]", "https://cdn.partner.example/f5.torrent", 1, now_rfc()),
               ("Frieren - 05 [1080p]", "magnet:?xt=urn:btih:abc", 1, now_rfc()))
    assert parse_rss(body) == []


def test_parse_rss_explicit_tags_win():
    body = (b'<rss><channel><item><title>Whatever</title><anilist_id>21</anilist_id><episode>7</episode>'
            b'<season>2</season><quality>1080p</quality>'
            b'<enclosure url="https://cdn.partner.example/x.mkv" length="5"/></item></channel></rss>')
    [it] = parse_rss(body)
    assert it["anilist_id"] == 21 and it["episode"] == 7 and it["season"] == 2


def test_parse_rss_blocks_entity_expansion():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
            b'<rss><channel><item><title>&b;</title></item></channel></rss>')
    with pytest.raises(SourceUnavailable, match="not valid XML"):
        parse_rss(bomb)


@pytest.mark.asyncio
async def test_rss_source_find_and_allowlist(cfg, monkeypatch):
    body = rss(("Frieren - 05 [1080p]", "https://evil.example/f5.mkv", 1, now_rfc()),
               ("Frieren - 05 [1080p]", "https://cdn.partner.example/f5.mkv", 777, now_rfc()))

    async def fake(*a, **kw):
        return body
    monkeypatch.setattr(adapters, "fetch_bytes", fake)
    ad = RssFeedSource("partner", {"url": "https://cdn.partner.example/rss"}, cfg.allowed_source_hosts)
    item = await ad.find({"_id": 21, **ANIME}, 5)
    assert item.ref == "https://cdn.partner.example/f5.mkv" and item.size == 777   # unlisted host skipped
    assert await ad.find({"_id": 21, **ANIME}, 6) is None


@pytest.mark.asyncio
async def test_rss_source_unavailable(cfg, monkeypatch):
    async def boom(*a, **kw):
        raise ns.DownloadError("Feed HTTP 503")
    monkeypatch.setattr(adapters, "fetch_bytes", boom)
    ad = RssFeedSource("partner", {"url": "https://cdn.partner.example/rss"}, cfg.allowed_source_hosts)
    with pytest.raises(SourceUnavailable):
        await ad.find({"_id": 21, **ANIME}, 5)


# ------------------------------------------------------------ release detection
class NoSlots:
    async def airing_window(self, ids, start, end):
        return []

    async def discover_releasing(self, max_pages=4):
        return []

    async def anime_info(self, ids):
        return {}

    async def close(self):
        pass


async def add_rss_source(db):
    await db.upsert_source("partner", {"type": "rss_feed", "config": {"url": "https://cdn.partner.example/rss"},
                                       "authorized": True, "license_note": "Partner deal", "enabled": True})


@pytest.mark.asyncio
async def test_feed_detects_release_once(cfg, db, client, ctx, monkeypatch):
    await db.upsert_anime(21, dict(ANIME))
    await add_rss_source(db)
    body = rss(("Frieren - 05 [1080p]", "https://cdn.partner.example/f5.mkv", 1, now_rfc()),
               ("Frieren - 01 [1080p]", "https://cdn.partner.example/f1.mkv", 1, now_rfc(60 * 24 * 30)),
               ("Other Show - 05 [1080p]", "https://cdn.partner.example/o5.mkv", 1, now_rfc()))

    async def fake(*a, **kw):
        return body
    monkeypatch.setattr(adapters, "fetch_bytes", fake)
    mon = ReleaseMonitor(cfg, db, ctx.notifier, anilist=NoSlots(), jikan=None, sources=ctx.sources)
    stats = await mon.poll_once()
    assert stats["feed_new"] == 1 and stats["aired"] == 1          # old episode 1 not backfilled
    ep = await db.get_episode("anilist:21:5")
    assert ep["state"] == M.AIRING and ep["schedule_source"] == "feed:partner"
    stats = await mon.poll_once()                                    # dedupe on the unique key
    assert stats["feed_new"] == 0 and await db.episodes.count_documents({}) == 1


@pytest.mark.asyncio
async def test_monitor_lock_single_leader(db):
    assert await db.acquire_lock("monitor", 60, owner="dyno-a")
    assert await db.acquire_lock("monitor", 60, owner="dyno-a")      # re-entrant
    assert not await db.acquire_lock("monitor", 60, owner="dyno-b")
    assert await db.lock_holder("monitor") == "dyno-a"
    await db.release_lock("monitor", owner="dyno-a")
    assert await db.acquire_lock("monitor", 60, owner="dyno-b")
    await db.settings.update_one({"_id": "lock:monitor"}, {"$set": {"until": utcnow() - timedelta(seconds=1)}})
    assert await db.acquire_lock("monitor", 60, owner="dyno-c")      # expired lock is taken over


# ------------------------------------------------------- end-to-end (mocked IO)
@pytest.fixture
def fake_download(monkeypatch, tmp_path):
    """Replace the HTTP download with a local copy of a synthetic clip; counts calls."""
    calls = []
    clip = {"path": None}

    async def dl(session, url, dest, allowlist, **kw):
        calls.append(url)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(clip["path"], dest)
        import hashlib
        return ns.DownloadResult(dest, os.path.getsize(dest), hashlib.sha256(open(dest, "rb").read()).hexdigest())
    monkeypatch.setattr(ns, "download", dl)          # used by SourceAdapter.download()
    return calls, clip


async def rss_episode(cfg, db, ctx, monkeypatch, size):
    await db.upsert_anime(21, dict(ANIME))
    await add_rss_source(db)
    body = rss(("Frieren - 05 [1080p]", "https://cdn.partner.example/f5.mkv", size, now_rfc()))

    async def fake(*a, **kw):
        return body
    monkeypatch.setattr(adapters, "fetch_bytes", fake)
    mon = ReleaseMonitor(cfg, db, ctx.notifier, anilist=NoSlots(), jikan=None, sources=ctx.sources)
    await mon.poll_once()


async def run_until_idle(p, db, cfg, limit=10):
    states = []
    for _ in range(limit):
        await db.episodes.update_many({}, {"$set": {"next_attempt_at": utcnow() - timedelta(seconds=1)}})
        doc = await db.claim_next(cfg.job_lease_seconds)
        if not doc:
            break
        states.append(await p.process(doc))
        if states[-1] in (M.PUBLISHED, M.FAILED, M.DRY_RUN_OK):
            break
    return states


@FFMPEG
@pytest.mark.asyncio
async def test_full_flow_detect_to_delivery(cfg, db, client, ctx, filedb, monkeypatch, tmp_path, fake_download):
    calls, clip = fake_download
    clip["path"] = str(make_media(tmp_path / "partner.mkv"))
    await rss_episode(cfg, db, ctx, monkeypatch, os.path.getsize(clip["path"]))
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    states = await run_until_idle(p, db, cfg)
    assert states[-1] == M.PUBLISHED, states
    ep = await db.get_episode("anilist:21:5")
    # uploaded once to storage, reference stored, temp files gone
    up = [c for c in client.calls if c[0] in ("send_video", "send_document")]
    assert len(up) == 1 and up[0][1] == STORAGE and up[0][0] == "send_video"
    assert ep["storage"]["chat_id"] == STORAGE and ep["storage"]["message_id"]
    assert ep["storage"]["quality"] == "90p"                     # verified from ffprobe, not the feed label
    assert ep["processing"]["action"] == "remux" and calls == ["https://cdn.partner.example/f5.mkv"]
    assert not os.path.exists(os.path.join(cfg.download_dir, "anilist_21_5"))
    # one announcement with a GET EPISODE deep link to this bot
    posts = [c for c in client.calls if c[1] == POST]
    assert len(posts) == 1
    button = posts[0][3]["reply_markup"].inline_keyboard[0][0]
    assert button.text.upper().startswith("GET EPISODE") or "GET EPISODE" in button.text.upper()
    assert button.url == f"https://t.me/AnimeSoulFileBot?start={ep['token']}"
    # user taps it: FSUB blocks, then Try Again delivers from storage
    start = FakeMessage(f"/start {ep['token']}")
    await user.start_handler(ctx, client, start)
    assert "join" in start.replies[-1][0].lower()
    client.members[(FSUB, USER)] = enums.ChatMemberStatus.MEMBER
    start2 = FakeMessage(f"/start {ep['token']}")
    await user.start_handler(ctx, client, start2)
    delivered = [c for c in client.calls if c[0] in ("copy_message", "send_cached_media") and c[1] == USER]
    assert delivered, client.calls[-3:]
    # a second worker cycle / restart cannot upload or post again
    await db.reset_for_retry("anilist:21:5")
    assert await run_until_idle(p, db, cfg) in ([], [M.PUBLISHED])
    assert len([c for c in client.calls if c[0] in ("send_video", "send_document")]) == 1
    assert len([c for c in client.calls if c[1] == POST]) == 1


@FFMPEG
@pytest.mark.asyncio
async def test_invalid_file_from_source_is_rejected_not_uploaded(cfg, db, client, ctx, monkeypatch, tmp_path,
                                                                 fake_download):
    _, clip = fake_download
    clip["path"] = str(make_media(tmp_path / "mute.mkv", audio=False))
    await rss_episode(cfg, db, ctx, monkeypatch, os.path.getsize(clip["path"]))
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    states = await run_until_idle(p, db, cfg, limit=4)
    assert M.PUBLISHED not in states
    ep = await db.get_episode("anilist:21:5")
    assert "No audio stream" in ep["last_error"] and ep["state"] in (M.AIRING, M.FAILED)
    assert not [c for c in client.calls if c[1] in (STORAGE, POST)]
    assert not os.path.isdir(os.path.join(cfg.download_dir, "anilist_21_5")) or \
        not os.listdir(os.path.join(cfg.download_dir, "anilist_21_5"))


@FFMPEG
@pytest.mark.asyncio
async def test_restart_during_processing_refetches_and_completes(cfg, db, client, ctx, monkeypatch, tmp_path,
                                                                 fake_download):
    calls, clip = fake_download
    clip["path"] = str(make_media(tmp_path / "p.mkv"))
    await rss_episode(cfg, db, ctx, monkeypatch, os.path.getsize(clip["path"]))
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    # Simulate a crash right after download: state PROCESSING but /tmp wiped by a dyno restart.
    real = pipeline_mod.processing.process

    async def crash(*a, **kw):
        raise SystemExit("dyno restarted")
    monkeypatch.setattr(pipeline_mod.processing, "process", crash)
    doc = await db.claim_next(cfg.job_lease_seconds)
    with pytest.raises(SystemExit):
        await p.process(doc)
    ep = await db.get_episode("anilist:21:5")
    assert ep["state"] == M.PROCESSING
    shutil.rmtree(cfg.download_dir)
    await db.episodes.update_one({"_id": ep["_id"]}, {"$set": {"lease_until": utcnow() - timedelta(seconds=1)}})
    monkeypatch.setattr(pipeline_mod.processing, "process", real)
    p2 = Pipeline(cfg, db, client, ctx.notifier, SourceRegistry(db, cfg))
    states = await run_until_idle(p2, db, cfg)
    assert states[0] == M.AVAILABLE and states[-1] == M.PUBLISHED
    assert len(calls) == 2 and len([c for c in client.calls if c[1] == STORAGE]) == 1


@FFMPEG
@pytest.mark.asyncio
async def test_transient_processing_error_keeps_source_for_retry(cfg, db, client, ctx, monkeypatch, tmp_path,
                                                                 fake_download):
    calls, clip = fake_download
    clip["path"] = str(make_media(tmp_path / "p.mkv"))
    await rss_episode(cfg, db, ctx, monkeypatch, os.path.getsize(clip["path"]))
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    real = pipeline_mod.processing.process
    n = {"i": 0}

    async def flaky(*a, **kw):
        n["i"] += 1
        if n["i"] == 1:
            raise pipeline_mod.processing.ProcessingError("Not enough disk space", transient=True)
        return await real(*a, **kw)
    monkeypatch.setattr(pipeline_mod.processing, "process", flaky)
    states = await run_until_idle(p, db, cfg)
    assert states[0] == M.PROCESSING and states[-1] == M.PUBLISHED
    assert len(calls) == 1                                           # not downloaded twice


@FFMPEG
@pytest.mark.asyncio
async def test_ffmpeg_missing_blocks_upload_and_alerts(cfg, db, client, ctx, monkeypatch, tmp_path, fake_download):
    _, clip = fake_download
    clip["path"] = str(make_media(tmp_path / "p.mkv"))
    await rss_episode(cfg, db, ctx, monkeypatch, os.path.getsize(clip["path"]))
    monkeypatch.setattr(pipeline_mod.processing, "find_tool", lambda name: None)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    states = await run_until_idle(p, db, cfg, limit=3)
    assert M.PUBLISHED not in states and not [c for c in client.calls if c[1] in (STORAGE, POST)]
    assert any("FFMPEG_NOT_AVAILABLE" in c[2] for c in client.sent("send_message") if c[1] == ADMIN)


# ----------------------------------------------------------------- test mode
@FFMPEG
@pytest.mark.asyncio
async def test_dry_run_process_downloads_and_validates_but_never_uploads(cfg, db, client, ctx, monkeypatch,
                                                                         tmp_path, fake_download):
    calls, clip = fake_download
    clip["path"] = str(make_media(tmp_path / "p.mkv"))
    c = with_cfg(cfg, dry_run=True, dry_run_process=True)
    ctx.cfg = c
    await rss_episode(c, db, ctx, monkeypatch, os.path.getsize(clip["path"]))
    p = Pipeline(c, db, client, ctx.notifier, SourceRegistry(db, c))
    states = await run_until_idle(p, db, c)
    assert states == [M.DRY_RUN_OK] and calls
    assert not [x for x in client.calls if x[1] in (STORAGE, POST)]
    report = [x[2] for x in client.sent("send_message") if x[1] == ADMIN and "DRY RUN" in x[2]]
    assert report and "FFmpeg check passed" in report[0] and "action=<b>remux</b>" in report[0]
    assert not os.path.isdir(os.path.join(c.download_dir, "anilist_21_5"))


@pytest.mark.asyncio
async def test_dry_run_switched_on_mid_job_blocks_upload_and_post(cfg, db, client, ctx):
    await db.upsert_anime(21, dict(ANIME))
    await db.upsert_schedule(21, 5, utcnow() - timedelta(minutes=5), "anilist")
    await db.set_state("anilist:21:5", M.UPLOADED, token="epX",
                       storage={"file_id": "F", "chat_id": STORAGE, "message_id": 5})
    await db.set_setting("dry_run", True)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    doc = await db.claim_next(cfg.job_lease_seconds)
    assert await p.process(doc) == M.UPLOADED
    assert not [c for c in client.calls if c[1] == POST]
    assert (await db.get_episode("anilist:21:5"))["lease_owner"] is None


def test_dry_run_is_the_default_when_unset(monkeypatch):
    from anime_soul.config import load_config
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert load_config().dry_run is True


def test_config_errors_name_the_variable_not_the_value(monkeypatch):
    from anime_soul.config import ConfigError, load_config
    monkeypatch.setenv("PROCESS_MODE", "explode")
    with pytest.raises(ConfigError, match="PROCESS_MODE"):
        load_config()
    monkeypatch.setenv("PROCESS_MODE", "auto")
    monkeypatch.setenv("FFMPEG_CRF", "secretvalue")
    with pytest.raises(ConfigError) as e:
        load_config()
    assert "FFMPEG_CRF" in str(e.value) and "secretvalue" not in str(e.value)


# -------------------------------------------------------------- admin commands
@pytest.mark.asyncio
async def test_queue_and_status_show_source_not_configured(cfg, db, client, ctx):
    await db.upsert_anime(21, dict(ANIME))
    await db.upsert_schedule(21, 5, utcnow() - timedelta(minutes=5), "anilist")
    await db.mark_aired()
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    await p.process(await db.claim_next(cfg.job_lease_seconds))
    q = FakeMessage("/queue", user_id=ADMIN)
    await admin.guard(admin.cmd_queue)(ctx, client, q)
    text = q.replies[-1][0]
    assert "anilist:21:5" in text and "waiting for an authorized file" in text and M.SOURCE_NOT_CONFIGURED in text
    s = FakeMessage("/status", user_id=ADMIN)
    await admin.guard(admin.cmd_status)(ctx, client, s)
    assert "Authorized sources enabled: 0" in s.replies[-1][0] and "FFmpeg:" in s.replies[-1][0]


@pytest.mark.asyncio
async def test_help_lists_admin_commands_only_for_admins(ctx, client):
    u = FakeMessage("/help", user_id=USER)
    await user.help_handler(ctx, client, u)
    assert len(u.replies) == 1 and "/queue" not in u.replies[0][0]
    a = FakeMessage("/help", user_id=ADMIN)
    await user.help_handler(ctx, client, a)
    assert "/queue" in a.replies[-1][0] and "/testupload" in a.replies[-1][0]


@pytest.mark.asyncio
async def test_add_rss_source_command(ctx, db, client):
    m = FakeMessage("/sources add_rss partner https://cdn.partner.example/rss | Licence #7", user_id=ADMIN)
    await admin.guard(admin.cmd_sources)(ctx, client, m)
    [src] = await db.list_sources()
    assert src["type"] == "rss_feed" and src["config"]["url"] == "https://cdn.partner.example/rss"


@pytest.mark.asyncio
async def test_testupload_publish_requires_test_channel_or_live(ctx, client):
    from tests.conftest import Media
    vid = Media(file_id="S", file_unique_id="U", file_size=10)
    m = FakeMessage("/testupload publish", user_id=ADMIN, reply_to=FakeMessage("", user_id=ADMIN, video=vid))
    await admin.guard(admin.cmd_testupload)(ctx, client, m)
    assert not [c for c in client.calls if c[1] == POST] and "Not posted" in m.replies[-1][0]
    ctx.cfg = with_cfg(ctx.cfg, test_channel_id=-1003000000099)
    m = FakeMessage("/testupload publish", user_id=ADMIN, reply_to=FakeMessage("", user_id=ADMIN, video=vid))
    await admin.guard(admin.cmd_testupload)(ctx, client, m)
    assert [c for c in client.calls if c[1] == -1003000000099] and not [c for c in client.calls if c[1] == POST]


@FFMPEG
@pytest.mark.asyncio
async def test_testupload_full_runs_ffmpeg_and_uploads(ctx, client, filedb, tmp_path):
    from tests.conftest import Media
    clip = make_media(tmp_path / "mine.mkv")
    vid = Media(file_id="S", file_unique_id="U", file_size=os.path.getsize(clip), file_name="mine.mkv")
    reply_to = FakeMessage("", user_id=ADMIN, video=vid)

    async def download(file_name=None, **kw):
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        shutil.copy(clip, file_name)
        return file_name
    reply_to.download = download
    m = FakeMessage("/testupload full", user_id=ADMIN, reply_to=reply_to)
    await admin.guard(admin.cmd_testupload)(ctx, client, m)
    assert any("Full pipeline test passed: remux" in r[0] for r in m.replies), m.replies
    up = client.sent("send_video")
    assert up and up[0][1] == STORAGE
    assert not [d for d in os.listdir(ctx.cfg.download_dir) if d.startswith("testupload_")]
    assert any("Test link" in r[0] for r in m.replies)
