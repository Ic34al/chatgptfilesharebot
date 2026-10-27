"""FSUB verification, actual file delivery, legacy links and admin authorization."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pyrogram import enums
from pyrogram.errors import ChatAdminRequired, FileReferenceExpired

from anime_soul import models as M
from anime_soul.delivery import deliver_episode
from anime_soul.handlers import admin, user
from anime_soul.timeutil import utcnow
from tests.conftest import ADMIN, FSUB, STORAGE, USER, FakeMessage, Media, with_cfg

TOKEN = "epTESTTOKEN12345678"


async def published_episode(db):
    await db.upsert_anime(21, {"title_romaji": "Frieren"})
    await db.upsert_schedule(21, 5, utcnow(), "anilist")
    await db.set_state("anilist:21:5", M.PUBLISHED, token=TOKEN,
                       storage={"file_id": "STOREDFILE", "chat_id": STORAGE, "message_id": 77, "file_size": 1})


# ---------------------------------------------------------------- FSUB
@pytest.mark.asyncio
async def test_fsub_member_states(ctx, client):
    S = enums.ChatMemberStatus
    for status, ok in [(S.MEMBER, True), (S.ADMINISTRATOR, True), (S.OWNER, True), (S.LEFT, False),
                       (S.BANNED, False)]:
        client.members[(FSUB, USER)] = status
        assert await ctx.fsub.is_member(FSUB, USER) is ok, status
    client.members[(FSUB, USER)] = None                     # UserNotParticipant
    assert await ctx.fsub.is_member(FSUB, USER) is False


@pytest.mark.asyncio
async def test_fsub_fails_closed_on_errors_and_alerts_admin(ctx, client):
    client.members[(FSUB, USER)] = ChatAdminRequired()
    assert await ctx.fsub.is_member(FSUB, USER) is False
    assert any("FSUB check failed" in c[2] for c in client.sent("send_message"))


@pytest.mark.asyncio
async def test_multiple_channels(ctx, client, cfg):
    other = -1003000000009
    ctx.fsub.cfg = with_cfg(cfg, fsub_channels=(FSUB, other))
    client.members[(FSUB, USER)] = enums.ChatMemberStatus.MEMBER
    missing = await ctx.fsub.missing(USER)
    assert [m.chat for m in missing] == [other]
    kb = ctx.fsub.keyboard(missing, "fsub:x")
    assert kb.inline_keyboard[-1][0].text == "🔄 TRY AGAIN" and kb.inline_keyboard[0][0].url.startswith("https://t.me/")


@pytest.mark.asyncio
async def test_join_request_ttl_and_stale_records(ctx, db, client, cfg):
    ctx.fsub.cfg = with_cfg(cfg, fsub_join_request=True, fsub_join_request_ttl_hours=1)
    await db.record_join_request(USER, FSUB)
    assert await ctx.fsub.is_member(FSUB, USER) is True       # pending request accepted while fresh
    await db.fsub_requests.update_one({"user_id": USER}, {"$set": {"at": utcnow() - timedelta(hours=2)}})
    assert await ctx.fsub.is_member(FSUB, USER) is False      # stale: denied and removed
    assert await db.get_join_request(USER, FSUB) is None
    ctx.fsub.cfg = cfg                                        # feature off => requests never count
    await db.record_join_request(USER, FSUB)
    assert await ctx.fsub.is_member(FSUB, USER) is False


@pytest.mark.asyncio
async def test_leaving_clears_join_request(ctx, db):
    await db.record_join_request(USER, FSUB)
    upd = SimpleNamespace(chat=SimpleNamespace(id=FSUB), old_chat_member=SimpleNamespace(user=SimpleNamespace(id=USER)),
                          new_chat_member=SimpleNamespace(user=SimpleNamespace(id=USER),
                                                          status=enums.ChatMemberStatus.LEFT))
    await user.member_update_handler(ctx, None, upd)
    assert await db.get_join_request(USER, FSUB) is None


# ------------------------------------------------------------ /start flow
@pytest.mark.asyncio
async def test_get_episode_blocked_until_joined_then_delivered(ctx, db, client):
    await published_episode(db)
    m = FakeMessage(f"/start {TOKEN}")
    await user.start_handler(ctx, client, m)
    text, kw = m.replies[-1]
    assert "join" in text.lower() and kw["reply_markup"].inline_keyboard[-1][0].callback_data == f"fsub:{TOKEN}"
    assert client.sent("send_cached_media") == []             # no file before verification

    # TRY AGAIN while still not joined
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    async def noop(*a, **k):
        return None
    q = SimpleNamespace(data=f"fsub:{TOKEN}", from_user=SimpleNamespace(id=USER), answer=answer,
                        message=SimpleNamespace(edit_reply_markup=noop, delete=noop))
    await user.try_again_handler(ctx, client, q)
    assert "still need to join" in answers[-1] and client.sent("send_cached_media") == []

    # join, then TRY AGAIN => actual stored file is sent to the user
    client.members[(FSUB, USER)] = enums.ChatMemberStatus.MEMBER
    await user.try_again_handler(ctx, client, q)
    sent = client.sent("send_cached_media")
    assert sent and sent[0][1] == USER and sent[0][2] == "STOREDFILE"


@pytest.mark.asyncio
async def test_start_delivers_immediately_when_subscribed(ctx, db, client):
    await published_episode(db)
    client.members[(FSUB, USER)] = enums.ChatMemberStatus.MEMBER
    await user.start_handler(ctx, client, FakeMessage(f"/start {TOKEN}"))
    assert client.sent("send_cached_media")[0][2] == "STOREDFILE"


@pytest.mark.asyncio
async def test_invalid_or_manipulated_tokens(ctx, db, client):
    await published_episode(db)
    for payload in ["epWRONGTOKEN0000000", "anilist:21:5", "{$ne:null}", "x" * 80]:
        m = FakeMessage(f"/start {payload}")
        await user.start_handler(ctx, client, m)
        assert m.replies[-1][0] == user.INVALID
    assert client.sent("send_cached_media") == []


@pytest.mark.asyncio
async def test_plain_start_keeps_original_welcome_and_records_user(ctx, db, client):
    m = FakeMessage("/start")
    await user.start_handler(ctx, client, m)
    assert m.replies[0][0] == user.WELCOME
    assert await db.users.find_one({"_id": USER})


@pytest.mark.asyncio
async def test_legacy_share_link_flow_preserved(ctx, filedb, client):
    media = Media(file_id="LEGACYFILE", file_unique_id="U1", file_size=2 * 1024 * 1024)
    up = FakeMessage("", document=media)
    await user.file_handler(ctx, client, up)
    reply = up.replies[0][0]
    assert "File saved" in reply and "https://t.me/AnimeSoulFileBot?start=" in reply
    token = reply.split("start=")[1].split()[0]
    assert len(token) <= 64
    # same file again reuses the token
    up2 = FakeMessage("", document=media)
    await user.file_handler(ctx, client, up2)
    assert token in up2.replies[0][0]
    # legacy links are not FSUB-gated by default (original behaviour)
    await user.start_handler(ctx, client, FakeMessage(f"/start {token}"))
    assert client.sent("send_cached_media")[0][2] == "LEGACYFILE"


@pytest.mark.asyncio
async def test_delivery_falls_back_to_copy(client, db):
    await published_episode(db)
    ep = await db.get_episode("anilist:21:5")
    client.fail["send_cached_media"] = FileReferenceExpired()
    await deliver_episode(client, USER, ep, {"title_romaji": "Frieren"})
    cp = client.sent("copy_message")[0]
    assert cp[1:4] == (USER, STORAGE, 77)


# -------------------------------------------------------------- admin
@pytest.mark.asyncio
async def test_admin_commands_require_admin(ctx, db, client):
    for name, fn in admin.COMMANDS.items():
        m = FakeMessage(f"/{name}", user_id=USER)
        await admin.guard(fn)(ctx, client, m)
        assert "only for ANIME SOUL administrators" in m.replies[-1][0], name
    assert not await db.get_setting("paused")


@pytest.mark.asyncio
async def test_admin_pause_resume_and_status(ctx, db, client):
    await admin.guard(admin.cmd_pause)(ctx, client, FakeMessage("/pause", user_id=ADMIN))
    assert await db.is_paused()
    m = FakeMessage("/status", user_id=ADMIN)
    await admin.guard(admin.cmd_status)(ctx, client, m)
    assert "Paused: <b>yes</b>" in m.replies[-1][0]
    await admin.guard(admin.cmd_resume)(ctx, client, FakeMessage("/resume", user_id=ADMIN))
    assert not await db.is_paused()


@pytest.mark.asyncio
async def test_admin_attach_queues_episode(ctx, db, client):
    await db.upsert_anime(21, {"title_romaji": "Frieren"})
    vid = Media(file_id="VID", file_unique_id="UV", file_size=10, file_name="e.mp4")
    reply_to = FakeMessage("", user_id=ADMIN, video=vid)
    m = FakeMessage("/attach 21 6 1080p", user_id=ADMIN, reply_to=reply_to)
    await admin.guard(admin.cmd_attach)(ctx, client, m)
    ep = await db.get_episode("anilist:21:6")
    assert ep["state"] == M.AIRING and (await db.get_inbox_file("anilist:21:6"))["file_id"] == "VID"


@pytest.mark.asyncio
async def test_admin_caption_set_validates(ctx, db, client):
    bad = FakeMessage("/captions set 21", user_id=ADMIN, reply_to=FakeMessage("{oops}"))
    await admin.guard(admin.cmd_captions)(ctx, client, bad)
    assert bad.replies[-1][0].startswith("❌") and await db.get_caption(21) is None
    good = FakeMessage("/captions set 21", user_id=ADMIN,
                       reply_to=FakeMessage("🔥 <b>{anime_title}</b> E{episode}"))
    await admin.guard(admin.cmd_captions)(ctx, client, good)
    assert await db.get_caption(21) == "🔥 <b>{anime_title}</b> E{episode}"
    assert await db.get_caption(99) is None                   # falls back to built-in default


@pytest.mark.asyncio
async def test_admin_source_requires_licence_note(ctx, db, client):
    m = FakeMessage("/sources add_feed partner https://cdn.partner.example/feed.json", user_id=ADMIN)
    await admin.guard(admin.cmd_sources)(ctx, client, m)
    assert "licence" in m.replies[-1][0] and not await db.list_sources()
    m = FakeMessage("/sources add_feed evil https://evil.example/feed.json | ok", user_id=ADMIN)
    await admin.guard(admin.cmd_sources)(ctx, client, m)
    assert m.replies[-1][0].startswith("❌") and not await db.list_sources()
    m = FakeMessage("/sources add_feed partner https://cdn.partner.example/feed.json | Partner deal #4",
                    user_id=ADMIN)
    await admin.guard(admin.cmd_sources)(ctx, client, m)
    assert (await db.list_sources())[0]["license_note"] == "Partner deal #4"


@pytest.mark.asyncio
async def test_testupload_creates_fsub_protected_link(ctx, filedb, client):
    vid = Media(file_id="SAMPLE", file_unique_id="US", file_size=10)
    reply_to = FakeMessage("", user_id=ADMIN, video=vid)
    m = FakeMessage("/testupload publish", user_id=ADMIN, reply_to=reply_to)
    await admin.guard(admin.cmd_testupload)(ctx, client, m)
    assert client.sent("copy_message")[0][1] == STORAGE
    link_reply = next(r[0] for r in m.replies if "Test link" in r[0])
    token = link_reply.split("start=")[1].split()[0]
    doc = await filedb.get_file(token)
    assert doc["fsub"] is True
    # FSUB enforced for the test link
    s = FakeMessage(f"/start {token}")
    await user.start_handler(ctx, client, s)
    assert "join" in s.replies[-1][0].lower()
