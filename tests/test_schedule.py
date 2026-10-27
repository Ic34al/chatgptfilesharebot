"""Release-schedule parsing, IST conversion, rescheduling and the monitor."""
from datetime import datetime, timedelta, timezone

import pytest

from anime_soul import models as M
from anime_soul.monitor import ReleaseMonitor
from anime_soul.schedule import AiringSlot, AnimeInfo, ScheduleError
from anime_soul.schedule.anilist import AniListProvider, parse_airing, parse_media
from anime_soul.schedule.jikan import next_broadcast
from anime_soul.tg import Notifier
from anime_soul.timeutil import fmt_ist, to_ist, utcnow


def test_parse_airing_and_ist_conversion():
    payload = {"data": {"Page": {"airingSchedules": [
        {"id": 1, "episode": 5, "airingAt": 1793008500, "mediaId": 21},   # 2026-10-26 09:55 UTC
        {"id": 2, "episode": "bad", "airingAt": 1, "mediaId": 21},       # malformed row is skipped
    ]}}}
    slots = parse_airing(payload)
    assert len(slots) == 1
    s = slots[0]
    assert (s.anime_id, s.episode) == (21, 5)
    assert s.airing_at == datetime(2026, 10, 26, 9, 55, tzinfo=timezone.utc)
    ist = to_ist(s.airing_at)
    assert (ist.hour, ist.minute) == (15, 25)   # UTC+5:30
    assert fmt_ist(s.airing_at) == "26 Oct 2026, 03:25 PM IST"


def test_parse_media_with_sequel():
    m = {"id": 1, "title": {"romaji": "Show"}, "status": "FINISHED", "nextAiringEpisode": None,
         "relations": {"edges": [{"relationType": "SEQUEL", "node": {"id": 2, "type": "ANIME", "title": {"romaji": "Show 2"},
                                                                     "status": "RELEASING", "format": "TV"}},
                                 {"relationType": "PREQUEL", "node": {"id": 0, "type": "ANIME", "title": {"romaji": "x"}}}]}}
    info = parse_media(m)
    assert [s.anime_id for s in info.sequels] == [2]


def test_jikan_next_broadcast_jst():
    after = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)  # Tuesday 09:00 JST
    nxt = next_broadcast({"day": "Sundays", "time": "23:30", "timezone": "Asia/Tokyo"}, after)
    assert nxt == datetime(2026, 11, 1, 14, 30, tzinfo=timezone.utc)
    assert next_broadcast({"day": None, "time": None}, after) is None


@pytest.mark.asyncio
async def test_upsert_schedule_new_same_rescheduled(db):
    t = utcnow() + timedelta(hours=5)
    assert await db.upsert_schedule(21, 1, t, "anilist") == "new"
    assert await db.upsert_schedule(21, 1, t + timedelta(seconds=10), "anilist") == "same"
    assert await db.upsert_schedule(21, 1, t + timedelta(hours=2), "anilist") == "rescheduled"
    doc = await db.get_episode("anilist:21:1")
    assert doc["history"][-1]["event"] == "rescheduled"
    # an estimate never overwrites a confirmed time
    assert await db.upsert_schedule(21, 1, t + timedelta(hours=9), "jikan", estimated=True) == "same"


@pytest.mark.asyncio
async def test_published_episode_is_never_rescheduled(db):
    t = utcnow() - timedelta(hours=1)
    await db.upsert_schedule(21, 2, t, "anilist")
    await db.set_state("anilist:21:2", M.PUBLISHED)
    assert await db.upsert_schedule(21, 2, t + timedelta(days=1), "anilist") == "same"
    assert (await db.get_episode("anilist:21:2"))["state"] == M.PUBLISHED


@pytest.mark.asyncio
async def test_mark_aired_only_past_episodes(db):
    await db.upsert_schedule(21, 1, utcnow() - timedelta(minutes=1), "anilist")
    await db.upsert_schedule(21, 2, utcnow() + timedelta(days=7), "anilist")
    moved = await db.mark_aired()
    assert [d["_id"] for d in moved] == ["anilist:21:1"]
    assert (await db.get_episode("anilist:21:2"))["state"] == M.SCHEDULED


