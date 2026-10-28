"""Ongoing-release system: parsing, adapter interface, feed backoff/reconciliation, release history,
VALIDATING state + failure codes, upload records, delivery failures, /reupload, schedule post."""
import hashlib
import os
import shutil
from datetime import timedelta
from email.utils import format_datetime

import pytest
from pyrogram.errors import ChatWriteForbidden, MessageIdInvalid

import anime_soul.netsafe as ns
import anime_soul.sources.adapters as adapters
from anime_soul import models as M
from anime_soul.handlers import admin, user
from anime_soul.matcher import parse_release_name
from anime_soul.monitor import ReleaseMonitor
from anime_soul.pipeline import Pipeline
from anime_soul.schedule_post import build_schedule_text, maybe_post_schedule
from anime_soul.sources.adapters import RssFeedSource, parse_rss, source_backoff_seconds
from anime_soul.sources.base import GenericSource, SourceItem, SourceUnavailable, TransferContext
from anime_soul.timeutil import IST, utcnow
from anime_soul.validate import InvalidFile
from tests.conftest import ADMIN, FSUB, POST, STORAGE, USER, FakeMessage, make_media, with_cfg

FFMPEG = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
ANIME = {"title_romaji": "Sousou no Frieren", "title_english": "Frieren", "season_number": 2,
         "quality": "1080p", "use_cover": False}
URL = "https://cdn.partner.example/rss"


