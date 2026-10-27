"""Upload, publish, duplicate protection, retries and recovery."""
import asyncio
import os
import time
from datetime import timedelta

import pytest
from pyrogram.errors import ChatWriteForbidden, FloodWait

from anime_soul import models as M
from anime_soul.db import WORKER_ID
from anime_soul.pipeline import Pipeline, backoff_seconds
from anime_soul.timeutil import utcnow
from tests.conftest import POST, STORAGE, with_cfg

MKV = b"\x1a\x45\xdf\xa3" + b"\x00" * 4000
ANIME = {"title_romaji": "Sousou no Frieren", "title_english": "Frieren", "quality": "1080p",
         "season_number": 2, "cover_url": None, "use_cover": False}


async def setup_episode(db, ep=5, attach=True):
    await db.upsert_anime(21, dict(ANIME))
    await db.upsert_schedule(21, ep, utcnow() - timedelta(minutes=5), "anilist")
    await db.mark_aired()
    if attach:
        await db.add_inbox_file(M.episode_key(21, ep), {"file_id": "ADMINFILE", "file_size": 5000,
                                                        "file_name": "ep.mkv", "media_type": "video"})


def make_pipeline(cfg, db, client, ctx):
    return Pipeline(cfg, db, client, ctx.notifier, ctx.sources)


async def claim_and_process(p, db, cfg):
    doc = await db.claim_next(cfg.job_lease_seconds)
    assert doc is not None
    return await p.process(doc)


@pytest.mark.asyncio
async def test_full_flow_inbox_upload_publish(cfg, db, client, ctx):
    await setup_episode(db)
    p = make_pipeline(cfg, db, client, ctx)
    assert await claim_and_process(p, db, cfg) == M.PUBLISHED
    ep = await db.get_episode("anilist:21:5")
    assert ep["storage"]["file_id"].startswith("FID") and ep["storage"]["chat_id"] == STORAGE
    assert M.is_valid_token(ep["token"]) and ep["token"].startswith("ep")
    up = client.sent("send_cached_media")
    assert up[0][1] == STORAGE and up[0][2] == "ADMINFILE"
    post = client.sent("send_message")[-1]
    assert post[1] == POST and "Frieren" in post[2]
    button = post[3]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "GET EPISODE"
    assert button.url == f"https://t.me/AnimeSoulFileBot?start={ep['token']}"
    assert (await db.get_post("anilist:21:5"))["state"] == "published"


@pytest.mark.asyncio
async def test_storage_upload_happens_before_post(cfg, db, client, ctx):
    await setup_episode(db)
    client.fail["send_cached_media"] = ChatWriteForbidden()
    p = make_pipeline(cfg, db, client, ctx)
    state = await claim_and_process(p, db, cfg)
    assert state == M.UPLOADING                 # retry the upload later, nothing posted
    assert client.sent("send_message") == [] or all(c[1] != POST for c in client.sent("send_message"))
    ep = await db.get_episode("anilist:21:5")
    assert ep.get("storage") is None and ep["attempts"] == 1 and ep["next_attempt_at"] > utcnow()


@pytest.mark.asyncio
async def test_no_duplicate_upload_or_post_after_restart(cfg, db, client, ctx):
    await setup_episode(db)
    p = make_pipeline(cfg, db, client, ctx)
    # Post fails transiently after the upload succeeded.
    client.fail["send_message"] = [ChatWriteForbidden()]
    assert await claim_and_process(p, db, cfg) == M.UPLOADED
    assert len(client.sent("send_cached_media")) == 1
    await db.episodes.update_one({"_id": "anilist:21:5"}, {"$set": {"next_attempt_at": utcnow()}})
    # "Restart": a new pipeline instance picks the episode up again.
    p2 = make_pipeline(cfg, db, client, ctx)
    assert await claim_and_process(p2, db, cfg) == M.PUBLISHED
    assert len(client.sent("send_cached_media")) == 1          # never re-uploaded
    posts = [c for c in client.sent("send_message") if c[1] == POST]
    assert len(posts) == 2 and (await db.get_post("anilist:21:5"))["state"] == "published"
    # A third run has nothing to do.
    assert await db.claim_next(cfg.job_lease_seconds) is None


