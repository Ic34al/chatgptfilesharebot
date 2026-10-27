"""Source matching, authorization, SSRF protection and file integrity."""
import hashlib
import os
import time

import pytest

from anime_soul.matcher import matches_episode, parse_release_name, title_matches
from anime_soul.netsafe import UnsafeURL, host_allowed, is_public_ip, validate_url
from anime_soul.sources import HttpFeedSource, LocalDirSource, build_adapter
from anime_soul.sources.base import SourceUnavailable
from anime_soul.validate import InvalidFile, sniff_container, validate_video

ANIME = {"_id": 21, "title_romaji": "Sousou no Frieren", "title_english": "Frieren: Beyond Journey's End",
         "synonyms": ["Frieren"], "quality": "1080p", "language": "Japanese (English Sub)", "season_number": 2}

MKV = b"\x1a\x45\xdf\xa3" + b"\x00" * 2000
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000


@pytest.mark.parametrize("name,title,ep,season,q", [
    ("[Studio] Sousou no Frieren - 05 [1080p].mkv", "Sousou no Frieren", 5, None, "1080p"),
    ("Frieren.S02E07.1080p.WEB.mkv", "Frieren", 7, 2, "1080p"),
    ("Frieren Episode 12 (720p).mp4", "Frieren", 12, None, "720p"),
])
def test_parse_release_name(name, title, ep, season, q):
    p = parse_release_name(name)
    assert (p.title, p.episode, p.season, p.quality) == (title, ep, season, q)


def test_title_matching_and_episode_rules():
    assert title_matches("Sousou no Frieren", ["Sousou no Frieren"])
    assert title_matches("frieren beyond journeys end", ["Frieren: Beyond Journey's End"])
    assert not title_matches("Dandadan", ["Sousou no Frieren"])
    assert matches_episode(ANIME, 5, title="Frieren", ep=5, quality="1080p")
    assert not matches_episode(ANIME, 5, title="Frieren", ep=6)                    # wrong episode
    assert not matches_episode(ANIME, 5, title="Frieren", ep=5, quality="720p")    # wrong quality
    assert not matches_episode(ANIME, 5, title="Frieren", ep=5, season=1)          # wrong season
    assert not matches_episode(ANIME, 5, title="Frieren", ep=5, language="Hindi")  # wrong language
    assert matches_episode(ANIME, 5, anime_id=21, ep=5)                            # exact id match
    assert not matches_episode(ANIME, 5, anime_id=22, title="Frieren", ep=5)       # id beats title


def test_unauthorized_sources_are_refused(cfg):
    with pytest.raises(ValueError):
        build_adapter({"_id": "x", "type": "http_feed", "config": {"url": "https://cdn.partner.example/f.json"},
                       "authorized": True, "license_note": ""}, cfg)
    with pytest.raises(ValueError):
        build_adapter({"_id": "x", "type": "http_feed", "config": {"url": "https://cdn.partner.example/f.json"},
                       "authorized": False, "license_note": "contract"}, cfg)
    ad = build_adapter({"_id": "x", "type": "http_feed", "config": {"url": "https://cdn.partner.example/f.json"},
                        "authorized": True, "license_note": "Partner agreement #12"}, cfg)
    assert isinstance(ad, HttpFeedSource)


@pytest.mark.parametrize("url", [
    "http://cdn.partner.example/a.mkv",          # plain http
    "https://evil.example/a.mkv",                # not allowlisted
    "https://127.0.0.1/a.mkv",                   # loopback
    "https://169.254.169.254/latest/meta-data",  # cloud metadata
    "https://user:pw@cdn.partner.example/a.mkv",  # embedded credentials
    "file:///etc/passwd",
    "https://cdn.partner.example.evil.com/a.mkv",  # suffix trick
])
def test_ssrf_and_allowlist(url):
    with pytest.raises(UnsafeURL):
        validate_url(url, ("cdn.partner.example",))


def test_allowlist_basics():
    assert validate_url("https://cdn.partner.example/a.mkv", ("cdn.partner.example",))
    assert host_allowed("x.cdn.partner.example", ("cdn.partner.example",))
    assert not is_public_ip("10.0.0.5") and not is_public_ip("::ffff:127.0.0.1") and is_public_ip("8.8.8.8")
    with pytest.raises(UnsafeURL):
        validate_url("https://cdn.partner.example/a.mkv", ())   # empty allowlist refuses everything