def rss(*items):
    body = "".join(
        f"<item><title>{t}</title><guid>{g}</guid><pubDate>{d}</pubDate>"
        f'<enclosure url="{u}" length="{n}" type="video/x-matroska"/></item>' for t, g, u, n, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>P</title>{body}</channel></rss>'.encode()


def ago(**kw):
    return format_datetime(utcnow() - timedelta(**kw))


class NoSlots:
    async def airing_window(self, ids, start, end):
        return []

    async def discover_releasing(self, max_pages=4):
        return []

    async def anime_info(self, ids):
        return {}

    async def close(self):
        pass


async def add_feed(db, name="partner"):
    await db.upsert_source(name, {"type": "rss_feed", "config": {"url": URL}, "authorized": True,
                                  "license_note": "Partner distribution agreement", "enabled": True})


def serve(monkeypatch, body_or_exc, calls=None):
    async def fake(*a, **kw):
        if calls is not None:
            calls.append(1)
        if isinstance(body_or_exc, Exception):
            raise body_or_exc
        return body_or_exc
    monkeypatch.setattr(adapters, "fetch_bytes", fake)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(adapters, "FETCH_RETRY_BASE_DELAY", 0)


# ------------------------------------------------------------------- parsing
@pytest.mark.parametrize("name,title,ep,batch", [
    ("[SubsPlease] Sousou no Frieren - 05 (1080p) [ABCD1234].mkv", "Sousou no Frieren", 5, False),
    ("[SubsPlease] Frieren (01-28) (1080p) [Batch]", "Frieren", None, True),
    ("Frieren S01E01-E12 1080p", "Frieren", None, True),
    ("Kaiju No. 8 - 12 [1080p].mkv", "Kaiju No. 8", 12, False),
    ("86 - 05 [720p].mkv", "86", 5, False),
    ("Re Zero - 100 [1080p]", "Re Zero", 100, False),
])
def test_parse_titles_episodes_batches(name, title, ep, batch):
    p = parse_release_name(name)
    assert (p.title, p.episode, p.is_batch) == (title, ep, batch)


def test_parse_metadata_tags():
    p = parse_release_name("[Grp] Frieren - 05v2 [1080p][Dual Audio][ABCD1234].mkv")
    assert p.group == "Grp" and p.version == 2 and p.crc32 == "ABCD1234" and p.language == "Dual Audio"
    assert parse_release_name("Frieren S02E05 1080p ESub.mp4").language == "English Sub"


def test_rss_release_ids_and_batches():
    body = rss(("Frieren - 05 [1080p]", "g-5", "https://cdn.partner.example/5.mkv", 9, ago(minutes=5)),
               ("Frieren (01-12) [1080p] [Batch]", "g-b", "https://cdn.partner.example/b.mkv", 9, ago(minutes=5)))
    items = parse_rss(body)
    assert items[0]["guid"] == "g-5" and items[1]["is_batch"] is True
    rid = adapters.release_id_of("partner", items[0])
    assert rid == adapters.release_id_of("partner", {"guid": "g-5", "url": "other"})   # GUID wins


@pytest.mark.asyncio
async def test_find_prefers_v2_and_skips_batches(cfg, monkeypatch):
    serve(monkeypatch, rss(
        ("Frieren (01-12) [1080p] [Batch]", "b", "https://cdn.partner.example/b.mkv", 9, ago(minutes=1)),
        ("Frieren - 05 [1080p]", "v1", "https://cdn.partner.example/v1.mkv", 10, ago(minutes=9)),
        ("Frieren - 05v2 [1080p]", "v2", "https://cdn.partner.example/v2.mkv", 11, ago(minutes=2))))
    ad = RssFeedSource("partner", {"url": URL}, cfg.allowed_source_hosts)
    item = await ad.find({"_id": 21, **ANIME}, 5)
    assert item.ref.endswith("v2.mkv") and item.version == 2
    md = item.metadata()
    for k in ("anime_id", "episode", "filename", "expected_size", "checksum", "quality", "language", "source_id"):
        assert k in md
    assert md["anime_id"] == 21 and md["episode"] == 5 and md["expected_size"] == 11
    assert md["source_id"].startswith("partner:")
    assert all(not r.get("is_batch") for r in await ad.releases())


def test_source_item_roundtrip():
    it = SourceItem("s", "url", "https://x/y.mkv", "y.mkv", size=5, sha256="ab", anime_id=1, episode=2,
                    release_id="r1", version=2)
    back = SourceItem.from_doc(it.to_doc())
    assert back.metadata() == it.metadata()
    assert SourceItem.from_doc({"source": "s", "kind": "url", "ref": "u", "file_name": "f"}).version == 1  # old docs


# ------------------------------------------------- adapter download/validate
@pytest.mark.asyncio
async def test_adapter_rejects_invalid_url(cfg, tmp_path):
    ad = GenericSource("partner", {})
    item = SourceItem("partner", "url", "https://evil.example/x.mkv", "x.mkv")
    with pytest.raises(ns.UnsafeURL):
        await ad.download(item, str(tmp_path / "x.mkv"), TransferContext(cfg=cfg, http=lambda: None))
    item.ref = "file:///etc/passwd"
    with pytest.raises(ns.UnsafeURL):
        await ad.download(item, str(tmp_path / "x.mkv"), TransferContext(cfg=cfg, http=lambda: None))


@pytest.mark.asyncio
async def test_adapter_validate_checksum_mismatch(cfg, tmp_path, no_ffprobe):
    f = tmp_path / "ep.mkv"
    f.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 4000)
    good = hashlib.sha256(f.read_bytes()).hexdigest()
    ad = GenericSource("partner", {})
    ctx = TransferContext(cfg=cfg)
    ok = await ad.validate(SourceItem("partner", "local", str(f), "ep.mkv", sha256=good), str(f), ctx)
    assert ok.container == "matroska"
    with pytest.raises(InvalidFile, match="checksum"):
        await ad.validate(SourceItem("partner", "local", str(f), "ep.mkv", sha256="0" * 64), str(f), ctx)
    with pytest.raises(InvalidFile, match="Size mismatch"):
        await ad.validate(SourceItem("partner", "local", str(f), "ep.mkv", size=1), str(f), ctx)


# ----------------------------------------------------- retries and backoff
def test_backoff_doubles_and_caps():
    assert [source_backoff_seconds(n) for n in (1, 2, 3)] == [60, 120, 240]
    assert source_backoff_seconds(50) == 3600


