import itertools
import os
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anime_soul.config import Config  # noqa: E402
from anime_soul.context import Ctx  # noqa: E402
from anime_soul.db import AnimeDB  # noqa: E402
from anime_soul.fsub import FsubChecker  # noqa: E402
from anime_soul.sources import SourceRegistry  # noqa: E402
from anime_soul.tg import Notifier  # noqa: E402
from database import FileDB  # noqa: E402

ADMIN = 1001
USER = 2002
STORAGE = -1003000000001
POST = -1003000000002
FSUB = -1003000000003


@pytest.fixture
def cfg(tmp_path):
    return Config(api_id=1, api_hash="x", bot_token="1:x", mongodb_uri="mongodb://localhost", owner_id=ADMIN,
                  admin_ids=(ADMIN,), storage_channel_id=STORAGE, post_channel_id=POST, fsub_channels=(FSUB,),
                  download_dir=str(tmp_path / "dl"), min_file_size_mb=0, local_source_roots=(str(tmp_path),),
                  allowed_source_hosts=("cdn.partner.example",), source_check_interval_seconds=60)


@pytest_asyncio.fixture
async def mongo():
    """mongomock by default. With REAL_MONGODB_URI set, every test runs against that real
    (disposable!) server instead; the 'test' database is dropped before and after each test."""
    uri = os.getenv("REAL_MONGODB_URI")
    if not uri:
        yield AsyncMongoMockClient(tz_aware=True)
        return
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    await client.drop_database("test")
    yield client
    await client.drop_database("test")
    client.close()


@pytest_asyncio.fixture
async def db(mongo):
    d = AnimeDB(mongo["test"])
    await d.ensure_indexes()
    return d


@pytest_asyncio.fixture
async def filedb(mongo):
    f = FileDB("mongodb://x", "test")
    f.client = mongo
    f.db = mongo["test"]
    f.files = f.db["files"]
    await f.files.create_index("token", unique=True)
    return f


class Media(SimpleNamespace):
    pass