@pytest.mark.asyncio
async def test_duplicate_post_claim_blocked(db):
    assert await db.claim_post("anilist:1:1", POST) is True
    assert await db.claim_post("anilist:1:1", POST) is False


@pytest.mark.asyncio
async def test_unique_episode_index(db):
    from pymongo.errors import DuplicateKeyError
    await db.episodes.insert_one({"_id": "a", "anime_id": 1, "episode": 1})
    with pytest.raises(DuplicateKeyError):
        await db.episodes.insert_one({"_id": "b", "anime_id": 1, "episode": 1})


@pytest.mark.asyncio
async def test_lease_prevents_concurrent_jobs_and_expires(cfg, db):
    await setup_episode(db)
    first = await db.claim_next(600)
    assert first and first["lease_owner"] == WORKER_ID
    assert await db.claim_next(600) is None                      # second worker gets nothing
    await db.episodes.update_one({"_id": first["_id"]}, {"$set": {"lease_until": utcnow() - timedelta(seconds=1)}})
    assert (await db.claim_next(600))["_id"] == first["_id"]     # crashed worker's lease expired


@pytest.mark.asyncio
async def test_waiting_for_source_backs_off(cfg, db, client, ctx):
    await setup_episode(db, attach=False)
    p = make_pipeline(cfg, db, client, ctx)
    assert await claim_and_process(p, db, cfg) == M.AIRING
    ep = await db.get_episode("anilist:21:5")
    assert ep["source_checks"] == 1 and ep["next_attempt_at"] > utcnow() and ep["lease_owner"] is None
    # Nothing uploaded/posted, no fake link; only an admin alert explaining why.
    assert all(c[1] not in (STORAGE, POST) for c in client.calls)
    assert ep["source_status"] == M.SOURCE_NOT_CONFIGURED and M.SOURCE_NOT_CONFIGURED in ep["last_error"]
    alerts = [c for c in client.sent("send_message") if M.SOURCE_NOT_CONFIGURED in c[2]]
    assert len(alerts) == 1 and alerts[0][1] == 1001


@pytest.mark.asyncio
async def test_local_source_upload_validates_and_keeps_library_file(cfg, db, client, ctx, tmp_path, sample_video):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "Frieren - S02E05 [1080p].mkv"
    f.write_bytes(sample_video.read_bytes())
    os.utime(f, (time.time() - 600,) * 2)
    await db.upsert_source("lib", {"type": "local_dir", "config": {"path": str(lib)}, "authorized": True,
                                   "license_note": "Own content", "enabled": True})
    await setup_episode(db, attach=False)
    p = make_pipeline(cfg, db, client, ctx)
    assert await claim_and_process(p, db, cfg) == M.PUBLISHED
    # H.264/AAC MKV without subtitles is remuxed (no re-encode) to a streamable MP4.
    vid_up = client.sent("send_video")[0]
    assert vid_up[1] == STORAGE and vid_up[3]["file_name"] == "Frieren - S02E05 [1080p].mp4"
    assert vid_up[3]["supports_streaming"] and vid_up[3]["duration"] == 40
    ep = await db.get_episode("anilist:21:5")
    assert ep["processing"]["action"] == "remux" and ep["media"]["audio_codecs"] == ["aac"]
    assert ep["storage"]["message_id"] and ep["storage"]["chat_id"] == STORAGE
    assert f.exists()                                            # never delete files we don't own
    assert not os.path.exists(vid_up[2])                         # temp remux removed after upload


