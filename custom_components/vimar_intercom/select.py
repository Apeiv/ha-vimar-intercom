"""Select platform for Vimar Intercom — ritardo della segreteria (issue #4).

Solo sugli impianti che mandano la risposta lunga di GET_INIT_STATUS (visto su un
Tab 5S Up 40515 / 2FV2): `vm_timeout` e la lista dei valori ammessi
`vm_timeout_values`. L'entità nasce quando la lista arriva, non prima: sugli
impianti con la risposta corta (40507 / 2F) non compare affatto, invece di
restare "non disponibile" per sempre.

Scrittura: SET_APT_PARAMS al PICG con `Panda: set`; il valore cambia solo con
`ERR_NONE` nella risposta (PROTOCOL §3).
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .device import device_info
from . import runtime as R

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    added = False

    @callback
    def _maybe_add() -> None:
        nonlocal added
        if added or not hub.stats.get("vm_timeout_values"):
            return
        added = True
        hub.unregister_state_callback(_maybe_add)
        async_add_entities([VimarVmTimeoutSelect(hub, entry.entry_id)])

    hub.register_state_callback(_maybe_add)
    entry.async_on_unload(lambda: hub.unregister_state_callback(_maybe_add))
    _maybe_add()


class VimarVmTimeoutSelect(SelectEntity):
    """Dopo quanto risponde la segreteria: uno dei valori che il Tab dichiara."""

    _attr_has_entity_name = False
    _attr_name = "Ritardo segreteria"
    _attr_icon = "mdi:timer-cog-outline"
    _attr_should_poll = False

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_vm_timeout"
        self._attr_device_info = device_info(entry_id)

    @property
    def options(self) -> list[str]:
        return [str(v) for v in self._hub.stats.get("vm_timeout_values") or []]

    @property
    def current_option(self) -> str | None:
        cur = self._hub.stats.get("vm_timeout")
        return str(cur) if cur is not None and str(cur) in self.options else None

    @property
    def available(self) -> bool:
        # Senza registrazione o senza indirizzo del PICG il comando non può partire.
        return bool(self._hub.registered and R.PICG_TARGET and self.options)

    async def async_added_to_hass(self) -> None:
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise HomeAssistantError(f"Valore non ammesso dal citofono: {option}")
        ok, msg = await self._hub.async_set_apt_param("vm_timeout", int(option))
        if not ok:
            raise HomeAssistantError(f"Ritardo segreteria non cambiato: {msg}")
        self.async_write_ha_state()