@pytest.mark.asyncio
async def test_temporary_feed_errors_retried_then_backoff_persisted(cfg, db, ctx, monkeypatch):
    await db.upsert_anime(21, dict(ANIME))
    await add_feed(db)
    calls = []
    serve(monkeypatch, ns.DownloadError("Feed HTTP 503"), calls)
    item, reason = await ctx.sources.find_with_reason({"_id": 21, **ANIME}, 5)
    assert item is None and reason == M.SOURCE_ERROR
    assert len(calls) == 1 + adapters.FETCH_RETRIES                    # quick retries within one check
    doc = await db.sources.find_one({"_id": "partner"})
    assert doc["fail_count"] == 1 and doc["retry_at"] and doc["last_ok"] is False
    # while backing off the feed is not contacted at all
    await ctx.sources.find_with_reason({"_id": 21, **ANIME}, 5)
    assert len(calls) == 1 + adapters.FETCH_RETRIES
    # a restart (new registry) still honours the persisted backoff
    from anime_soul.sources import SourceRegistry
    reg2 = SourceRegistry(db, cfg)
    await reg2.adapters(force=True)
    assert reg2.in_backoff("partner")
    # recovery resets the counter and records the last successful check
    await db.sources.update_one({"_id": "partner"}, {"$set": {"retry_at": utcnow() - timedelta(seconds=1)}})
    serve(monkeypatch, rss(("Frieren - 05 [1080p]", "g", "https://cdn.partner.example/5.mkv", 9, ago(minutes=5))))
    await reg2.adapters(force=True)
    item, _ = await reg2.find_with_reason({"_id": 21, **ANIME}, 5)
    doc = await db.sources.find_one({"_id": "partner"})
    assert item and doc["fail_count"] == 0 and doc["last_success_at"] and not reg2.in_backoff("partner")


@pytest.mark.asyncio
async def test_permanent_feed_error_not_retried(cfg, ctx, db, monkeypatch):
    await add_feed(db)
    calls = []
    serve(monkeypatch, ns.UnsafeURL("Host not in ALLOWED_SOURCE_HOSTS"), calls)
    await ctx.sources.find_with_reason({"_id": 21, **ANIME}, 5)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_network_exception_becomes_source_unavailable(cfg, monkeypatch):
    import aiohttp
    serve(monkeypatch, aiohttp.ClientConnectionError("reset"))
    ad = RssFeedSource("partner", {"url": URL}, cfg.allowed_source_hosts)
    with pytest.raises(SourceUnavailable):
        await ad.find({"_id": 21, **ANIME}, 5)


# --------------------------------------------- release history / reconciliation
def monitor(cfg, db, ctx):
    return ReleaseMonitor(cfg, db, ctx.notifier, anilist=NoSlots(), jikan=None, sources=ctx.sources)


@pytest.mark.asyncio
async def test_release_history_dedupes(cfg, db, ctx, monkeypatch):
    await db.upsert_anime(21, dict(ANIME))
    await add_feed(db)
    serve(monkeypatch, rss(("Frieren - 05 [1080p]", "g5", "https://cdn.partner.example/5.mkv", 9, ago(minutes=5)),
                           ("Other - 05 [1080p]", "o5", "https://cdn.partner.example/o.mkv", 9, ago(minutes=5))))
    mon = monitor(cfg, db, ctx)
    s1 = await mon.poll_once()
    assert s1["feed_new"] == 1 and s1["feed_releases_new"] == 2
    rel = await db.releases.find_one({"guid": "g5"})
    assert rel["matched_key"] == "anilist:21:5" and rel["episode"] == 5 and rel["first_seen"]
    s2 = await mon.poll_once()
    assert s2["feed_new"] == 0 and s2["feed_releases_new"] == 0
    assert await db.releases.count_documents({}) == 2 and await db.episodes.count_documents({}) == 1


