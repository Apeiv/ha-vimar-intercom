"""/api/vimar_intercom/away_upload: the card's "Upload" for the away message file.
Admin only, the same rules as the options upload (save_upload), the size cap
enforced while the body is read."""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest
from harness.web import Request, load_views

from custom_components.vimar_intercom import away_config
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom.const import DOMAIN

MAX = away_config.UPLOAD_MAX


class _Body:
    """request.content: hands out the body in chunks and counts what was read."""

    def __init__(self, data: bytes, chunk=64 * 1024):
        self.data, self.chunk, self.read = data, chunk, 0

    async def iter_chunked(self, n):
        for i in range(0, len(self.data), self.chunk):
            part = self.data[i : i + self.chunk]
            self.read += len(part)
            yield part


class _Hub:
    touched = 0

    def notify(self):
        self.touched += 1


def _req(name, data=b"ID3audio", admin=True, user=True, length=None):
    r = Request(admin=admin, query={"name": name} if name is not None else {})
    if not user:
        r._user = None
    r.content = _Body(data)
    r.content_length = len(data) if length is None else length
    return r


@pytest.fixture
def env(monkeypatch, tmp_path):
    views = load_views(monkeypatch)
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "")
    entry = types.SimpleNamespace(entry_id="e1", options={"x": 1})

    def upd(e, options):
        e.options = options

    async def executor(f, *a):
        return f(*a)

    hass = types.SimpleNamespace(
        data={DOMAIN: {"e1": {"hub": _Hub()}}},
        config=types.SimpleNamespace(media_dirs={"local": str(tmp_path)}),
        config_entries=types.SimpleNamespace(
            async_update_entry=upd, async_loaded_entries=lambda domain: [entry] if domain == DOMAIN else []
        ),
        async_add_executor_job=executor,
    )
    view = views.VimarAwayUploadView(hass)
    folder = tmp_path / "citofono" / "messaggi"
    return types.SimpleNamespace(view=view, hass=hass, entry=entry, folder=folder, root=tmp_path)


def _post(env, req):
    return asyncio.run(env.view.post(req))


def test_the_view_requires_home_assistant_auth(env):
    assert env.view.requires_auth is True


@pytest.mark.parametrize("admin,user", [(False, True), (False, False)])
def test_non_admins_and_requests_without_a_user_are_refused(env, admin, user):
    req = _req("a.mp3", admin=admin, user=user)
    with pytest.raises(sys.modules["homeassistant.exceptions"].Unauthorized):
        _post(env, req)
    assert req.content.read == 0  # the body is not even read
    assert not env.folder.exists() and env.entry.options == {"x": 1}


def test_an_upload_is_saved_and_becomes_the_away_message(env):
    r = _post(env, _req("Benvenuti (1).mp3"))
    assert r.status == 200 and r.data == {"file": "Benvenuti (1).mp3"}
    saved = env.folder / "Benvenuti (1).mp3"
    assert saved.read_bytes() == b"ID3audio"
    assert env.entry.options == {"x": 1, "away_message_file": str(saved)}
    assert R.AWAY_MESSAGE_FILE == str(saved)
    assert env.hass.data[DOMAIN]["e1"]["hub"].touched == 1


def test_a_different_file_with_the_same_name_is_kept(env):
    env.folder.mkdir(parents=True)
    (env.folder / "a.mp3").write_bytes(b"old")
    r = _post(env, _req("a.mp3", b"new"))
    assert r.data == {"file": "a-2.mp3"}
    assert (env.folder / "a.mp3").read_bytes() == b"old" and (env.folder / "a-2.mp3").read_bytes() == b"new"
    assert env.entry.options["away_message_file"] == str(env.folder / "a-2.mp3")


@pytest.mark.parametrize("name", ["../../a.mp3", "..\\..\\a.mp3", "/etc/a.mp3", "C:\\x\\a.mp3", "sub/a.mp3"])
def test_a_path_in_the_name_keeps_only_the_file_name(env, name):
    r = _post(env, _req(name))
    assert r.data == {"file": "a.mp3"}
    assert os.listdir(env.folder) == ["a.mp3"]
    assert [p.name for p in env.root.rglob("a.mp3")] == ["a.mp3"]  # nothing outside the folder


@pytest.mark.parametrize(
    "name", ["", "..", "../", ".hidden.mp3", "a.txt", "a.mp3.exe", "a.mp3/", "a\x00.mp3", "a;b.mp3", "<b>.mp3", None]
)
def test_bad_names_and_extensions_are_refused(env, name):
    r = _post(env, _req(name))
    assert r.status == 400 and r.data == {"error": "upload_bad_type"}
    assert not env.folder.exists() or not os.listdir(env.folder)
    assert env.entry.options == {"x": 1}


def test_a_declared_oversize_body_is_refused_without_reading_it(env):
    req = _req("a.mp3", b"x" * 10, length=MAX + 1)
    r = _post(env, req)
    assert r.status == 413 and r.data == {"error": "upload_too_big"}
    assert req.content.read == 0


def test_an_oversize_body_is_cut_at_the_cap(env):
    """Chunked upload (no Content-Length): reading stops at the first chunk past the cap."""
    req = _req("a.mp3", b"x" * (3 * MAX), length=0)
    r = _post(env, req)
    assert r.status == 413 and r.data == {"error": "upload_too_big"}
    assert MAX < req.content.read <= MAX + req.content.chunk
    assert not env.folder.exists() and env.entry.options == {"x": 1}


def test_a_body_exactly_at_the_cap_is_accepted(env):
    assert _post(env, _req("a.wav", b"x" * MAX)).status == 200


def test_without_the_integration_loaded_it_is_503(env):
    env.hass.config_entries.async_loaded_entries = lambda domain: []  # unloaded
    r = _post(env, _req("a.mp3"))
    assert r.status == 503 and not env.folder.exists()


def test_a_disk_error_is_500_and_changes_nothing(env, monkeypatch):
    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(away_config.shutil, "copyfileobj", boom)  # save_upload writes through its own fd (#105)
    r = _post(env, _req("a.mp3"))
    assert r.status == 500 and r.data == {"error": "upload_failed"}
    assert env.entry.options == {"x": 1}


def test_the_entry_is_the_loaded_one_from_config_entries(env):
    """Home Assistant 2025.10+ lists the loaded entries itself (async_loaded_entries): the upload
    asks it for this domain's entry instead of walking hass.data."""
    asked = []
    loaded = env.hass.config_entries.async_loaded_entries
    env.hass.config_entries.async_loaded_entries = lambda domain: asked.append(domain) or loaded(domain)
    assert _post(env, _req("a.mp3")).status == 200
    assert asked == [DOMAIN]
    assert env.entry.options["away_message_file"] == str(env.folder / "a.mp3")
