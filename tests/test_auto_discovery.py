"""Automatic discovery of currently airing anime (AniList mocked; MongoDB mocked or real via REAL_MONGODB_URI)."""
from datetime import timedelta

import pytest

from anime_soul import models as M
from anime_soul.handlers import admin
from anime_soul.monitor import ReleaseMonitor, guess_season
from anime_soul.pipeline import Pipeline
from anime_soul.schedule import AiringSlot, AnimeInfo
from anime_soul.schedule.anilist import DISCOVER_QUERY, AniListProvider
from anime_soul.tg import Notifier
from anime_soul.timeutil import fmt_ist, utcnow
from tests.conftest import ADMIN, POST, STORAGE, FakeMessage, with_cfg
from tests.test_schedule import FakeAniList, FakeJikan


def media(i, title, status="RELEASING", nxt=None, english=None, synonyms=None):
    return {"id": i, "idMal": i + 1000, "format": "TV", "status": status, "season": "FALL", "seasonYear": 2026,
            "episodes": 12, "synonyms": synonyms or [], "title": {"romaji": title, "english": english},
            "coverImage": {"extraLarge": f"https://img.example/{i}.jpg"},
            "nextAiringEpisode": nxt}


def page(items, has_next):
    return {"data": {"Page": {"pageInfo": {"hasNextPage": has_next}, "media": items}}}


class ScriptedAniList(AniListProvider):
    """Real AniListProvider with query() replaced by scripted pages (no network)."""

    def __init__(self, pages):
        super().__init__(per_minute=6000)
        self.pages, self.calls = list(pages), []

    async def query(self, query, variables):
        self.calls.append((query, variables))
        return self.pages[min(len(self.calls), len(self.pages)) - 1]


# --------------------------------------------------------------- provider
@pytest.mark.asyncio
async def test_discover_parses_releasing_and_skips_others():
    p = ScriptedAniList([page([media(1, "Show A", nxt={"episode": 4, "airingAt": 1793013300}),
                               media(2, "Finished", status="FINISHED"),
                               media(3, "Show C", english="Show C EN")], False)])
    out = await p.discover_releasing()
    assert [i.anime_id for i in out] == [1, 3]
    a = out[0]
    assert a.title_romaji == "Show A" and a.id_mal == 1001 and a.status == "RELEASING" and a.format == "TV"
    assert a.next_episode == 4 and a.cover_url == "https://img.example/1.jpg"
    assert p.calls[0][0] is DISCOVER_QUERY and p.calls[0][1] == {"page": 1}
    assert "status: RELEASING" in DISCOVER_QUERY and "format_in: [TV, TV_SHORT, ONA]" in DISCOVER_QUERY


@pytest.mark.asyncio
async def test_discover_stops_when_no_next_page():
    p = ScriptedAniList([page([media(1, "A")], True), page([media(2, "B")], False), page([media(3, "C")], True)])
    out = await p.discover_releasing(max_pages=10)
    assert [i.anime_id for i in out] == [1, 2] and len(p.calls) == 2


@pytest.mark.asyncio
async def test_discover_respects_page_limit():
    p = ScriptedAniList([page([media(1, "A")], True)])
    await p.discover_releasing(max_pages=2)
    assert [c[1]["page"] for c in p.calls] == [1, 2]
    p = ScriptedAniList([page([media(1, "A")], True)])
    await p.discover_releasing(max_pages=99)          # hard cap of 10 pages
    assert len(p.calls) == 10
    p = ScriptedAniList([page([media(1, "A")], True)])
    await p.discover_releasing(max_pages=0)           # at least 1
    assert len(p.calls) == 1


def test_guess_season():
    assert guess_season(AnimeInfo(1, "Sousou no Frieren 2nd Season")) == 2
    assert guess_season(AnimeInfo(1, "X", title_english="Frieren Season 3")) == 3
    assert guess_season(AnimeInfo(1, "One Piece")) == 1


# ---------------------------------------------------------------- monitor
def monitor(cfg, db, client, anilist):
    return ReleaseMonitor(cfg, db, Notifier(client, cfg.all_admin_ids, 0), anilist=anilist, jikan=FakeJikan())


