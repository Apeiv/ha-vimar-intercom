"""Webhook squillo (`ring_webhook_url`/`ring_end_webhook_url`): un GET a inizio squillo
(fire_ring_callbacks, lo stesso punto dell'evento doorbell) e uno a fine squillo
(`_handle_broadcast`, quando lo stato smette di essere "ringing", risposto o no).
Vuoto = niente GET; una richiesta fallita non deve mai propagare l'eccezione."""
from __future__ import annotations

import asyncio
import logging

import pytest

from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom import webhook

sip = hub_mod.sip

START_URL = "http://scrypted.local:11080/webhook/abc/token123/turnOn"
END_URL = "http://scrypted.local:11080/webhook/abc/token123/turnOff"


@pytest.fixture
def calls(monkeypatch):
    """webhook.fire finto: registra ogni URL chiamato, senza rete."""
    log: list[str] = []

    async def fake_fire(url):
        log.append(url)

    monkeypatch.setattr(webhook, "fire", fake_fire)
    monkeypatch.setattr(R, "RING_WEBHOOK_URL", START_URL)
    monkeypatch.setattr(R, "RING_END_WEBHOOK_URL", END_URL)
    return log


def test_inizio_squillo_chiama_lurl_di_start(hub, calls):
    async def _run():
        hub.fire_ring_callbacks()  # crea il task del GET: serve un loop attivo
        await asyncio.sleep(0)     # lascia girare il task fire-and-forget
    asyncio.run(_run())
    assert calls == [START_URL]


@pytest.mark.parametrize("msg_type, msg", [
    ("ring_ended", "Squillo scaduto"),
    ("call_started", "Chiamata attiva!"),
])
def test_fine_squillo_chiama_lurl_di_end(hub, calls, monkeypatch, msg_type, msg):
    # Come in produzione: sip chiude lo squillo PRIMA di diffondere l'evento
    # (ring_ended per annullato/scaduto, call_started se rispondiamo).
    hub._was_ringing = True
    monkeypatch.setitem(sip.pending_incoming, "active", False)
    asyncio.run(hub._handle_broadcast(msg_type, msg))
    assert calls == [END_URL]
    assert hub._was_ringing is False  # non riscatta al prossimo evento


def test_ring_ripetuto_durante_lo_squillo_non_riarma_lurl_di_start(hub, calls, monkeypatch):
    """item 9: un secondo "ring" (es. re-INVITE) mentre si squilla ancora non deve
    rifirmare il webhook di partenza -- prima, a una fine sola corrispondevano due inizi."""
    monkeypatch.setitem(sip.pending_incoming, "caller_uri", "sip:55001@dom")
    monkeypatch.setitem(sip.pending_incoming, "active", True)
    monkeypatch.setitem(sip.pending_incoming, "early", True)

    async def _noop():
        pass

    monkeypatch.setattr(sip, "send_keyframe_request", _noop)  # niente SIP vero qui
    asyncio.run(hub._handle_broadcast("ring", "Chiamata da: 55001"))
    asyncio.run(hub._handle_broadcast("ring", "Chiamata da: 55001"))  # re-INVITE, si squilla ancora
    assert calls == [START_URL]


def test_call_started_in_uscita_non_e_fine_squillo(hub, calls):
    """Auto-call/"Vedi esterno": call_started senza uno squillo prima, niente webhook."""
    asyncio.run(hub._handle_broadcast("call_started", "Connesso!"))
    assert calls == []


def test_url_vuoto_niente_get(hub, calls, monkeypatch):
    monkeypatch.setattr(R, "RING_WEBHOOK_URL", "")
    monkeypatch.setattr(R, "RING_END_WEBHOOK_URL", "")
    hub.fire_ring_callbacks()
    hub._was_ringing = True
    monkeypatch.setitem(sip.pending_incoming, "active", False)
    asyncio.run(hub._handle_broadcast("ring_ended", "Squillo scaduto"))
    assert calls == []


# ─── webhook.fire vero: timeout, GET, log senza il token, niente eccezioni ───

@pytest.fixture
def fake_hass(monkeypatch):
    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self):
            self.calls = []

        def get(self, url):
            self.calls.append(url)
            return _Resp()

    session = _Session()
    monkeypatch.setattr(webhook, "async_get_clientsession", lambda hass: session)
    webhook.setup(object())
    return session


def test_fire_fa_un_get_alla_url_esatta(fake_hass):
    asyncio.run(webhook.fire(START_URL + "?t=segreto"))
    assert fake_hass.calls == [START_URL + "?t=segreto"]


def test_fire_url_vuota_non_chiama(fake_hass):
    asyncio.run(webhook.fire(""))
    assert fake_hass.calls == []


def test_fire_fallita_non_solleva_e_logga_senza_token(fake_hass, monkeypatch, caplog):
    def _raise(url):
        raise TimeoutError("boom")
    monkeypatch.setattr(fake_hass, "get", _raise)
    with caplog.at_level(logging.WARNING):
        asyncio.run(webhook.fire(START_URL + "?t=segreto"))  # non deve sollevare
    assert "token123" not in caplog.text and "segreto" not in caplog.text
    assert "scrypted.local" in caplog.text


def test_safe_non_mostra_user_password_e_fire_logga_solo_il_tipo(fake_hass, monkeypatch, caplog):
    url = "http://utente:segreta@scrypted.local:11080/x?t=tok"
    assert webhook._safe(url) == "http://scrypted.local:11080"

    def _raise(u):
        raise ValueError(f"URL non valido: {u}")
    monkeypatch.setattr(fake_hass, "get", _raise)
    with caplog.at_level(logging.WARNING):
        asyncio.run(webhook.fire(url))
    assert "segreta" not in caplog.text and "tok" not in caplog.text
    assert "ValueError" in caplog.text
