"""docs/ENTITIES.md and ENTITIES.it.md list every entity the platforms create (by the key at
the end of the unique id) and every action in services.yaml, and nothing else, so the docs
can't fall behind the code again (#145)."""
from __future__ import annotations

import asyncio
import re
import types
from pathlib import Path

import pytest
import yaml

from custom_components.vimar_intercom import (
    binary_sensor,
    button,
    camera,
    event,
    lock,
    select,
    sensor,
    switch,
    text,
)
from custom_components.vimar_intercom.const import DOMAIN

ROOT = Path(__file__).resolve().parents[1]
DOCS = {"ENTITIES.md": ("## Entities", "## Services"), "ENTITIES.it.md": ("## Entità", "## Servizi")}
PLATFORMS = (binary_sensor, button, camera, event, lock, select, sensor, switch, text)


class _Hub:
    """Just what the platforms' constructors read: every optional entity is switched on."""

    def __init__(self):
        self.stats = {"vm_timeout_values": ["20", "30"]}
        self.state_cbs = []

    def register_state_callback(self, cb):
        self.state_cbs.append(cb)

    def unregister_state_callback(self, cb):
        if cb in self.state_cbs:
            self.state_cbs.remove(cb)


def _code_keys() -> set[str]:
    hub = _Hub()
    entry = types.SimpleNamespace(entry_id="e1", options={}, data={}, async_on_unload=lambda f: None)

    async def executor(f, *a):
        return f(*a)

    hass = types.SimpleNamespace(data={DOMAIN: {"e1": {"hub": hub}}}, async_add_executor_job=executor,
                                 config=types.SimpleNamespace(media_dirs={}))
    added = []
    for module in PLATFORMS:
        asyncio.run(module.async_setup_entry(hass, entry, lambda ents, *_: added.extend(ents)))
    return {e._attr_unique_id.removeprefix("e1_") for e in added}


def _section(name: str, start: str, end: str) -> str:
    doc = (ROOT / "docs" / name).read_text(encoding="utf-8")
    return doc[doc.index(start):doc.index(end)]


def _doc_keys(name: str) -> set[str]:
    """The `Key` column (third) of the entity table; `<...>` marks a dynamic key, left out."""
    keys = set()
    for line in _section(name, *DOCS[name]).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[2].startswith("`"):
            keys.update(k for k in re.findall(r"`([^`]+)`", cells[2]) if "<" not in k)
    return keys


def _doc_services(name: str) -> set[str]:
    section = _section(name, DOCS[name][1], "## Ev")  # Events / Eventi
    return set(re.findall(r"^\| `vimar_intercom\.(\w+)`", section, re.M))


@pytest.mark.parametrize("name", DOCS)
def test_the_entity_table_lists_every_entity_the_platforms_create(name):
    code, doc = _code_keys(), _doc_keys(name)
    assert code - doc == set(), f"entities missing from docs/{name}"
    assert doc - code == set(), f"docs/{name} lists entities the code doesn't create"


@pytest.mark.parametrize("name", DOCS)
def test_the_services_table_lists_every_action_in_services_yaml(name):
    services = yaml.safe_load(
        (ROOT / "custom_components" / "vimar_intercom" / "services.yaml").read_text(encoding="utf-8"))
    assert _doc_services(name) == set(services)