@pytest.mark.asyncio
async def test_new_anime_saved_and_scheduled_in_same_cycle(cfg, db, client):
    soon = (utcnow() + timedelta(hours=3)).replace(microsecond=0)
    found = [AnimeInfo(101, "New Show", status="RELEASING", format="TV", next_episode=4, id_mal=9),
             AnimeInfo(102, "Another Show 2nd Season", status="RELEASING", format="ONA")]
    al = FakeAniList(slots=[AiringSlot(101, 4, soon, "anilist")], discovered=found)
    stats = await monitor(cfg, db, client, al).poll_once()
    assert stats["auto_discovered"] == 2 and stats["new"] == 1
    a = await db.get_anime(101)
    assert a["active"] and a["auto_discovered"] and a["title_romaji"] == "New Show"
    assert a["quality"] == cfg.default_quality and a["language"] == cfg.default_language
    assert a["season_number"] == 1 and a["next_episode"] == 4 and a["aliases"] == []
    assert (await db.get_anime(102))["season_number"] == 2
    ep = await db.get_episode("anilist:101:4")                  # reloaded list → scheduled this cycle
    assert ep["state"] == M.SCHEDULED and ep["airing_at"] == soon
    assert al.discover_calls == [cfg.auto_discover_max_pages]
    last = await db.get_setting("monitor:last_stats")
    assert last["auto_discovered"] == 2 and "new" in last and "errors" in last


@pytest.mark.asyncio
async def test_tracked_and_removed_anime_are_not_duplicated_or_overwritten(cfg, db, client):
    await db.upsert_anime(101, {"title_romaji": "Mine", "quality": "720p", "season_number": 3, "aliases": ["M"]})
    await db.upsert_anime(102, {"title_romaji": "Removed"})
    await db.set_anime_active(102, False)
    found = [AnimeInfo(101, "Mine (AniList)", status="RELEASING"), AnimeInfo(102, "Removed", status="RELEASING")]
    mon = monitor(cfg, db, client, FakeAniList(discovered=found))
    for _ in range(2):
        assert (await mon.poll_once())["auto_discovered"] == 0
    assert await db.anime.count_documents({}) == 2
    mine = await db.get_anime(101)
    assert mine["quality"] == "720p" and mine["season_number"] == 3 and not mine.get("auto_discovered")
    assert (await db.get_anime(102))["active"] is False            # /removeanime is respected


@pytest.mark.asyncio
async def test_discovery_failure_does_not_stop_schedule_polling(cfg, db, client):
    await db.upsert_anime(21, {"title_romaji": "One Piece"})
    soon = utcnow() + timedelta(hours=1)
    al = FakeAniList(slots=[AiringSlot(21, 7, soon, "anilist")], discover_fail=True)
    stats = await monitor(cfg, db, client, al).poll_once()
    assert stats["errors"] == 1 and stats["new"] == 1 and stats["auto_discovered"] == 0
    assert await db.get_setting("heartbeat:monitor")


@pytest.mark.asyncio
async def test_discovery_can_be_disabled(cfg, db, client, monkeypatch):
    al = FakeAniList(discovered=[AnimeInfo(101, "New Show", status="RELEASING")])
    stats = await monitor(with_cfg(cfg, auto_discover_airing_anime=False), db, client, al).poll_once()
    assert al.discover_calls == [] and stats["auto_discovered"] == 0 and not await db.get_anime(101)
    from anime_soul.config import load_config
    monkeypatch.setenv("AUTO_DISCOVER_AIRING_ANIME", "false")
    monkeypatch.setenv("AUTO_DISCOVER_MAX_PAGES", "50")
    c = load_config()
    assert c.auto_discover_airing_anime is False and c.auto_discover_max_pages == 10
    monkeypatch.delenv("AUTO_DISCOVER_AIRING_ANIME")
    assert load_config().auto_discover_airing_anime is True


