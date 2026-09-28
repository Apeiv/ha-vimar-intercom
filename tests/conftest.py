"""Stub minimi di Home Assistant per eseguire i test senza HA installato.
Aggiunge la radice del repo a sys.path così `custom_components.vimar_intercom` è importabile.

Marker (esclusi di default da pyproject, come `live`): `media` vuole ffmpeg/ffprobe e
aiohttp, `browser` vuole Playwright e aiohttp. Se mancano, i test si saltano da soli.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "vimar_intercom"
sys.path.insert(0, str(ROOT))


def _stub_package(name: str, path: Path) -> types.ModuleType:
    """Registra un package il cui corpo non viene mai eseguito.

    `custom_components/vimar_intercom/__init__.py` importa aiohttp, Home
    Assistant, l'hub e le view HTTP: importarlo per arrivare a un modulo puro
    tirerebbe dentro tutto il componente. Con un package fittizio il cui
    `__path__` punta alla cartella, `from custom_components.vimar_intercom
    import runtime` risolve `runtime.py` normalmente — import relativi
    compresi — senza che `__init__.py` giri mai.
    """
    module = types.ModuleType(name)
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    module.__path__ = [str(path)]
    module.__spec__ = spec
    sys.modules[name] = module
    return module


_parent = _stub_package("custom_components", COMPONENT.parent)
_parent.vimar_intercom = _stub_package("custom_components.vimar_intercom", COMPONENT)


def _mod(name: str, **attrs) -> types.ModuleType:
    m = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Any:
    """Classe jolly: accetta qualsiasi sottoclasse/attributo/chiamata."""
    def __init__(self, *a, **k): ...
    def __getattr__(self, item):
        return _Any()
    def __call__(self, *a, **k):
        return _Any()
    def __iter__(self):
        return iter(())


def _stub_ha() -> None:
    if "homeassistant" in sys.modules and not getattr(sys.modules["homeassistant"], "_is_stub", False):
        return  # HA vero installato
    ha = _mod("homeassistant", _is_stub=True)
    for sub in [
        "core", "config_entries", "const", "exceptions", "helpers", "helpers.entity", "helpers.entity_platform",
        "helpers.restore_state", "helpers.device_registry", "helpers.storage", "helpers.event", "helpers.aiohttp_client",
        "helpers.config_validation", "components", "components.http", "components.camera", "components.sensor",
        "components.binary_sensor", "components.switch", "components.button", "components.event", "components.lock",
        "components.select",
        "components.ffmpeg", "util", "util.dt",
    ]:
        m = _mod(f"homeassistant.{sub}")
        m.__getattr__ = lambda name, _m=m: _Any  # type: ignore[attr-defined]
        parent, _, child = f"homeassistant.{sub}".rpartition(".")
        setattr(sys.modules[parent], child, m)
    ha.core.HomeAssistant = _Any
    ha.core.ServiceCall = _Any
    ha.core.callback = lambda f: f
    ha.config_entries.ConfigEntry = _Any
    ha.config_entries.ConfigFlow = _Any
    ha.config_entries.OptionsFlow = _Any
    ha.const.Platform = _Any()
    # selector: il config flow ne usa classi e attributi (SelectSelectorMode.DROPDOWN)
    # al momento di costruire lo schema, non solo all'import.
    sel = _mod("homeassistant.helpers.selector",
               SelectSelector=_Any, SelectSelectorConfig=_Any, SelectSelectorMode=_Any(),
               FileSelector=_Any, FileSelectorConfig=_Any)
    ha.helpers.selector = sel
    _mod("voluptuous", Schema=_Any, Required=_Any, Optional=_Any, All=_Any, Coerce=_Any, In=_Any, Range=_Any)


_stub_ha()

try:
    import aiohttp  # noqa: F401
    REAL_AIOHTTP = True
except ImportError:  # __init__.py lo importa: basta uno stub (le view usano harness.web.WEB)
    _mod("aiohttp", web=_Any(), ClientSession=_Any)
    REAL_AIOHTTP = False

_MISSING = {
    "media": None if REAL_AIOHTTP and shutil.which("ffmpeg") and shutil.which("ffprobe")
    else "servono ffmpeg, ffprobe e aiohttp",
    "browser": None if REAL_AIOHTTP and importlib.util.find_spec("playwright")
    else "servono playwright e aiohttp",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        for marker, reason in _MISSING.items():
            if reason and marker in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture
def hub(monkeypatch):
    """Hub vero con SIP finto: registrato, a riposo; do_call riuscita e registrata in hub.chiamate."""
    from custom_components.vimar_intercom import hub as hub_mod
    sip = hub_mod.sip
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setitem(sip.pending_incoming, "active", False)
    h.chiamate = []

    async def _fake_do_call(target=None):
        h.chiamate.append(target)
        return True, "200"

    monkeypatch.setattr(sip, "do_call", _fake_do_call)
    return h
