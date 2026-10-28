"""Telegram provider channel source: an authorized provider uploads files to a channel and ANIME SOUL
uploads + publishes them automatically (no /attach). Also the HTTPS content-type check and reporter."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pyrogram import enums
from pyrogram.errors import ChatWriteForbidden, FloodWait

from anime_soul import models as M
from anime_soul import netsafe as ns
from anime_soul.handlers import admin, source_ingest, user
from anime_soul.monitor import ReleaseMonitor
from anime_soul.pipeline import Pipeline
from anime_soul.sources import build_adapter
from anime_soul.sources.telegram_channel import TelegramChannelSource, extract_media, parse_post
from anime_soul.timeutil import utcnow
from tests.conftest import ADMIN, FSUB, POST, STORAGE, USER, FakeMessage, Media, with_cfg

PROVIDER = -1004000000009
ANIME = {"title_romaji": "Sousou no Frieren", "title_english": "Frieren", "season_number": 2,
         "quality": "1080p", "use_cover": False}
MB = 1024 * 1024


def post(mid, name="[Partner] Frieren S2 - 05 [1080p].mkv", *, uid=None, size=300 * MB, mime="video/x-matroska",
         duration=1420, caption=None, kind="document", chat=PROVIDER, height=1080):
    media = Media(file_id=f"PFID{mid}", file_unique_id=uid or f"PUID{mid}", file_size=size, file_name=name,
                  mime_type=mime, duration=duration, width=1920, height=height)
    msg = SimpleNamespace(id=mid, chat=SimpleNamespace(id=chat), caption=caption, video=None, document=None,
                          animation=None, empty=False, date=datetime.now(timezone.utc) - timedelta(minutes=1))
    setattr(msg, kind, media)
    return msg


async def add_provider(ctx, client, name="partner"):
    client.members[(PROVIDER, "me")] = enums.ChatMemberStatus.ADMINISTRATOR
    m = FakeMessage(f"/sources add_tg {name} {PROVIDER} | Licensed by Partner Ltd, contract #42", user_id=ADMIN)
    await admin.cmd_sources(ctx, client, m)
    assert "✅" in m.replies[-1][0], m.replies
    return next(a for a in await ctx.sources.channel_adapters(force=True) if a.name == name)


async def tracked(db, anime_id=21):
    await db.upsert_anime(anime_id, dict(ANIME))


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


# ------------------------------------------------------------------ parsing / validation
def test_extract_media_accepts_video_rejects_other_documents():
    assert extract_media(post(1))["file_unique_id"] == "PUID1"
    assert extract_media(post(2, kind="video", mime="video/mp4", name="x.mp4"))["media_type"] == "video"
    assert extract_media(post(3, name="notes.pdf", mime="application/pdf")) is None
    assert extract_media(post(4, name="setup.exe", mime="application/octet-stream")) is None
    assert extract_media(SimpleNamespace(video=None, document=None)) is None


def test_parse_post_from_caption_and_anilist_tag():
    m = parse_post(extract_media(post(1, name="video.mkv")), "Frieren Season 2\nEpisode 7 · 1080p\n#anilist21")
    assert m["episode"] == 7 and m["anilist_id"] == 21
    m2 = parse_post(extract_media(post(2)), "")
    assert m2["episode"] == 5 and m2["quality"] == "1080p" and not m2["is_batch"]
    m4 = parse_post(extract_media(post(4, name="Frieren S02E05.mkv", height=720)), "")
    assert m4["season"] == 2 and m4["quality"] == "720p"            # quality from Telegram's video height
    m3 = parse_post(extract_media(post(3, name="[Partner] Frieren S2 (01-12) [Batch].mkv")), "")
    assert m3["is_batch"]


def test_build_adapter_requires_authorization_and_chat_id(cfg):
    with pytest.raises(ValueError):
        build_adapter({"_id": "x", "type": "telegram_channel", "config": {"chat_id": PROVIDER}}, cfg)
    with pytest.raises(ValueError):
        build_adapter({"_id": "x", "type": "telegram_channel", "config": {"chat_id": "abc"},
                       "authorized": True, "license_note": "ok"}, cfg)
    ad = build_adapter({"_id": "x", "type": "telegram_channel", "config": {"chat_id": PROVIDER},
                        "authorized": True, "license_note": "ok"}, cfg)
    assert isinstance(ad, TelegramChannelSource) and ad.chat_id == PROVIDER


# ------------------------------------------------------------------ admin registration
@pytest.mark.asyncio
async def test_add_tg_requires_licence_and_admin_rights(ctx, client):
    m = FakeMessage(f"/sources add_tg partner {PROVIDER}", user_id=ADMIN)
    await admin.cmd_sources(ctx, client, m)
    assert "licence" in m.replies[-1][0].lower()
    client.members[(PROVIDER, "me")] = enums.ChatMemberStatus.MEMBER
    m = FakeMessage(f"/sources add_tg partner {PROVIDER} | licensed", user_id=ADMIN)
    await admin.cmd_sources(ctx, client, m)
    assert "administrator" in m.replies[-1][0]
    m = FakeMessage(f"/sources add_tg partner {STORAGE} | licensed", user_id=ADMIN)
    await admin.cmd_sources(ctx, client, m)
    assert "separate intake channel" in m.replies[-1][0]
    assert await ctx.db.list_sources() == []
    ad = await add_provider(ctx, client)
    assert ad.chat_id == PROVIDER


@pytest.mark.asyncio
async def test_non_admin_cannot_register_sources(ctx, client):
    m = FakeMessage(f"/sources add_tg partner {PROVIDER} | licensed", user_id=USER)
    await admin.guard(admin.cmd_sources)(ctx, client, m)
    assert "only for ANIME SOUL administrators" in m.replies[-1][0]
    assert await ctx.db.list_sources() == []


# ------------------------------------------------------------------ the full automatic flow
@pytest.mark.asyncio
async def test_provider_post_is_uploaded_published_and_delivered(cfg, db, client, ctx, filedb):
    await tracked(db)
    await add_provider(ctx, client)
    # the provider uploads the episode into its channel → live handler
    await source_ingest.provider_post_handler(ctx, client, post(501))
    f = await db.source_files.find_one({"_id": f"{PROVIDER}:501"})
    assert f["status"] == "ok" and f["matched_key"] == "anilist:21:5"
    ep = await db.get_episode("anilist:21:5")
    assert ep and ep["state"] == M.AIRING                 # created + queued without /attach
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await drive(p, db, cfg))[-1] == M.PUBLISHED
    stored = client.sent("send_cached_media")
    assert stored[0][1] == STORAGE and stored[0][2] == "PFID501"     # copied by file_id, no re-download
    ep = await db.get_episode("anilist:21:5")
    assert ep["candidate"]["source"] == "partner" and ep["storage"]["chat_id"] == STORAGE
    up = await db.get_upload("anilist:21:5")
    assert up["message_id"] == ep["storage"]["message_id"] and up["status"] == "published"
    # public post carries the GET EPISODE button with the existing deep-link format
    posts = [c for c in client.calls if c[1] == POST]
    assert len(posts) == 1
    markup = posts[0][-1]["reply_markup"]
    btn = markup.inline_keyboard[0][0]
    assert "GET EPISODE" in btn.text.upper() and btn.url.endswith(f"?start={ep['token']}")
    # the button opens the bot: FSUB is enforced, then the stored file is delivered
    m = FakeMessage(f"/start {ep['token']}")
    await user.start_handler(ctx, client, m)
    assert not [c for c in client.calls if c[0] in ("copy_message", "send_cached_media") and c[1] == USER]
    client.members[(FSUB, USER)] = enums.ChatMemberStatus.MEMBER
    m2 = FakeMessage(f"/start {ep['token']}")
    await user.start_handler(ctx, client, m2)
    assert [c for c in client.calls if c[0] in ("copy_message", "send_cached_media") and c[1] == USER]


@pytest.mark.asyncio
async def test_unregistered_chat_and_non_video_are_ignored(cfg, db, client, ctx):
    await tracked(db)
    await add_provider(ctx, client)
    await source_ingest.provider_post_handler(ctx, client, post(1, chat=-1009999))
    await source_ingest.provider_post_handler(ctx, client, post(2, name="notes.pdf", mime="application/pdf"))
    assert await db.source_files.count_documents({}) == 0
    assert await db.get_episode("anilist:21:5") is None


@pytest.mark.asyncio
async def test_invalid_files_rejected_never_uploaded(cfg, db, client, ctx):
    await tracked(db)
    ad = await add_provider(ctx, client)
    assert await ad.ingest(post(1, size=3000 * MB)) == "rejected"
    assert await ad.ingest(post(2, duration=10)) == "rejected"
    assert await ad.ingest(post(3, name="[Partner] Frieren S2 (01-12) [Batch].mkv")) == "rejected"
    docs = await db.source_files.find({}).to_list(length=10)
    assert {d["status"] for d in docs} == {"rejected"}
    assert "too large" in docs[0]["reject_reason"]
    assert await db.get_episode("anilist:21:5") is None
    m = FakeMessage("/unmatched", user_id=ADMIN)
    await admin.cmd_unmatched(ctx, client, m)
    assert "too short" in m.replies[-1][0]
    m = FakeMessage(f"/assign {PROVIDER}:1 21 5", user_id=ADMIN)
    await admin.cmd_assign(ctx, client, m)
    assert "rejected" in m.replies[-1][0]
    assert await db.get_episode("anilist:21:5") is None


@pytest.mark.asyncio
async def test_duplicate_posts_never_upload_or_post_twice(cfg, db, client, ctx):
    await tracked(db)
    ad = await add_provider(ctx, client)
    assert await ad.ingest(post(10)) == "new"
    assert await ad.ingest(post(10)) == "duplicate"                 # same message (live + catch-up)
    assert await ad.ingest(post(11, uid="PUID10")) == "duplicate"   # same file re-posted
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await drive(p, db, cfg))[-1] == M.PUBLISHED
    # a 720p copy of the same episode after publishing creates no new upload or post
    assert await ad.ingest(post(12, name="[Partner] Frieren S2 - 05 [720p].mkv", height=720)) == "new"
    await drive(p, db, cfg)
    assert len(client.sent("send_cached_media")) == 1
    assert len([c for c in client.calls if c[1] == POST]) == 1


@pytest.mark.asyncio
async def test_prefers_tracked_quality_and_v2(cfg, db, client, ctx):
    await tracked(db)
    ad = await add_provider(ctx, client)
    await ad.ingest(post(20, name="[Partner] Frieren S2 - 05 [720p].mkv", height=720))
    await ad.ingest(post(21, name="[Partner] Frieren S2 - 05 [1080p].mkv"))
    await ad.ingest(post(22, name="[Partner] Frieren S2 - 05v2 [1080p].mkv"))
    item = await ad.find(await db.get_anime(21), 5)
    assert item.ref == "PFID22" and item.version == 2 and item.quality == "1080p" and item.kind == "telegram"


@pytest.mark.asyncio
async def test_unmatched_file_alerts_and_assign(cfg, db, client, ctx):
    await tracked(db)
    ad = await add_provider(ctx, client)
    assert await ad.ingest(post(30, name="ep_final_master.mkv"), ctx.notifier) == "unmatched"
    alerts = [c[2] for c in client.sent("send_message") if c[1] == ADMIN]
    assert any(f"/assign {PROVIDER}:30" in a for a in alerts)
    assert "PFID30" not in " ".join(alerts)                           # no file IDs in admin alerts
    m = FakeMessage(f"/assign {PROVIDER}:30 21 6", user_id=ADMIN)
    await admin.cmd_assign(ctx, client, m)
    assert "anilist:21:6" in m.replies[-1][0]
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await drive(p, db, cfg))[-1] == M.PUBLISHED
    assert client.sent("send_cached_media")[0][2] == "PFID30"


@pytest.mark.asyncio
async def test_unmatched_file_matches_after_anime_is_added(cfg, db, client, ctx):
    ad = await add_provider(ctx, client)
    assert await ad.ingest(post(40)) == "unmatched"
    await tracked(db)
    assert await ad.rematch_unmatched() == 1
    assert (await db.get_episode("anilist:21:5"))["state"] == M.AIRING


# ------------------------------------------------------------------ catch-up after downtime
@pytest.mark.asyncio
async def test_monitor_catches_up_missed_posts(cfg, db, client, ctx):
    await tracked(db)
    ad = await add_provider(ctx, client)
    await ad.ingest(post(100, name="[Partner] Frieren S2 - 04 [1080p].mkv"))   # last post seen live
    channel = {103: post(103), 105: post(105, name="[Partner] Frieren S2 - 06 [1080p].mkv")}
    asked = []

    async def get_messages(chat_id, ids):
        asked.append((chat_id, ids[0], ids[-1]))
        return [channel.get(i) or SimpleNamespace(id=i, empty=True) for i in ids]
    client.get_messages = get_messages

    class NoAniList:
        async def airing_window(self, *a):
            return []

        async def discover_releasing(self, **kw):
            return []

        async def anime_info(self, ids):
            return {}

        async def close(self):
            pass
    mon = ReleaseMonitor(with_cfg(cfg, auto_discover_airing_anime=False), db, ctx.notifier, anilist=NoAniList(), jikan=False,
                         sources=ctx.sources)
    stats = await mon.poll_once()
    assert stats["provider_files"] == 2
    assert asked[0] == (PROVIDER, 101, 200) and len(asked) == 2        # stops after an empty batch
    assert await db.get_episode("anilist:21:5") and await db.get_episode("anilist:21:6")
    assert (await db.sources.find_one({"_id": "partner"}))["last_message_id"] == 105
    stats = await mon.poll_once()                                       # no reprocessing
    assert stats["provider_files"] == 0


@pytest.mark.asyncio
async def test_catch_up_failure_backs_off_and_alerts(cfg, db, client, ctx):
    ad = await add_provider(ctx, client)
    await ad.ingest(post(100))

    async def broken(chat_id, ids):
        raise ChatWriteForbidden()
    client.get_messages = broken
    mon = ReleaseMonitor(with_cfg(cfg, feed_alert_after_failures=1), db, ctx.notifier, anilist=None, jikan=False,
                         sources=ctx.sources)
    stats = {"errors": 0}
    await mon._provider_channels(stats)
    assert stats["errors"] == 1 and ctx.sources.in_backoff("partner")
    assert any("still an admin" in c[2] for c in client.sent("send_message") if c[1] == ADMIN)


@pytest.mark.asyncio
async def test_sources_scan_backfills_older_posts(cfg, db, client, ctx):
    await tracked(db)
    ad = await add_provider(ctx, client)
    await ad.ingest(post(900, name="[Partner] Frieren S2 - 09 [1080p].mkv"))
    channel = {5: post(5)}

    async def get_messages(chat_id, ids):
        return [channel.get(i) or SimpleNamespace(id=i, empty=True) for i in ids]
    client.get_messages = get_messages
    m = FakeMessage("/sources scan partner 1", user_id=ADMIN)
    await admin.cmd_sources(ctx, client, m)
    assert "1 new" in m.replies[-1][0]
    assert await db.get_episode("anilist:21:5")
    assert (await db.sources.find_one({"_id": "partner"}))["last_message_id"] == 900   # not moved back


# ------------------------------------------------------------------ retries / failures
@pytest.mark.asyncio
async def test_temporary_upload_failure_is_retried(cfg, db, client, ctx):
    await tracked(db)
    ad = await add_provider(ctx, client)
    await ad.ingest(post(50))
    client.fail["send_cached_media"] = [FloodWait(value=0), OSError("connection reset")]
    p = Pipeline(with_cfg(cfg, max_retries=4), db, client, ctx.notifier, ctx.sources)
    states = await drive(p, db, cfg, limit=8)
    assert states[-1] == M.PUBLISHED
    assert len([c for c in client.calls if c[1] == POST]) == 1


@pytest.mark.asyncio
async def test_permanent_upload_failure_is_reported_not_published(cfg, db, client, ctx):
    await tracked(db)
    ad = await add_provider(ctx, client)
    await ad.ingest(post(60))
    client.fail["send_cached_media"] = ChatWriteForbidden()
    cfg = with_cfg(cfg, max_retries=1)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await drive(p, db, cfg))[-1] == M.FAILED
    ep = await db.get_episode("anilist:21:5")
    assert ep["failure_code"] == M.UPLOAD_FAILED and not ep.get("post_message_id")
    assert not [c for c in client.calls if c[1] == POST]
    assert any("UPLOAD_FAILED" in c[2] for c in client.sent("send_message") if c[1] == ADMIN)


@pytest.mark.asyncio
async def test_dry_run_never_uploads_provider_files(cfg, db, client, ctx):
    await tracked(db)
    ad = await add_provider(ctx, client)
    await ad.ingest(post(70))
    cfg = with_cfg(cfg, dry_run=True)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    await drive(p, db, cfg, limit=3)
    assert client.sent("send_cached_media") == [] and not [c for c in client.calls if c[1] == POST]


# ------------------------------------------------------------------ progress reporter
@pytest.mark.asyncio
async def test_log_channel_reporter_edits_one_message(cfg, db, client, ctx):
    LOG = -1005000000001
    edits = []

    async def edit_message_text(chat_id, mid, text, **kw):
        edits.append((chat_id, mid, text))
    client.edit_message_text = edit_message_text
    await tracked(db)
    ad = await add_provider(ctx, client)
    await ad.ingest(post(80))
    cfg = with_cfg(cfg, log_channel_id=LOG)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert (await drive(p, db, cfg))[-1] == M.PUBLISHED
    sent = [c for c in client.sent("send_message") if c[1] == LOG]
    assert len(sent) == 1 and "Frieren" in sent[0][2]
    assert edits and "DONE" in edits[-1][2] and all(e[1] == edits[0][1] for e in edits)
    assert "PFID80" not in " ".join(e[2] for e in edits)


# ------------------------------------------------------------------ HTTPS source content-type check
def test_media_content_type_check():
    assert ns.media_content_type_problem("video/x-matroska") is None
    assert ns.media_content_type_problem("application/octet-stream; charset=binary") is None
    assert ns.media_content_type_problem(None) is None
    assert "text/html" in ns.media_content_type_problem("text/html; charset=utf-8")
    assert ns.media_content_type_problem("application/json")
    assert ns.media_content_type_problem("application/x-mpegurl")


@pytest.mark.asyncio
async def test_download_rejects_html_page(monkeypatch, tmp_path):
    class Resp:
        status = 200
        headers = {"Content-Type": "text/html", "Content-Length": "10"}

        def release(self):
            pass

    async def fake_open(session, url, allowlist, allow_http, headers):
        return Resp()
    monkeypatch.setattr(ns, "_open", fake_open)
    with pytest.raises(ns.DownloadError) as e:
        await ns.download(None, "https://cdn.partner.example/a.mkv", str(tmp_path / "a.mkv"), ("cdn.partner.example",),
                          max_bytes=10 * MB, timeout_s=5)
    assert not e.value.transient and "not a video" in str(e.value)
    assert not (tmp_path / "a.mkv").exists() and not (tmp_path / "a.mkv.part").exists()


@pytest.mark.asyncio
async def test_two_dynos_receiving_same_post_store_it_once(cfg, db, client, ctx):
    """If the worker is accidentally scaled to 2, both receive the post; only one queues it."""
    import asyncio
    await tracked(db)
    await add_provider(ctx, client)
    doc = await db.sources.find_one({"_id": "partner"})
    a1, a2 = build_adapter(doc, cfg, db), build_adapter(doc, cfg, db)
    results = await asyncio.gather(a1.ingest(post(1)), a2.ingest(post(1)))
    assert sorted(results) == ["duplicate", "new"]
    assert await db.source_files.count_documents({}) == 1
    assert await db.episodes.count_documents({}) == 1