@pytest.mark.asyncio
async def test_sequels_still_tracked_with_discovery_on(cfg, db, client):
    seq = AnimeInfo(500, "Show S2", status="NOT_YET_RELEASED", format="TV")
    al = FakeAniList(discovered=[AnimeInfo(101, "Show", status="RELEASING")],
                     infos={101: AnimeInfo(101, "Show", status="RELEASING", sequels=[seq])})
    stats = await monitor(cfg, db, client, al).poll_once()
    assert stats["auto_discovered"] == 1 and stats["sequels"] == 1
    assert (await db.get_anime(500))["auto_added_from"] == 101


@pytest.mark.asyncio
async def test_delay_alerts_for_discovered_anime_are_grouped(cfg, db, client):
    past = utcnow() - timedelta(hours=5)
    found = [AnimeInfo(100 + i, f"Show {i}", status="RELEASING") for i in range(5)]
    slots = [AiringSlot(100 + i, 1, past, "anilist") for i in range(5)]
    await db.upsert_anime(21, {"title_romaji": "Manual"})
    slots.append(AiringSlot(21, 2, past, "anilist"))
    stats = await monitor(cfg, db, client, FakeAniList(slots=slots, discovered=found)).poll_once()
    assert stats["delayed"] == 6
    texts = [c[2] for c in client.sent("send_message") if c[1] == ADMIN]
    assert len([t for t in texts if "⏳" in t]) == 2                  # 1 for the manual show + 1 summary
    assert any("5 episode(s) of auto-discovered anime" in t for t in texts)


# ------------------------------------------- no authorized source → no upload
@pytest.mark.asyncio
async def test_discovered_episode_is_queued_but_never_uploaded_without_source(cfg, db, client, ctx):
    past = utcnow() - timedelta(minutes=10)
    al = FakeAniList(slots=[AiringSlot(101, 4, past, "anilist")],
                     discovered=[AnimeInfo(101, "New Show", status="RELEASING")])
    await monitor(cfg, db, client, al).poll_once()
    assert (await db.get_episode("anilist:101:4"))["state"] == M.AIRING
    p = Pipeline(with_cfg(cfg, dry_run=False), db, client, ctx.notifier, ctx.sources)
    for _ in range(3):
        await db.episodes.update_many({}, {"$set": {"next_attempt_at": utcnow() - timedelta(seconds=1)}})
        doc = await db.claim_next(cfg.job_lease_seconds)
        if doc:
            assert await p.process(doc) == M.AIRING
    ep = await db.get_episode("anilist:101:4")
    assert ep["state"] == M.AIRING and ep["source_status"] == M.SOURCE_NOT_CONFIGURED and not ep.get("storage")
    assert not [c for c in client.calls if c[1] in (STORAGE, POST)]
    assert not [c for c in client.calls if c[0] in ("download_media", "send_video", "send_document")]


# ----------------------------------------------------------- admin output
@pytest.mark.asyncio
async def test_anime_command_shows_ist_time_marker_and_pages(cfg, db, client, ctx):
    soon = utcnow() + timedelta(hours=2)
    found = [AnimeInfo(1000 + i, f"Airing Show With A Fairly Long Title {i:03d}", status="RELEASING")
             for i in range(200)]
    al = FakeAniList(slots=[AiringSlot(1000, 5, soon, "anilist")], discovered=found)
    await monitor(cfg, db, client, al).poll_once()
    m = FakeMessage("/anime", user_id=ADMIN)
    await admin.guard(admin.cmd_anime)(ctx, client, m)
    text = m.replies[-1][0]
    assert "200 (200 auto-discovered)" in text and "page 1/5" in text and "/anime 2" in text
    assert "🛰" in text and f"E5 {fmt_ist(soon)}" in text and len(text) <= 4000
    assert text.count("<code>") == text.count("</code>")              # never cut inside a tag
    m2 = FakeMessage("/anime 5", user_id=ADMIN)
    await admin.guard(admin.cmd_anime)(ctx, client, m2)
    assert "page 5/5" in m2.replies[-1][0]
    q = FakeMessage("/queue", user_id=ADMIN)
    await admin.guard(admin.cmd_queue)(ctx, client, q)
    assert "Upcoming (IST)" in q.replies[-1][0] and "Airing Show With A Fairly Long Title 000 E5" in q.replies[-1][0]