class FakeAniList:
    def __init__(self, slots=None, infos=None, fail=False):
        self.slots, self.infos, self.fail = slots or [], infos or {}, fail

    async def airing_window(self, ids, start, end):
        if self.fail:
            raise ScheduleError("HTTP 500")
        return [s for s in self.slots if s.anime_id in ids]

    async def anime_info(self, ids):
        return {i: v for i, v in self.infos.items() if i in ids}


class FakeJikan:
    async def estimate(self, items, start, end):
        return [AiringSlot(i["anime_id"], i["next_episode"], start + timedelta(hours=1), "jikan", True)
                for i in items if i.get("next_episode")]

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_monitor_cycle_delay_alert_and_sequel(cfg, db, client):
    await db.upsert_anime(21, {"title_romaji": "One Piece", "next_episode": 3})
    past = utcnow() - timedelta(hours=5)
    sequel = AnimeInfo(500, "One Piece S2", status="NOT_YET_RELEASED", format="TV")
    mon = ReleaseMonitor(cfg, db, Notifier(client, cfg.all_admin_ids, 0),
                         anilist=FakeAniList([AiringSlot(21, 3, past, "anilist")],
                                             {21: AnimeInfo(21, "One Piece", status="RELEASING", sequels=[sequel])}),
                         jikan=FakeJikan())
    stats = await mon.poll_once()
    assert stats["new"] == 1 and stats["aired"] == 1 and stats["delayed"] == 1 and stats["sequels"] == 1
    assert (await db.get_episode("anilist:21:3"))["state"] == M.AIRING
    assert await db.get_anime(500)
    texts = [c[2] for c in client.sent("send_message")]
    assert any("no authorized file" in t for t in texts)
    assert any("Auto-tracking new season" in t for t in texts)
    # second cycle must not alert again for the same delay
    client.calls.clear()
    stats = await mon.poll_once()
    assert stats["delayed"] == 0


@pytest.mark.asyncio
async def test_monitor_falls_back_to_jikan(cfg, db, client):
    await db.upsert_anime(21, {"title_romaji": "One Piece", "id_mal": 21, "next_episode": 9})
    mon = ReleaseMonitor(cfg, db, Notifier(client, cfg.all_admin_ids, 0), anilist=FakeAniList(fail=True),
                         jikan=FakeJikan())
    stats = await mon.poll_once()
    assert stats["errors"] == 1 and stats["new"] == 1
    assert (await db.get_episode("anilist:21:9"))["estimated"] is True


class _Resp:
    def __init__(self, status, data=None, headers=None):
        self.status, self._data, self.headers = status, data or {}, headers or {}

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Sess:
    closed = False

    def __init__(self, responses):
        self.responses = responses

    def post(self, *a, **kw):
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_anilist_retries_after_429(monkeypatch):
    import anime_soul.schedule.anilist as mod
    sleeps = []

    real_sleep = mod.asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    ok = {"data": {"Page": {"pageInfo": {"hasNextPage": False}, "airingSchedules": [
        {"episode": 1, "airingAt": 1793013300, "mediaId": 21}]}}}
    p = AniListProvider(session=_Sess([_Resp(429, headers={"Retry-After": "7"}), _Resp(200, ok)]), per_minute=6000)
    slots = await p.airing_window([21], utcnow(), utcnow())
    assert len(slots) == 1 and 7 in sleeps


@pytest.mark.asyncio
async def test_anilist_gives_up_with_schedule_error(monkeypatch):
    import anime_soul.schedule.anilist as mod

    real_sleep = mod.asyncio.sleep

    async def fake_sleep(s):
        await real_sleep(0)
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    p = AniListProvider(session=_Sess([_Resp(502) for _ in range(4)]), per_minute=6000)
    with pytest.raises(ScheduleError):
        await p.airing_window([21], utcnow(), utcnow())