@pytest.mark.asyncio
async def test_http_feed_matching(cfg, monkeypatch):
    import anime_soul.sources.adapters as mod
    items = {"items": [
        {"anilist_id": 21, "episode": 4, "quality": "1080p", "url": "https://cdn.partner.example/4.mkv"},
        {"anilist_id": 21, "episode": 5, "quality": "1080p", "url": "https://evil.example/5.mkv"},
        {"anilist_id": 21, "episode": 5, "quality": "1080p", "url": "https://cdn.partner.example/5.mkv",
         "size": 999, "sha256": "ab"},
    ]}

    async def fake_fetch(*a, **kw):
        return items
    monkeypatch.setattr(mod, "fetch_json", fake_fetch)
    ad = HttpFeedSource("p", {"url": "https://cdn.partner.example/feed.json"}, cfg.allowed_source_hosts)
    item = await ad.find(ANIME, 5)
    assert item.ref == "https://cdn.partner.example/5.mkv" and item.size == 999   # evil host skipped
    assert await ad.find(ANIME, 6) is None


@pytest.mark.asyncio
async def test_http_feed_unavailable(cfg, monkeypatch):
    import anime_soul.sources.adapters as mod
    from anime_soul.netsafe import DownloadError

    async def boom(*a, **kw):
        raise DownloadError("HTTP 503")
    monkeypatch.setattr(mod, "fetch_json", boom)
    ad = HttpFeedSource("p", {"url": "https://cdn.partner.example/feed.json"}, cfg.allowed_source_hosts)
    with pytest.raises(SourceUnavailable):
        await ad.find(ANIME, 5)


@pytest.mark.asyncio
async def test_local_dir_source(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    f = lib / "[X] Sousou no Frieren - S02E05 [1080p].mkv"
    f.write_bytes(MKV)
    old = time.time() - 600
    os.utime(f, (old, old))
    (lib / "fresh - 06 [1080p].mkv").write_bytes(MKV)
    src = LocalDirSource("lib", {"path": str(lib)}, [str(tmp_path)])
    item = await src.find(ANIME, 5)
    assert item and item.kind == "local" and item.owned is False
    assert await src.find(ANIME, 6) is None
    with pytest.raises(ValueError):
        LocalDirSource("etc", {"path": "/etc"}, [str(tmp_path)])   # outside allowed roots


@pytest.mark.asyncio
async def test_local_dir_symlink_escape_ignored(tmp_path):
    outside = tmp_path.parent / f"outside_{os.getpid()}"
    outside.mkdir(exist_ok=True)
    target = outside / "Frieren - 05 [1080p].mkv"
    target.write_bytes(MKV)
    lib = tmp_path / "lib"
    lib.mkdir()
    os.symlink(target, lib / "Frieren - 05 [1080p].mkv")
    src = LocalDirSource("lib", {"path": str(lib)}, [str(tmp_path)])
    assert await src.find(ANIME, 5) is None


def test_sniff_container():
    assert sniff_container(MKV[:512]) == "matroska"
    assert sniff_container(MP4[:512]) == "mp4"
    assert sniff_container(b"<html>not a video</html>") is None


@pytest.mark.asyncio
async def test_validate_video(tmp_path, no_ffprobe):
    good = tmp_path / "a.mkv"
    good.write_bytes(MKV)
    rep = await validate_video(str(good), min_bytes=100, max_bytes=10_000)
    assert rep.container == "matroska" and rep.size == len(MKV)
    digest = hashlib.sha256(MKV).hexdigest()
    await validate_video(str(good), min_bytes=100, max_bytes=10_000, expected_size=len(MKV), expected_sha256=digest)
    with pytest.raises(InvalidFile):
        await validate_video(str(good), min_bytes=100, max_bytes=10_000, expected_sha256="00" * 32)
    with pytest.raises(InvalidFile):
        await validate_video(str(good), min_bytes=100, max_bytes=10_000, expected_size=5)
    with pytest.raises(InvalidFile):
        await validate_video(str(good), min_bytes=10_000, max_bytes=20_000)       # too small / truncated
    with pytest.raises(InvalidFile):
        await validate_video(str(good), min_bytes=1, max_bytes=100)              # too large
    bad = tmp_path / "b.mkv"
    bad.write_bytes(b"<html>" + b"x" * 2000)
    with pytest.raises(InvalidFile):
        await validate_video(str(bad), min_bytes=100, max_bytes=10_000)          # not a video
    with pytest.raises(InvalidFile):
        await validate_video(str(tmp_path / "missing.mkv"), min_bytes=1, max_bytes=10)


@pytest.mark.asyncio
async def test_validate_real_synthetic_video_with_ffprobe(tmp_path, sample_video):
    rep = await validate_video(str(sample_video), min_bytes=1000, max_bytes=50 * 1024 * 1024)
    assert rep.container == "matroska" and rep.duration and rep.duration >= 39
    fake = tmp_path / "fake.mkv"
    fake.write_bytes(MKV)                 # right magic bytes, but not a decodable video
    with pytest.raises(InvalidFile):
        await validate_video(str(fake), min_bytes=100, max_bytes=10_000)