class FakeClient:
    """Records calls; mimics the subset of Pyrogram used by the app."""

    def __init__(self):
        self.calls = []
        self.fail = {}            # method -> exception (raised once) or list of exceptions
        self.members = {}         # (chat, user) -> status / exception
        self._ids = itertools.count(100)
        self.me = SimpleNamespace(id=999, username="AnimeSoulFileBot")

    def _maybe_fail(self, name):
        f = self.fail.get(name)
        if isinstance(f, list):
            if f:
                raise f.pop(0)
        elif f is not None:
            self.fail.pop(name)
            raise f

    def _msg(self, chat_id, kind=None, file_size=1234, file_name="ep.mkv"):
        mid = next(self._ids)
        media = Media(file_id=f"FID{mid}", file_unique_id=f"UID{mid}", file_size=file_size, file_name=file_name)
        msg = SimpleNamespace(id=mid, chat=SimpleNamespace(id=chat_id), video=None, document=None, animation=None)
        if kind:
            setattr(msg, kind, media)
        return msg

    async def get_me(self):
        return self.me

    async def download_media(self, ref, file_name=None, **kw):
        """Simulates downloading a Telegram file: copies self.downloads[ref] to file_name."""
        import shutil as _sh
        self.calls.append(("download_media", ref, file_name, kw))
        self._maybe_fail("download_media")
        src = getattr(self, "downloads", {}).get(ref)
        if not src:
            return None
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        _sh.copy(src, file_name)
        return file_name

    async def send_cached_media(self, chat_id, file_id, **kw):
        self.calls.append(("send_cached_media", chat_id, file_id, kw))
        self._maybe_fail("send_cached_media")
        return self._msg(chat_id, "video")

    async def send_video(self, chat_id, path, **kw):
        self.calls.append(("send_video", chat_id, path, kw))
        self._maybe_fail("send_video")
        return self._msg(chat_id, "video", os.path.getsize(path), kw.get("file_name"))

    async def send_document(self, chat_id, path, **kw):
        self.calls.append(("send_document", chat_id, path, kw))
        self._maybe_fail("send_document")
        return self._msg(chat_id, "document", os.path.getsize(path), kw.get("file_name"))

    async def send_photo(self, chat_id, photo, **kw):
        self.calls.append(("send_photo", chat_id, photo, kw))
        self._maybe_fail("send_photo")
        return self._msg(chat_id)

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send_message", chat_id, text, kw))
        self._maybe_fail("send_message")
        return self._msg(chat_id)

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        self.calls.append(("copy_message", chat_id, from_chat_id, message_id, kw))
        self._maybe_fail("copy_message")
        return self._msg(chat_id, "video")

    async def get_chat_member(self, chat, user_id):
        self.calls.append(("get_chat_member", chat, user_id))
        v = self.members.get((chat, user_id))
        if isinstance(v, Exception):
            raise v
        if v is None:
            from pyrogram.errors import UserNotParticipant
            raise UserNotParticipant()
        return SimpleNamespace(status=v, is_member=True)

    async def get_messages(self, chat_id, message_ids):
        """Storage-channel lookup used by /checkfiles. ids in self.deleted come back empty."""
        self.calls.append(("get_messages", chat_id, message_ids))
        self._maybe_fail("get_messages")
        if message_ids in getattr(self, "deleted", set()):
            return SimpleNamespace(id=message_ids, empty=True, video=None, document=None, animation=None)
        return SimpleNamespace(id=message_ids, empty=False, video=Media(file_id="F"), document=None, animation=None)

    async def get_chat(self, chat):
        return SimpleNamespace(id=chat if isinstance(chat, int) else -100555, title="ANIME SOUL", username=None)

    async def create_chat_invite_link(self, chat, **kw):
        return SimpleNamespace(invite_link="https://t.me/+invite123")

    def sent(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def ctx(cfg, db, filedb, client):
    notifier = Notifier(client, cfg.all_admin_ids, cooldown_s=0)
    return Ctx(cfg=cfg, db=db, filedb=filedb, notifier=notifier, fsub=FsubChecker(client, db, cfg, notifier),
               sources=SourceRegistry(db, cfg))


def with_cfg(cfg, **kw):
    return replace(cfg, **kw)


class FakeMessage:
    def __init__(self, text="", user_id=USER, chat_id=None, reply_to=None, **media):
        self.text = text
        self.caption = None
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id or user_id)
        self.id = 1
        self.reply_to_message = reply_to
        self.replies = []
        self.document = media.get("document")
        self.video = media.get("video")
        self.audio = media.get("audio")
        self.photo = media.get("photo")
        self.animation = media.get("animation")
        self.protect_content = False

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))
        return SimpleNamespace(edit_text=self._edit)

    async def _edit(self, text, **kw):
        self.replies.append((text, kw))


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    """A synthetic 40-second test-pattern video generated locally (no copyrighted content)."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("sample") / "sample.mkv"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=40",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=40", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(out)], check=True)
    return out


def make_media(path, *, duration=40, size="160x90", audio=True, subs=False, vcodec="libx264", fmt=None):
    """Generate a small synthetic test-pattern file with ffmpeg (no copyrighted content)."""
    import subprocess
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    if subs:
        srt = str(path) + ".srt"
        with open(srt, "w") as fh:
            fh.write("1\n00:00:01,000 --> 00:00:03,000\nHello\n")
        cmd += ["-i", srt]
    cmd += ["-map", "0:v"]
    if audio:
        cmd += ["-map", "1:a", "-c:a", "aac"]
    if subs:
        cmd += ["-map", f"{2 if audio else 1}:s", "-c:s", "ass"]
    cmd += ["-c:v", vcodec, "-preset", "ultrafast"] if vcodec == "libx264" else ["-c:v", vcodec]
    if fmt:
        cmd += ["-f", fmt]
    cmd.append(str(path))
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture
def no_ffprobe(monkeypatch):
    import anime_soul.validate as v

    async def none(path, timeout=60):
        return None
    monkeypatch.setattr(v, "ffprobe_duration", none)
