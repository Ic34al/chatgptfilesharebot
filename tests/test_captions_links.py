"""Caption rendering and deep-link generation/resolution."""
import pytest

from anime_soul import models as M
from anime_soul.captions import (DEFAULT_TEMPLATE, TemplateError, caption_values, human_size, render,
                                 validate_template, visible_length)
from anime_soul.deeplink import build_link, parse_start_payload
from anime_soul.handlers.user import resolve_token
from anime_soul.timeutil import utcnow

VALUES = {"anime_title": "Frieren", "season": 2, "episode": 5, "quality": "1080p", "language": "Japanese",
          "release_date": "26 Oct 2026", "file_size": "350 MB", "channel_name": "ANIME SOUL"}


def test_default_template_renders():
    out = render(DEFAULT_TEMPLATE, VALUES)
    assert "<b>Frieren</b>" in out and "Episode: 5" in out and "ANIME SOUL" in out


def test_user_example_template():
    tpl = ("🔥 {anime_title}\n\n📺 Season: {season}\n🎬 Episode: {episode}\n🎧 Language: {language}\n"
           "💎 Quality: {quality}\n\nThe new episode is available!")
    assert "Season: 2" in render(tpl, VALUES)


def test_dynamic_text_is_html_escaped():
    out = render(DEFAULT_TEMPLATE, {**VALUES, "anime_title": "<script>Tom & Jerry</script>"})
    assert "&lt;script&gt;Tom &amp; Jerry&lt;/script&gt;" in out


@pytest.mark.parametrize("tpl", ["{unknown}", "{anime_title.__class__}", "{episode!r}", "{episode:>99}",
                                 "<b>unclosed", "<script>x</script>", "<a href='javascript:x'>x</a>", "{", ""])
def test_bad_templates_rejected(tpl):
    with pytest.raises(TemplateError):
        validate_template(tpl)


def test_caption_length_limit_truncates_title():
    out = render("{anime_title} ep {episode}", {**VALUES, "anime_title": "A" * 3000}, limit=1024)
    assert visible_length(out) <= 1024 and out.endswith("ep 5")
    with pytest.raises(TemplateError):
        render("x" * 1100 + "{episode}", VALUES, limit=1024)


def test_caption_values_and_size():
    from anime_soul.config import Config
    vals = caption_values({"title_romaji": "Sousou", "season_number": 2}, {"episode": 3, "airing_at": utcnow(),
                                                                           "storage": {"file_size": 1536 * 1024 * 1024}},
                          Config())
    assert vals["anime_title"] == "Sousou" and vals["season"] == 2 and vals["file_size"] == "1.50 GB"
    assert human_size(None) == "N/A"


def test_tokens_and_links():
    t = M.new_episode_token()
    assert t.startswith("ep") and len(t) <= 64 and M.is_valid_token(t)
    assert len({M.new_episode_token() for _ in range(1000)}) == 1000
    assert build_link("@AnimeSoulFileBot", t) == f"https://t.me/AnimeSoulFileBot?start={t}"
    with pytest.raises(ValueError):
        build_link("bad name", t)
    with pytest.raises(ValueError):
        build_link("AnimeSoulFileBot", "../../etc")
    assert parse_start_payload(f"/start {t}") == t
    assert parse_start_payload("/start") is None
    assert parse_start_payload("/start {$ne:1}") is None
    assert parse_start_payload("/start " + "a" * 65) is None


@pytest.mark.asyncio
async def test_resolve_token_only_returns_uploaded_episodes(ctx, db):
    await db.upsert_schedule(21, 1, utcnow(), "anilist")
    await db.episodes.update_one({"_id": "anilist:21:1"}, {"$set": {"token": "epAAAAAAAAAAAAAAAA",
                                                                    "state": M.AIRING}})
    assert await resolve_token(ctx, "epAAAAAAAAAAAAAAAA") == (None, None)    # not uploaded yet
    await db.set_state("anilist:21:1", M.PUBLISHED, storage={"file_id": "F"})
    kind, doc = await resolve_token(ctx, "epAAAAAAAAAAAAAAAA")
    assert kind == "episode" and doc["_id"] == "anilist:21:1"
    assert await resolve_token(ctx, "epBBBBBBBBBBBBBBBB") == (None, None)    # unrelated/guessed token
    assert await resolve_token(ctx, {"$ne": None}) == (None, None)           # injection attempt
