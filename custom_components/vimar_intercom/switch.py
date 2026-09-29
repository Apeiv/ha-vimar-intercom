"""Switch platform for Vimar Intercom — Segreteria e Non disturbare.

Comandi SIP (MESSAGE, header Panda: blue, verso l'SGA = SYSTEM.MAGIC_APT_INTERCOM,
55001 sull'impianto di riferimento, ma configurabile via options — vedi
runtime.SGA_TARGET / const.SGA_TARGET) [VERIFICATO 19/08/2026]:
  Segreteria      → VOICEMAIL;ON / VOICEMAIL;OFF
  Non disturbare  → DND;ON / DND;OFF

Lo stato è REALE: il Tab annuncia i cambi via SIP MESSAGE (sia da UI locale
che da app), quindi lo switch riflette lo stato effettivo e non è ottimistico.

Issue #9: su un impianto che non annuncia nulla (risponde 200 al comando senza
agire, o non manda mai VOICEMAIL;/DND;) lo switch mostrava per sempre l'ultimo
comando come se fosse lo stato. Ora il valore supposto dura CONFIRM_S dopo
l'invio, e intanto si chiede lo stato al citofono (GET_INIT_STATUS); se nessuno
conferma, lo switch torna "sconosciuto". Al riavvio si riprende solo uno stato
che il Tab aveva confermato, non una supposizione.
Il comando è confermato sul campo: `VOICEMAIL;ON` → 55001 accende la segreteria
sul Tab (i vecchi tentativi verso 55002 davano 200 senza effetto = target sbagliato).
"""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    SEGRETERIA_ON, SEGRETERIA_OFF,
    SEGRETERIA_HEADER_NAME, SEGRETERIA_HEADER_VALUE,
    DND_ON, DND_OFF,
)
from .device import device_info
from . import runtime as R

_LOGGER = logging.getLogger(__name__)

# Quanto resta visibile lo stato del comando appena inviato, in attesa che il Tab
# lo annunci o che la risposta a GET_INIT_STATUS lo confermi (sul 40507 l'annuncio
# arriva in meno di un secondo).
CONFIRM_S = 10.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    # Target letto da runtime (SGA_TARGET): valore da options — manuale o
    # importato da rubrica.db — con fallback al default storico in const.py.
    # runtime.configure() è già stato chiamato da async_setup_entry() prima
    # di avviare le piattaforme, quindi il valore qui è già quello corrente.
    async_add_entities([
        VimarModeSwitch(
            hub, entry.entry_id, key="segreteria", name="Segreteria",
            icon="mdi:voicemail", target=R.SGA_TARGET,
            cmd_on=SEGRETERIA_ON, cmd_off=SEGRETERIA_OFF,
            state_attr="voicemail",
            hname=SEGRETERIA_HEADER_NAME, hvalue=SEGRETERIA_HEADER_VALUE),
        VimarModeSwitch(
            hub, entry.entry_id, key="dnd", name="Non Disturbare",
            icon="mdi:bell-off", target=R.SGA_TARGET,
            cmd_on=DND_ON, cmd_off=DND_OFF, state_attr="dnd",
            hname="Panda", hvalue="blue"),
        VimarAwaySwitch(hub, entry.entry_id),
    ])