@pytest.mark.asyncio
async def test_reconciles_after_downtime_but_never_backfills(cfg, db, ctx, monkeypatch):
    cfg = with_cfg(cfg, lookback_hours=72, reconcile_max_hours=168)
    await db.upsert_anime(21, dict(ANIME))
    await add_feed(db)
    # the bot was offline for 5 days: last successful feed check 5 days ago
    await db.sources.update_one({"_id": "partner"}, {"$set": {"last_success_at": utcnow() - timedelta(days=5)}})
    serve(monkeypatch, rss(
        ("Frieren - 04 [1080p]", "g4", "https://cdn.partner.example/4.mkv", 9, ago(days=4)),     # missed
        ("Frieren - 01 [1080p]", "g1", "https://cdn.partner.example/1.mkv", 9, ago(days=10))))   # too old
    stats = await monitor(cfg, db, ctx).poll_once()
    assert stats["feed_new"] == 1
    assert await db.get_episode("anilist:21:4") and await db.get_episode("anilist:21:1") is None


def test_reconcile_start_bounds(cfg, db, ctx):
    mon = monitor(with_cfg(cfg, lookback_hours=72, reconcile_max_hours=168), db, ctx)
    now = utcnow()
    assert mon.reconcile_start(now, None) == now - timedelta(hours=72)
    assert mon.reconcile_start(now, now - timedelta(hours=100)) == now - timedelta(hours=101)
    assert mon.reconcile_start(now, now - timedelta(days=30)) == now - timedelta(hours=168)


@pytest.mark.asyncio
async def test_feed_gap_and_persistent_failure_alerts(cfg, db, ctx, client, monkeypatch):
    cfg = with_cfg(cfg, feed_alert_after_failures=2)
    await db.upsert_anime(21, dict(ANIME))
    await add_feed(db)
    await db.sources.update_one({"_id": "partner"}, {"$set": {"last_success_at": utcnow() - timedelta(days=3)}})
    serve(monkeypatch, rss(*[(f"Show{i} - 01 [1080p]", f"g{i}", f"https://cdn.partner.example/{i}.mkv", 9,
                              ago(hours=i)) for i in range(1, 7)]))
    await monitor(cfg, db, ctx).poll_once()
    assert any("may be missing" in c[2] for c in client.sent("send_message") if c[1] == ADMIN)
    serve(monkeypatch, ns.DownloadError("Feed HTTP 503"))
    mon = monitor(cfg, db, ctx)
    for _ in range(2):
        await db.sources.update_one({"_id": "partner"}, {"$set": {"retry_at": None}})
        ctx.sources._retry_at.clear()
        for ad in await ctx.sources.feed_adapters():
            ad._cache = None                       # the 60 s feed cache would hide the outage
        await mon.poll_once()
    assert any("failed 2 checks in a row" in c[2] for c in client.sent("send_message") if c[1] == ADMIN)


# ----------------------------------------- VALIDATING state + failure codes
@pytest.fixture
def fake_download(monkeypatch):
    clip = {"path": None}

    async def dl(session, url, dest, allowlist, **kw):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(clip["path"], dest)
        return ns.DownloadResult(dest, os.path.getsize(dest), hashlib.sha256(open(dest, "rb").read()).hexdigest())
    monkeypatch.setattr(ns, "download", dl)
    return clip


async def feed_episode(cfg, db, ctx, monkeypatch, path, sha=None):
    await db.upsert_anime(21, dict(ANIME))
    await add_feed(db)
    sha_tag = f"<sha256>{sha}</sha256>" if sha else ""
    body = (f'<rss><channel><item><title>Frieren - 05 [1080p]</title><guid>g5</guid>{sha_tag}'
            f'<pubDate>{ago(minutes=5)}</pubDate><enclosure url="https://cdn.partner.example/5.mkv" '
            f'length="{os.path.getsize(path)}" type="video/x-matroska"/></item></channel></rss>').encode()
    serve(monkeypatch, body)
    await monitor(cfg, db, ctx).poll_once()


