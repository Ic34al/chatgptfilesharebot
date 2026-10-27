"""FFprobe validation and FFmpeg remux/transcode (uses the real ffmpeg binary on synthetic clips)."""
import asyncio
import hashlib
import os
import shutil
from collections import namedtuple

import pytest

from anime_soul import processing as P
from tests.conftest import make_media, with_cfg

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                reason="ffmpeg/ffprobe not installed")


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.mark.asyncio
async def test_probe_reads_streams(tmp_path):
    f = make_media(tmp_path / "a.mkv", subs=True)
    info = await P.probe(str(f))
    assert info.video_codec == "h264" and info.audio_codecs == ["aac"] and info.subtitle_codecs == ["ass"]
    assert info.width == 160 and info.height == 90 and 39 <= info.duration <= 41
    assert "matroska" in info.format_name


@pytest.mark.asyncio
async def test_remux_mkv_without_subs_to_mp4_keeps_source(tmp_path, cfg):
    f = make_media(tmp_path / "a.mkv")
    before = sha(f)
    res = await P.process(str(f), str(tmp_path / "work"), cfg, base_name="Show - 01")
    assert res.action == "remux" and res.container == "mp4" and res.created
    assert res.path.endswith("Show_-_01.mp4") and os.path.isfile(res.path)
    assert (await P.probe(res.path)).is_mp4
    assert sha(f) == before                                    # source untouched
    assert not [p for p in os.listdir(tmp_path / "work") if ".part" in p]


@pytest.mark.asyncio
async def test_mkv_with_ass_subtitles_is_not_touched(tmp_path, cfg):
    f = make_media(tmp_path / "subs.mkv", subs=True)
    res = await P.process(str(f), str(tmp_path / "work"), cfg)
    assert res.action == "none" and res.path == str(f) and not res.created and res.container == "matroska"


@pytest.mark.asyncio
async def test_mp4_is_not_reencoded(tmp_path, cfg):
    f = make_media(tmp_path / "a.mp4")
    res = await P.process(str(f), str(tmp_path / "work"), cfg)
    assert res.action == "none" and res.path == str(f) and res.container == "mp4"


@pytest.mark.asyncio
async def test_copy_mode_never_modifies(tmp_path, cfg):
    f = make_media(tmp_path / "a.mkv")
    res = await P.process(str(f), str(tmp_path / "work"), with_cfg(cfg, process_mode="copy"))
    assert res.action == "none" and res.path == str(f)


@pytest.mark.asyncio
async def test_transcode_downscales_and_keeps_subtitles_in_mkv(tmp_path, cfg):
    f = make_media(tmp_path / "big.mkv", duration=5, size="320x240", subs=True)
    c = with_cfg(cfg, process_mode="transcode", max_output_height=120, ffmpeg_preset="ultrafast",
                 min_duration_seconds=1)
    res = await P.process(str(f), str(tmp_path / "work"), c)
    assert res.action == "transcode" and res.info.height == 120 and res.info.subtitle_codecs == ["ass"]
    assert res.path.endswith(".mkv")


@pytest.mark.asyncio
async def test_auto_mode_transcodes_only_when_over_max_height(tmp_path, cfg):
    f = make_media(tmp_path / "hd.mp4", duration=5, size="320x240")
    c = with_cfg(cfg, max_output_height=240, min_duration_seconds=1)
    assert (await P.process(str(f), str(tmp_path / "w"), c)).action == "none"
    c = with_cfg(cfg, max_output_height=120, min_duration_seconds=1, ffmpeg_preset="ultrafast",
                 output_container="mp4")
    res = await P.process(str(f), str(tmp_path / "w2"), c)
    assert res.action == "transcode" and res.container == "mp4" and res.info.height == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,needle", [
    ({"audio": False}, "No audio stream"),
    ({"duration": 3}, "too short"),
])
async def test_rejects_bad_media(tmp_path, cfg, kwargs, needle):
    f = make_media(tmp_path / "bad.mkv", **kwargs)
    with pytest.raises(P.MediaRejected, match=needle):
        await P.process(str(f), str(tmp_path / "work"), cfg)


@pytest.mark.asyncio
async def test_audio_optional_when_configured(tmp_path, cfg):
    f = make_media(tmp_path / "mute.mp4", audio=False)
    res = await P.process(str(f), str(tmp_path / "w"), with_cfg(cfg, require_audio=False))
    assert res.action == "none"


@pytest.mark.asyncio
async def test_rejects_garbage_and_too_large(tmp_path, cfg):
    g = tmp_path / "x.mkv"
    g.write_bytes(b"\x1a\x45\xdf\xa3" + os.urandom(4000))
    with pytest.raises(P.MediaRejected):
        await P.process(str(g), str(tmp_path / "w"), cfg)
    f = make_media(tmp_path / "a.mkv")
    with pytest.raises(P.MediaRejected, match="too large"):
        await P.process(str(f), str(tmp_path / "w"), with_cfg(cfg, max_file_size_mb=0))


@pytest.mark.asyncio
async def test_missing_ffmpeg_is_transient_unless_not_required(tmp_path, cfg, monkeypatch):
    f = make_media(tmp_path / "a.mkv")
    monkeypatch.setattr(P, "find_tool", lambda name: None)
    with pytest.raises(P.FFmpegMissing) as e:
        await P.process(str(f), str(tmp_path / "w"), cfg)
    assert e.value.transient and "FFMPEG_NOT_AVAILABLE" in str(e.value)
    res = await P.process(str(f), str(tmp_path / "w"), with_cfg(cfg, ffmpeg_required=False))
    assert res.action == "none" and res.path == str(f)
    res = await P.process(str(f), str(tmp_path / "w"), with_cfg(cfg, process_mode="off"))
    assert res.action == "none"


@pytest.mark.asyncio
async def test_disk_space_check(tmp_path, cfg, monkeypatch):
    f = make_media(tmp_path / "a.mkv")
    DU = namedtuple("DU", "total used free")
    monkeypatch.setattr(P.shutil, "disk_usage", lambda p: DU(10, 10, 1024))
    with pytest.raises(P.ProcessingError, match="Not enough disk space") as e:
        await P.process(str(f), str(tmp_path / "w"), cfg)
    assert e.value.transient


@pytest.mark.asyncio
async def test_ffmpeg_failure_cleans_partial_output(tmp_path, cfg):
    f = make_media(tmp_path / "a.mkv", duration=5)
    c = with_cfg(cfg, process_mode="transcode", ffmpeg_video_codec="no_such_codec", min_duration_seconds=1)
    with pytest.raises(P.ProcessingError) as e:
        await P.process(str(f), str(tmp_path / "w"), c)
    assert not e.value.transient
    assert os.listdir(tmp_path / "w") == [] and f.exists()


@pytest.mark.asyncio
async def test_subprocess_timeout_kills_process():
    with pytest.raises(P.ProcessingError, match="timed out"):
        await P._run(["sleep", "5"], timeout=1)


@pytest.mark.asyncio
async def test_cancel_kills_ffmpeg(tmp_path):
    task = asyncio.create_task(P._run(["sleep", "30"], timeout=60))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