class VimarModeSwitch(SwitchEntity, RestoreEntity):
    """Switch per una modalità del Tab (segreteria / non disturbare)."""

    _attr_has_entity_name = False

    def __init__(self, hub, entry_id: str, *, key: str, name: str, icon: str,
                 target: str, cmd_on: str, cmd_off: str, state_attr: str,
                 hname: str | None, hvalue: str | None) -> None:
        self._hub = hub
        self._key = key
        self._target = target
        self._cmd_on = cmd_on
        self._cmd_off = cmd_off
        self._state_attr = state_attr
        self._hname = hname
        self._hvalue = hvalue
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = device_info(entry_id)
        self._last_result: str | None = None
        # Stato confermato dal Tab prima del riavvio (attributo stato_reale salvato).
        self._restored: bool | None = None
        # Comando appena inviato: (valore, scadenza monotonic, mode_seq all'invio).
        # mode_seq cresce a ogni annuncio VOICEMAIL;/DND; e a ogni GET_INIT_STATUS_REPLY
        # che porta dnd/voicemail: una notizia arrivata dopo l'invio è la verità.
        self._pending: tuple[bool, float, int] | None = None
        self._expire_handle: asyncio.TimerHandle | None = None

    def _real(self) -> bool | None:
        return self._hub.stats.get(self._state_attr)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        # Solo uno stato confermato: fino alla 1.0.10 si riprendeva anche l'ultimo
        # comando supposto, che così sopravviveva ai riavvii (issue #9).
        if last is not None and isinstance(last.attributes.get("stato_reale"), bool):
            self._restored = last.attributes["stato_reale"]
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self._on_state_change)
        self._cancel_expire()

    def _cancel_expire(self) -> None:
        if self._expire_handle:
            self._expire_handle.cancel()
            self._expire_handle = None

    @callback
    def _on_state_change(self) -> None:
        if self._pending is not None:
            _, _, seq_at_send = self._pending
            # Il Tab ha detto come stanno le cose dopo il comando (conferma o smentita).
            if self._real() is not None and self._hub.stats.get("mode_seq", 0) != seq_at_send:
                self._pending = None
                self._cancel_expire()
        self.async_write_ha_state()

    @callback
    def _expire(self) -> None:
        self._expire_handle = None
        if self._pending is not None:
            if self._real() is None:
                _LOGGER.warning("%s: il citofono non ha confermato il comando in %.0f s, "
                                "stato sconosciuto", self._attr_name, CONFIRM_S)
            self._pending = None
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Disponibile appena registrati: HA non lascia comandare un'entità non
        disponibile, e sugli impianti che non annunciano mai lo stato (issue #9) lo
        switch resterebbe inutilizzabile. Stato ignoto = is_on None."""
        return bool(self._hub.registered)

    @property
    def is_on(self) -> bool | None:
        if self._pending is not None and time.monotonic() < self._pending[1]:
            return self._pending[0]
        real = self._real()
        if real is not None:
            return real
        return self._restored  # None = sconosciuto

    @property
    def assumed_state(self) -> bool:
        """Vero mentre lo stato mostrato non viene dal Tab: il comando appena inviato
        in attesa di conferma, o lo stato confermato prima del riavvio. Home Assistant
        lo segnala con i due pulsanti on/off al posto dell'interruttore."""
        return self._pending is not None or self._real() is None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "target": self._target,
            "comando_on": self._cmd_on,
            "comando_off": self._cmd_off,
            # L'ultimo stato confermato dal Tab (anche prima del riavvio): è quello
            # che il riavvio successivo riprende.
            "stato_reale": self._real() if self._real() is not None else self._restored,
            "ultimo_esito": self._last_result,
            "nota": "Comando via SIP MESSAGE (Panda: blue) verso l'SGA; stato letto dagli annunci del Tab.",
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._send(self._cmd_on, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._send(self._cmd_off, False)

    async def _send(self, body: str, new_state: bool) -> None:
        # Letto PRIMA dell'invio: sul 40507 l'annuncio DND;OFF arriva mentre si aspetta
        # ancora il 200 del MESSAGE, e contato dopo sarebbe già "vecchio".
        seq = self._hub.stats.get("mode_seq", 0)
        ok, msg = await self._hub.async_send_command(
            body=body, target=self._target,
            header_name=self._hname or None, header_value=self._hvalue or None)
        self._last_result = msg
        # Fino alla 1.0.6 lo stato cambiava anche a comando fallito (404, timeout,
        # «Non registrato»): lo switch mostrava ON con la segreteria spenta, e
        # senza un annuncio del Tab a smentirlo lo stato falso sopravviveva anche
        # al riavvio (RestoreEntity). Ora solo un invio riuscito sposta lo stato
        # supposto; quello reale arriva comunque dall'annuncio VOICEMAIL;/DND;.
        if ok:
            self._pending = (new_state, time.monotonic() + CONFIRM_S, seq)
            self._cancel_expire()
            self._expire_handle = asyncio.get_running_loop().call_later(CONFIRM_S, self._expire)
            self._on_state_change()  # annuncio già arrivato durante l'invio: niente attesa
        if ok and new_state and self._key == "segreteria":
            self._hub.on_voicemail_on()
        _LOGGER.info("%s %s → ok=%s msg=%s", self._attr_name,
                     "ON" if new_state else "OFF", ok, msg)
        self.async_write_ha_state()
        if not ok:
            raise HomeAssistantError(f"{self._attr_name}: comando non riuscito ({msg})")
        # Il Tab di solito annuncia il cambio da solo; chi non lo fa può comunque
        # rispondere a GET_INIT_STATUS con dnd/voicemail.
        await self._hub.async_request_status()


class VimarAwaySwitch(SwitchEntity, RestoreEntity):
    """Messaggio di assenza di HA (opzioni away_message_*), escluso a vicenda con la
    segreteria del Tab: acceso questo, VOICEMAIL;OFF al Tab; accesa quella, questo si spegne.

    Disponibile solo se il messaggio è configurato. Di default acceso, ma senza mandare
    VOICEMAIL;OFF all'avvio: il comando parte solo su azione dell'utente."""

    _attr_has_entity_name = False
    _attr_name = "Messaggio di assenza"
    _attr_icon = "mdi:message-voice"
    _attr_should_poll = False

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_away_message"
        self._attr_device_info = device_info(entry_id)

    @property
    def available(self) -> bool:
        return R.away_message_configured()

    @property
    def is_on(self) -> bool:
        return self._hub.away_enabled

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            # Segreteria del Tab già accesa: il messaggio resta spento.
            self._hub.set_away_enabled(last.state == "on" and not self._hub.stats.get("voicemail"))
        self._hub.register_state_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_state_callback(self.async_write_ha_state)

    async def async_turn_on(self, **kwargs) -> None:
        ok, msg = await self._hub.async_send_command(
            body=SEGRETERIA_OFF, target=R.SGA_TARGET,
            header_name=SEGRETERIA_HEADER_NAME, header_value=SEGRETERIA_HEADER_VALUE)
        if not ok:
            raise HomeAssistantError(f"{self._attr_name}: segreteria del Tab non spenta ({msg})")
        self._hub.set_away_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._hub.set_away_enabled(False)
