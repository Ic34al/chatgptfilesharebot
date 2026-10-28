"""Static checks of the Heroku deployment files (no Heroku access needed)."""
import json
import os
import re

import pytest

from anime_soul.config import ConfigError, load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS = ("API_ID", "API_HASH", "BOT_TOKEN", "MONGODB_URI", "OWNER_ID", "STORAGE_CHANNEL_ID", "POST_CHANNEL_ID")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def known_env_names():
    src = read("anime_soul/config.py")
    return set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"', src))


def test_procfile_process_types():
    lines = [ln for ln in read("Procfile").splitlines() if ln.strip()]
    procs = dict(ln.split(":", 1) for ln in lines)
    assert set(procs) == {"worker", "uploader"}             # no web dyno: the bot uses long polling
    assert procs["worker"].strip() == "python main.py"
    assert procs["uploader"].strip() == "python main.py --role pipeline"
    assert os.path.isfile(os.path.join(ROOT, "main.py"))


def test_app_json_buildpacks_formation_and_env():
    app = json.loads(read("app.json"))
    packs = [b["url"] for b in app["buildpacks"]]
    assert "heroku/python" in packs
    assert any("activestorage-preview" in p or "ffmpeg" in p for p in packs)   # provides ffmpeg + ffprobe
    assert app["formation"]["worker"]["quantity"] == 1
    assert app["formation"]["uploader"]["quantity"] == 0     # optional second dyno, off by default
    env = app["env"]
    for k in SECRETS:
        assert env[k].get("required") is True and not env[k].get("value"), k   # never a default secret
    assert env["DRY_RUN"]["value"] == "true"                 # safe default stays on
    unknown = set(env) - known_env_names()
    assert not unknown, f"app.json lists variables the code never reads: {unknown}"


def test_env_example_is_consistent_and_secret_free():
    text = read(".env.example")
    keys = re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.M)
    assert not set(keys) - known_env_names()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    for k in ("API_HASH", "BOT_TOKEN"):
        assert values.get(k, "") == "" or "your" in values[k].lower(), k
    assert "USER:PASSWORD@" in values["MONGODB_URI"] or not values["MONGODB_URI"]   # placeholder only
    assert values["DRY_RUN"].split("#")[0].strip() == "true"


@pytest.mark.parametrize("name", ["app.json", ".env.example", "README.md", "docs/DEPLOYMENT.md", "Procfile"])
def test_no_credentials_committed(name):
    text = read(name)
    assert not re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b", text), "looks like a real bot token"
    for m in re.finditer(r"mongodb(?:\+srv)?://([^<\s:/]+):([^<\s@]+)@", text):
        assert m.group(1).upper() in ("USER", "USERNAME") and m.group(2).upper() in ("PASSWORD", "PASS"), \
            "MongoDB URI with a real-looking password"


def test_python_version_and_pinned_requirements():
    assert re.fullmatch(r"3\.11(\.\d+)?", read(".python-version").strip())
    assert not os.path.exists(os.path.join(ROOT, "runtime.txt"))     # one way to pick Python only
    reqs = [ln.strip() for ln in read("requirements.txt").splitlines() if ln.strip() and not ln.startswith("#")]
    assert all("==" in r for r in reqs), reqs                         # reproducible builds
    names = {r.split("==")[0].lower() for r in reqs}
    assert {"pyrogram", "tgcrypto", "motor", "pymongo", "dnspython", "aiohttp", "defusedxml"} <= names
    assert "pytest" not in names                                      # test deps stay in requirements-dev


BASE = {"API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "1:x", "MONGODB_URI": "mongodb://localhost",
        "OWNER_ID": "5", "STORAGE_CHANNEL_ID": "-1001", "POST_CHANNEL_ID": "-1002"}


def set_env(monkeypatch, **kw):
    for k in known_env_names():
        monkeypatch.delenv(k, raising=False)
    for k, v in {**BASE, **kw}.items():
        monkeypatch.setenv(k, v)


def test_config_loads_with_minimum_config_vars(monkeypatch):
    set_env(monkeypatch)
    cfg = load_config()
    assert cfg.dry_run is True and cfg.schedule_post_enabled is False
    assert cfg.reconcile_max_hours == 168 and cfg.feed_alert_after_failures == 5


@pytest.mark.parametrize("override", [{"SCHEDULE_POST_TIME": "25:00"}, {"ROLE": "web"}, {"API_ID": "abc"}])
def test_bad_config_vars_fail_fast(monkeypatch, override):
    set_env(monkeypatch, **override)
    with pytest.raises(ConfigError):
        load_config()


@pytest.mark.parametrize("name", ["BOT_TOKEN", "MONGODB_URI", "STORAGE_CHANNEL_ID"])
def test_missing_required_config_var_is_reported(monkeypatch, name):
    set_env(monkeypatch, **{name: ""})
    assert name in load_config().missing_for_role()