@pytest.mark.asyncio
async def test_invalid_file_is_rejected_not_uploaded(cfg, db, client, ctx, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "Frieren - S02E05 [1080p].mkv"
    f.write_bytes(b"<html>fake</html>" * 100)
    os.utime(f, (time.time() - 600,) * 2)
    await db.upsert_source("lib", {"type": "local_dir", "config": {"path": str(lib)}, "authorized": True,
                                   "license_note": "Own content", "enabled": True})
    await setup_episode(db, attach=False)
    p = make_pipeline(cfg, db, client, ctx)
    assert await claim_and_process(p, db, cfg) == M.AIRING
    assert client.sent("send_document") == []
    assert any("Rejected file" in c[2] for c in client.sent("send_message"))
    assert (await db.get_episode("anilist:21:5"))["candidate"] is None


@pytest.mark.asyncio
async def test_retries_are_bounded(cfg, db, client, ctx):
    cfg = with_cfg(cfg, max_retries=2)
    await setup_episode(db)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    client.fail["send_cached_media"] = [ChatWriteForbidden(), ChatWriteForbidden()]
    assert await claim_and_process(p, db, cfg) == M.UPLOADING
    await db.episodes.update_one({"_id": "anilist:21:5"}, {"$set": {"next_attempt_at": utcnow()}})
    assert await claim_and_process(p, db, cfg) == M.FAILED
    assert any("failed after 2 attempts" in c[2] for c in client.sent("send_message"))
    assert await db.reset_for_retry("anilist:21:5")
    assert await claim_and_process(p, db, cfg) == M.PUBLISHED


@pytest.mark.asyncio
async def test_ambiguous_publish_is_not_reposted(cfg, db, client, ctx):
    await setup_episode(db)
    client.fail["send_message"] = [asyncio.TimeoutError()]
    p = make_pipeline(cfg, db, client, ctx)
    assert await claim_and_process(p, db, cfg) == M.FAILED
    assert (await db.get_post("anilist:21:5"))["state"] == "publishing"
    await db.reset_for_retry("anilist:21:5")                     # without "publish": claim kept
    assert await claim_and_process(p, db, cfg) == M.FAILED
    await db.reset_for_retry("anilist:21:5", include_publish=True)
    assert await claim_and_process(p, db, cfg) == M.PUBLISHED


@pytest.mark.asyncio
async def test_dry_run_uploads_nothing(cfg, db, client, ctx):
    cfg = with_cfg(cfg, dry_run=True)
    await setup_episode(db)
    p = Pipeline(cfg, db, client, ctx.notifier, ctx.sources)
    assert await claim_and_process(p, db, cfg) == M.DRY_RUN_OK
    assert client.sent("send_cached_media") == []
    msgs = client.sent("send_message")
    assert msgs and all(c[1] != POST for c in msgs) and "DRY RUN" in msgs[0][2]


@pytest.mark.asyncio
async def test_pause_stops_processing(cfg, db, client, ctx):
    await setup_episode(db)
    await db.set_setting("paused", True)
    p = make_pipeline(cfg, db, client, ctx)
    assert await claim_and_process(p, db, cfg) == M.AVAILABLE
    assert client.sent("send_cached_media") == []


@pytest.mark.asyncio
async def test_floodwait_is_honoured(cfg, db, client, ctx, monkeypatch):
    import anime_soul.tg as tg
    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        await real_sleep(0)
    monkeypatch.setattr(tg.asyncio, "sleep", fake_sleep)
    await setup_episode(db)
    client.fail["send_cached_media"] = [FloodWait(value=3)]
    p = make_pipeline(cfg, db, client, ctx)
    assert await claim_and_process(p, db, cfg) == M.PUBLISHED
    assert 4 in slept


@pytest.mark.asyncio
async def test_restart_with_wiped_tmp_redownloads(cfg, db, client, ctx):
    await setup_episode(db, attach=False)
    await db.set_state("anilist:21:5", M.UPLOADING, local_path="/tmp/does-not-exist.mkv",
                       candidate={"source": "feed", "kind": "url", "ref": "https://cdn.partner.example/x.mkv",
                                  "file_name": "x.mkv"})
    p = make_pipeline(cfg, db, client, ctx)
    assert await claim_and_process(p, db, cfg) == M.AVAILABLE
    assert client.calls == []


@pytest.mark.asyncio
async def test_shutdown_releases_leases(cfg, db, client, ctx):
    await setup_episode(db)
    await db.claim_next(600)
    p = make_pipeline(cfg, db, client, ctx)
    await p.shutdown(grace_s=0)
    assert (await db.get_episode("anilist:21:5"))["lease_owner"] is None


def test_backoff_grows_and_caps():
    assert backoff_seconds(0) < backoff_seconds(3) <= 3600 * 1.15
    assert backoff_seconds(20) <= 3600 * 1.15
