"""Validazione di camera_target nello step "settings" dell'options flow.

Stesso pattern di sga_target/picg_target: solo cifre, vuoto ammesso (vedi
config_flow._parse_actuators e i controlli isdigit() in async_step_settings).
Riusa lo stub minimo di HA di test_config_flow_picg.py.
"""
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


def _flow(cf, data: dict, options: dict | None = None):
    flow = cf.OptionsFlowHandler(_Entry(data, options))
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    return flow


def _base_entry_data() -> dict:
    return {
        "sip_user": "12345", "sip_password": "x", "sip_domain": "example.test",
        "local_proxy": "192.0.2.1", "use_local_udp": False, "gid": "101",
    }


def test_non_digit_camera_target_rejected(cf):
    flow = _flow(cf, _base_entry_data())
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1",
        "use_local_udp": False,
        "camera_target": "abc",
    }))
    assert result["type"] == "form"
    assert result["errors"]["camera_target"] == "invalid_target"


def test_empty_camera_target_accepted_and_saved(cf):
    flow = _flow(cf, _base_entry_data())
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1",
        "use_local_udp": False,
        "camera_target": "",
    }))
    assert result["type"] == "create_entry"
    assert result["data"]["camera_target"] == ""


def test_digits_camera_target_accepted_and_saved(cf):
    flow = _flow(cf, _base_entry_data())
    result = asyncio.run(flow.async_step_settings({
        "local_proxy": "192.0.2.1",
        "use_local_udp": False,
        "camera_target": "55001",
    }))
    assert result["type"] == "create_entry"
    assert result["data"]["camera_target"] == "55001"