async def drive(p, db, cfg, limit=10):
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
async def test_lifecycle_has_validating_and_records_upload(cfg, db, client, ctx, monkeypatch, tmp_path,
                                                           fake_download):
    fake_download["path"] = str(make_media(tmp_path / "a.mkv"))
    await feed_episode(cfg, db, ctx, monkeypatch, fake_download["path"])
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await drive(p, db, cfg))[-1] == M.PUBLISHED
    ep = await db.get_episode("anilist:21:5")
    history = [h["event"] for h in ep["history"]]
    order = [M.DOWNLOADING, M.VALIDATING, M.PROCESSING, M.UPLOADING, M.UPLOADED, M.PUBLISHED]
    assert [h for h in history if h in order] == order
    assert ep["validation"]["sha256"] == hashlib.sha256(open(fake_download["path"], "rb").read()).hexdigest()
    up = await db.get_upload("anilist:21:5")
    assert up["storage_chat_id"] == STORAGE and up["message_id"] == ep["storage"]["message_id"]
    assert up["anime_id"] == 21 and up["episode"] == 5 and up["sha256"] == ep["validation"]["sha256"]
    assert up["quality"] and up["status"] == "published"
    events = [e["event"] for e in await db.recent_events("anilist:21:5", 50)]
    assert "validated" in events and "downloaded" in events
    assert M.public_state(ep) == "PUBLISHED" and ep.get("failure_code") is None


@FFMPEG
@pytest.mark.asyncio
async def test_checksum_mismatch_is_validation_failed(cfg, db, client, ctx, monkeypatch, tmp_path, fake_download):
    fake_download["path"] = str(make_media(tmp_path / "a.mkv"))
    await feed_episode(cfg, db, ctx, monkeypatch, fake_download["path"], sha="0" * 64)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    states = await drive(p, db, cfg, limit=3)
    assert M.PUBLISHED not in states
    ep = await db.get_episode("anilist:21:5")
    assert ep["failure_code"] == M.VALIDATION_FAILED and "checksum" in ep["last_error"]
    assert not [c for c in client.calls if c[1] in (STORAGE, POST)]
    assert await db.get_upload("anilist:21:5") is None


async def inbox_episode(db, ep=5):
    await db.upsert_anime(21, dict(ANIME))
    await db.upsert_schedule(21, ep, utcnow() - timedelta(minutes=5), "anilist")
    await db.mark_aired()
    await db.add_inbox_file(M.episode_key(21, ep), {"file_id": "ADMINFILE", "file_size": 5000,
                                                    "file_name": "ep.mkv", "media_type": "video"})


@pytest.mark.asyncio
async def test_upload_failure_code_and_status(cfg, db, client, ctx):
    cfg = with_cfg(cfg, max_retries=1)
    await inbox_episode(db)
    client.fail["send_cached_media"] = ChatWriteForbidden()
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await drive(p, db, cfg))[-1] == M.FAILED
    ep = await db.get_episode("anilist:21:5")
    assert ep["failure_code"] == M.UPLOAD_FAILED and M.public_state(ep) == M.UPLOAD_FAILED
    assert any("UPLOAD_FAILED" in c[2] for c in client.sent("send_message") if c[1] == ADMIN)
    assert (await db.count_failure_codes())[M.UPLOAD_FAILED] == 1
    m = FakeMessage("/status", user_id=ADMIN)
    await admin.cmd_status(ctx, client, m)
    text = m.replies[-1][0]
    assert "MongoDB: ok" in text and "UPLOAD_FAILED=1" in text
    # no post was ever made for an unconfirmed upload
    assert not [c for c in client.calls if c[1] == POST]


