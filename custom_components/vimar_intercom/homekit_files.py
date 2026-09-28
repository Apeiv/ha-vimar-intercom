"""The HomeKit accessory's files, without importing pyhap.

HAP-python is installed only when the HomeKit option is on (see __init__), so
removing an entry must not need it.
"""
from __future__ import annotations

import os

from .const import DOMAIN


def homekit_files(hass, entry_id: str) -> tuple[str, str]:
    """(state, setup code): the entry's HomeKit files under .storage."""
    storage = hass.config.path(".storage")
    return (os.path.join(storage, f"{DOMAIN}.{entry_id}.homekit.state"),
            os.path.join(storage, f"{DOMAIN}.{entry_id}.homekit.pin"))


def remove_homekit_files(hass, entry_id: str) -> None:
    """The entry was deleted: its pairing keys and setup code go with it.
    Blocking: run it in an executor."""
    for path in homekit_files(hass, entry_id):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
