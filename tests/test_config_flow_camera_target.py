"""Validazione delle targhe nello step "settings" dell'options flow: solo id
SIP numerici (validate.sip_target), vuoto ammesso."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest


def _stub(name: str, **attrs) -> None:
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


@pytest.fixture(scope="module")
def cf():
    _stub("homeassistant.components.file_upload", process_uploaded_file=None)
    _stub("homeassistant.data_entry_flow", FlowResult=dict)
    _stub("homeassistant.helpers.selector")

    class _ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    ce = sys.modules["homeassistant.config_entries"]
    if getattr(sys.modules["homeassistant"], "_is_stub", False):
        ce.ConfigFlow = _ConfigFlow
    return pytest.importorskip("custom_components.vimar_intercom.config_flow")


class _Entry:
    def __init__(self, data: dict, options: dict | None = None):
        self.data = data
        self.options = options or {}


def _hass(**kw) -> types.SimpleNamespace:
    """hass finto: l'elenco utenti (per `allowed_users`) e ciò che il test aggiunge."""
    async def _users():
        return []
    return types.SimpleNamespace(auth=types.SimpleNamespace(async_get_users=_users), **kw)


def _flow(cf, data: dict, options: dict | None = None):
    flow = cf.OptionsFlowHandler(_Entry(data, options))
    flow.hass = _hass()
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    return flow


def _base_entry_data() -> dict:
    return {
        "sip_user": "12345", "sip_password": "x", "sip_domain": "example.test",
        "local_proxy": "192.0.2.1", "use_local_udp": False, "gid": "101",
    }


@pytest.mark.parametrize("key", ["camera_target", "internal_panel_target", "door_target"])
def test_non_digit_target_rejected(cf, key):
    flow = _flow(cf, _base_entry_data())
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1",
        "use_local_udp": False,
        key: "55001@altro",
    }))
    assert result["type"] == "form"
    assert result["errors"][key] == "invalid_target"


@pytest.mark.parametrize("target, key", [("55001@altro", "invalid_actuator_target"), ("AUTO", None), ("55002", None)])
def test_target_attuatore_come_le_targhe(cf, target, key):
    """Il target di un attuatore finisce nella request line del MESSAGE: stessa regola
    numerica di hub.sip_uri, più AUTO (la targa dell'apri-porta)."""
    flow = _flow(cf, _base_entry_data())
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": False,
        "actuators": f'[{{"name": "Luce", "msg": "L", "target": "{target}", "icon": "light"}}]',
    }))
    if key:
        assert result["type"] == "form" and result["errors"]["actuators"] == key
        assert "'55001@altro'" in result["description_placeholders"]["actuators_error"]
    else:
        assert result["type"] == "create_entry" and result["data"]["actuators"][0]["target"] == target


def test_cartella_foto_sotto_www_rifiutata(cf, tmp_path):
    """/config/www è servita su /local senza login: la foto della strada no."""
    flow = _flow(cf, _base_entry_data())

    async def _job(f, *a):
        return f(*a)

    flow.hass = _hass(
        async_add_executor_job=_job,
        config=types.SimpleNamespace(is_allowed_path=lambda p: True, path=lambda *p: str(tmp_path.joinpath(*p))))
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": False,
        "snapshot_dir": str(tmp_path / "www" / "citofono")}))
    assert result["type"] == "form" and result["errors"]["snapshot_dir"] == "path_public"


def test_messaggio_di_assenza_fuori_dalle_cartelle_lette_da_ha(cf, tmp_path):
    """Il percorso va dritto a `ffmpeg -i`: stessa regola di snapshot_dir (is_allowed_path)."""
    flow = _flow(cf, _base_entry_data())
    f = tmp_path / "messaggio.mp3"
    f.write_bytes(b"x")

    async def _job(fn, *a):
        return fn(*a)

    flow.hass = _hass(
        async_add_executor_job=_job,
        config=types.SimpleNamespace(is_allowed_path=lambda p: False, path=lambda *p: str(tmp_path.joinpath("cfg", *p))))
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1", "use_local_udp": False, "away_message_file": str(f)}))
    assert result["type"] == "form" and result["errors"]["away_message_file"] == "file_not_allowed"