@pytest.mark.asyncio
async def test_confirmed_upload_record_prevents_duplicate_upload(cfg, db, client, ctx):
    await inbox_episode(db)
    key = "anilist:21:5"
    # Crash simulation: Telegram confirmed the upload and it was recorded, but the state was not saved.
    await db.record_upload(key, {"chat_id": STORAGE, "message_id": 77, "file_id": "FID77", "media_type": "video",
                                 "file_size": 5000}, quality="1080p")
    await db.episodes.update_one({"_id": key}, {"$set": {"state": M.UPLOADING, "candidate": SourceItem(
        "inbox", "telegram", "ADMINFILE", "ep.mkv").to_doc()}})
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await drive(p, db, cfg))[-1] == M.PUBLISHED
    assert client.sent("send_cached_media") == []
    assert (await db.get_episode(key))["storage"]["message_id"] == 77


def test_public_state_mapping():
    assert M.public_state({"state": M.SCHEDULED}) == "DETECTED"
    assert M.public_state({"state": M.AIRING}) == "QUEUED"
    assert M.public_state({"state": M.AIRING, "source_status": M.SOURCE_NOT_CONFIGURED}) == M.SOURCE_NOT_CONFIGURED
    assert M.public_state({"state": M.VALIDATING}) == "VALIDATING"
    assert M.public_state({"state": M.FAILED, "failure_code": M.DOWNLOAD_FAILED}) == M.DOWNLOAD_FAILED
    assert M.public_state({"state": M.FAILED, "failed_stage": M.PROCESSING}) == M.PROCESSING_FAILED


@pytest.mark.asyncio
async def test_missing_source_is_reported_not_silent(cfg, db, client, ctx):
    await db.upsert_anime(21, dict(ANIME))
    await db.upsert_schedule(21, 5, utcnow() - timedelta(minutes=5), "anilist")
    await db.mark_aired()
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    await drive(p, db, cfg, limit=1)
    ep = await db.get_episode("anilist:21:5")
    assert M.public_state(ep) == M.SOURCE_NOT_CONFIGURED
    assert any(M.SOURCE_NOT_CONFIGURED in c[2] for c in client.sent("send_message") if c[1] == ADMIN)
    assert (await db.count_failure_codes())[M.SOURCE_NOT_CONFIGURED] == 1


@FFMPEG
@pytest.mark.asyncio
async def test_restart_during_validating_redownloads(cfg, db, client, ctx, monkeypatch, tmp_path, fake_download):
    fake_download["path"] = str(make_media(tmp_path / "a.mkv"))
    await feed_episode(cfg, db, ctx, monkeypatch, fake_download["path"])
    item = (await ctx.sources.find_with_reason({"_id": 21, **ANIME}, 5))[0]
    await db.episodes.update_one({"_id": "anilist:21:5"}, {"$set": {
        "state": M.VALIDATING, "candidate": item.to_doc(), "source_path": "/tmp/gone/after/restart.mkv"}})
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    states = await drive(p, db, cfg)
    assert states[0] == M.AVAILABLE and states[-1] == M.PUBLISHED


# ----------------------------------- delivery failure, /checkfiles, /reupload
async def published(cfg, db, client, ctx):
    await inbox_episode(db)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await drive(p, db, cfg))[-1] == M.PUBLISHED
    client.members[(FSUB, USER)] = "member"
    return p, await db.get_episode("anilist:21:5")


