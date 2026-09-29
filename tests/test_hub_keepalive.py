"""Recupero della registrazione SIP (v1.0.6).

Fino alla 1.0.5 il keepalive era racchiuso in `if sip.registered:`. Persa la
registrazione, il loop girava a vuoto per sempre: in UDP locale — il default —
non esisteva nessun altro percorso di recupero, e il citofono restava
scollegato fino al riavvio di Home Assistant. Peggio, `do_register()` non
azzerava `registered` quando falliva, quindi spesso quel flag restava `True` e
Home Assistant mostrava il citofono raggiungibile mentre non lo era.
"""
from __future__ import annotations

import asyncio

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")

sip = hub_mod.sip


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    return h


@pytest.fixture
def chiamate(monkeypatch):
    """Registra cosa il tick ha provato a fare, senza toccare la rete."""
    fatte = {"register": 0, "reconnect": 0, "init_status": 0}

    async def _register():
        fatte["register"] += 1
        return fatte.get("register_ok", True)

    async def _reconnect():
        fatte["reconnect"] += 1
        return fatte.get("reconnect_ok", True)

    monkeypatch.setattr(sip, "do_register", _register)
    monkeypatch.setattr(sip, "reconnect", _reconnect)
    return fatte


def _tick(hub, fatte, monkeypatch):
    async def _init():
        fatte["init_status"] += 1
        hub._init_status_sent = True

    monkeypatch.setattr(hub, "_request_init_status", _init)
    asyncio.run(hub._keepalive_tick())


# ─── registrato: keepalive normale ───────────────────────────────────────────

def test_se_registrati_si_rinnova_e_basta(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", True, raising=False)
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["register"] == 1
    assert chiamate["reconnect"] == 0
    assert chiamate["init_status"] == 0, "lo stato iniziale c'era già: non si richiede"


# ─── non registrato: il caso che prima non esisteva ──────────────────────────

def test_se_non_registrati_si_tenta_il_recupero(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", False, raising=False)
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["reconnect"] == 1, "il loop girava a vuoto invece di riconnettersi"


def test_dopo_il_recupero_si_richiede_lo_stato(hub, chiamate, monkeypatch):
    """Mentre eravamo scollegati segreteria, DND e versione rubrica possono
    essere cambiati sul Tab: ripartire con i valori di prima è sbagliato."""
    monkeypatch.setattr(sip, "registered", False, raising=False)
    hub._init_status_sent = True

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["init_status"] == 1


def test_recupero_fallito_conta_come_fallimento(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", False, raising=False)
    chiamate["reconnect_ok"] = False
    prima = hub.stats["register_failures"]

    _tick(hub, chiamate, monkeypatch)

    assert hub.stats["register_failures"] == prima + 1
    assert chiamate["init_status"] == 0


def test_un_errore_non_ferma_il_loop(hub, monkeypatch):
    """Il tick è dentro un try: un'eccezione qui ucciderebbe il keepalive e
    riporterebbe al bug originale, solo per un'altra strada."""
    async def _esplode():
        raise RuntimeError("rete sparita")

    monkeypatch.setattr(sip, "registered", True, raising=False)
    monkeypatch.setattr(sip, "do_register", _esplode)

    asyncio.run(hub._keepalive_tick())  # non deve sollevare


# ─── registered, renewal fails: reconnect at once ────────────────────────────

def test_a_failed_renewal_reconnects_at_once(hub, chiamate, monkeypatch):
    """Waiting for the next tick left the intercom unreachable for 120 s."""
    monkeypatch.setattr(sip, "registered", True, raising=False)
    chiamate["register_ok"] = False
    hub._init_status_sent = True
    prima = hub.stats["register_failures"]

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["register"] == 1 and chiamate["reconnect"] == 1
    assert hub.stats["register_failures"] == prima + 1
    assert chiamate["init_status"] == 1, "back after an outage: ask for the state again"
    assert hub.stats["last_register_time"] is not None


def test_a_failed_renewal_and_reconnect_both_count(hub, chiamate, monkeypatch):
    monkeypatch.setattr(sip, "registered", True, raising=False)
    chiamate["register_ok"] = False
    chiamate["reconnect_ok"] = False
    prima = hub.stats["register_failures"]

    _tick(hub, chiamate, monkeypatch)

    assert chiamate["reconnect"] == 1
    assert hub.stats["register_failures"] == prima + 2
    assert chiamate["init_status"] == 0


def test_the_fast_reconnect_joins_the_running_attempt(monkeypatch):
    """The keepalive and the reader share one reconnect: never two in parallel."""
    started = []

    async def _slow():
        started.append(True)
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(sip, "_reconnect", _slow)
    monkeypatch.setattr(sip, "_reconnect_task", None)

    async def _run():
        return await asyncio.gather(sip.reconnect(), sip.reconnect())

    assert asyncio.run(_run()) == [True, True]
    assert started == [True]


# ─── the lifetime the registrar grants ───────────────────────────────────────

def _ok200(extra: str) -> str:
    return ("SIP/2.0 200 OK\r\nVia: SIP/2.0/UDP 192.0.2.5:5070;branch=z9hG4bKx\r\n"
            "From: <sip:12345@example.test>;tag=a\r\nTo: <sip:12345@example.test>;tag=b\r\n"
            "Call-ID: reg-1\r\nCSeq: 1 REGISTER\r\n" + extra + "Content-Length: 0\r\n\r\n")


@pytest.mark.parametrize("extra, expected", [
    ('Contact: <sip:12345@192.0.2.9:5070>;+sip.instance="<urn:uuid:other>";expires=30\r\n'
     'Contact: <sip:12345@192.0.2.5:5070>;+sip.instance="<urn:uuid:ours>";expires=90\r\n', 90),
    ("Expires: 60\r\n", 60),
    ("", None),
])
def test_the_granted_expiry_prefers_our_own_binding(monkeypatch, extra, expected):
    monkeypatch.setattr(sip.R, "DEVICE_UUID", "ours")
    monkeypatch.setattr(sip, "MY_IP", None)
    _, hdrs, *_ = sip._parse(_ok200(extra))
    assert sip._granted_expires(hdrs) == expected


@pytest.mark.parametrize("expires, warned", [(60, True), (3600, False)])
def test_a_short_granted_expiry_is_a_warning(monkeypatch, caplog, expires, warned):
    async def _send_request(_msg, _cid):
        return [_ok200(f"Expires: {expires}\r\n")]

    monkeypatch.setattr(sip.R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(sip, "_udp_sock", object())
    monkeypatch.setattr(sip, "_my_port", lambda: 5070)
    monkeypatch.setattr(sip, "_send_request", _send_request)
    monkeypatch.setattr(sip, "_record_bindings", lambda hdrs: None)
    monkeypatch.setattr(sip, "_set_registered", lambda v: None)
    with caplog.at_level("WARNING"):
        assert asyncio.run(sip.do_register())
    assert ("granted only" in caplog.text) is warned