@pytest.mark.asyncio
async def test_deleted_storage_message_alerts_and_reupload_keeps_link(cfg, db, client, ctx):
    p, ep = await published(cfg, db, client, ctx)
    from pyrogram import enums
    client.members[(FSUB, USER)] = enums.ChatMemberStatus.MEMBER
    client.fail["copy_message"] = [MessageIdInvalid()] * 3
    client.fail["send_cached_media"] = [MessageIdInvalid()] * 3
    m = FakeMessage(f"/start {ep['token']}")
    await user.start_handler(ctx, client, m)
    assert "couldn" in m.replies[-1][0].lower()
    alerts = [c[2] for c in client.sent("send_message") if c[1] == ADMIN]
    assert any("/reupload anilist:21:5" in a for a in alerts)
    assert ep["token"] not in " ".join(alerts)                      # no deep-link token / file ids in alerts
    assert (await db.get_upload("anilist:21:5"))["status"] == "delivery_failed"
    client.fail.clear()
    # /checkfiles finds the deleted storage message
    client.deleted = {ep["storage"]["message_id"]}
    cm = FakeMessage("/checkfiles", user_id=ADMIN)
    await admin.cmd_checkfiles(ctx, client, cm)
    assert "anilist:21:5" in cm.replies[-1][0] and "deleted" in cm.replies[-1][0]
    # /reupload: same token, same post, exactly one new storage upload, no new announcement
    posts_before = len([c for c in client.calls if c[1] == POST])
    rm = FakeMessage("/reupload anilist:21:5", user_id=ADMIN)
    await admin.cmd_reupload(ctx, client, rm)
    uploads_before = len(client.sent("send_cached_media"))
    assert (await drive(p, db, cfg))[-1] == M.PUBLISHED
    ep2 = await db.get_episode("anilist:21:5")
    assert ep2["token"] == ep["token"] and ep2["post_message_id"] == ep["post_message_id"]
    assert ep2["storage"]["message_id"] != ep["storage"]["message_id"]
    assert len(client.sent("send_cached_media")) == uploads_before + 1
    assert len([c for c in client.calls if c[1] == POST]) == posts_before
    # the old link now delivers again
    m2 = FakeMessage(f"/start {ep['token']}")
    await user.start_handler(ctx, client, m2)
    assert [c for c in client.calls if c[0] in ("copy_message", "send_cached_media") and c[1] == USER]


@pytest.mark.asyncio
async def test_invalid_deep_link_is_handled(cfg, db, client, ctx):
    for bad in ("/start ep$$$", "/start " + "x" * 300, "/start epdoesnotexist000"):
        m = FakeMessage(bad)
        await user.start_handler(ctx, client, m)
        assert m.replies, bad
    assert not [c for c in client.calls if c[0] in ("copy_message", "send_cached_media")]


@pytest.mark.asyncio
async def test_events_command(cfg, db, client, ctx):
    await published(cfg, db, client, ctx)
    m = FakeMessage("/events anilist:21:5", user_id=ADMIN)
    await admin.cmd_events(ctx, client, m)
    assert "published" in m.replies[-1][0]


# --------------------------------------------------------- daily schedule post
@pytest.mark.asyncio
async def test_schedule_post_once_per_day_and_dry_run(cfg, db, client, ctx):
    await db.upsert_anime(21, dict(ANIME))
    from datetime import datetime
    day = datetime(2026, 10, 28, 12, 0, tzinfo=IST)
    await db.upsert_schedule(21, 6, day.astimezone(IST), "anilist")
    text = await build_schedule_text(db, day)
    assert "Frieren" in text and "[12:00]" in text and "Episode 6" in text
    on = with_cfg(cfg, schedule_post_enabled=True, schedule_post_time="00:30")
    early = datetime(2026, 10, 28, 0, 10, tzinfo=IST)
    assert not await maybe_post_schedule(on, db, client, ctx.notifier, dry_run=True, now=early)
    late = datetime(2026, 10, 28, 0, 40, tzinfo=IST)
    assert await maybe_post_schedule(on, db, client, ctx.notifier, dry_run=True, now=late)
    assert not await maybe_post_schedule(on, db, client, ctx.notifier, dry_run=False, now=late)  # once per day
    assert not [c for c in client.calls if c[1] == POST]               # dry run → admins only
    assert any("DRY RUN" in c[2] for c in client.sent("send_message") if c[1] == ADMIN)
    nxt = datetime(2026, 10, 29, 1, 0, tzinfo=IST)
    assert await maybe_post_schedule(on, db, client, ctx.notifier, dry_run=False, now=nxt)
    assert [c for c in client.calls if c[1] == POST]
    off = with_cfg(cfg, schedule_post_enabled=False)
    assert not await maybe_post_schedule(off, db, client, ctx.notifier, dry_run=False,
                                         now=datetime(2026, 10, 30, 1, 0, tzinfo=IST))
