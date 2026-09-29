"""Sessione video dell'hub (`hub` del conftest: SIP finto, do_call registrata).

* A fine chiamata /av finisce (EOF): go2rtc si riconnette subito e lo stream worker
  di HA dopo 10 s; ogni riconnessione faceva un auto-call (486, poi un altro).
* Durante il messaggio di assenza la card mostra "Microfono": parlare non annullava
  il messaggio, che a fine file riagganciava.
* Un INVITE a chiamata in corso faceva diventare lo stato "ringing": il tasto sotto
  il dito diventava "Rispondi", e rispondere rompeva la chiamata attiva.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.vimar_intercom import hub as hub_mod

sip = hub_mod.sip


def _apri(hub):
    async def _run():
        r = await hub.stream_opened()
        await asyncio.sleep(0)
        return r
    return asyncio.run(_run())


def _fine_chiamata(hub, monkeypatch, flag="in_call"):
    """in_call (o calling) True → False, come lo notifica sip_client."""
    monkeypatch.setattr(sip, flag, True)
    hub._on_sip_state_change()
    monkeypatch.setattr(sip, flag, False)
    hub._on_sip_state_change()


# ─── Auto-call: niente richiamata sulla riconnessione di /av ─────────────────

@pytest.mark.parametrize("flag", ["in_call", "calling"])
def test_riapertura_subito_dopo_la_fine_non_chiama(hub, monkeypatch, flag):
    """«Vedi esterno» chiuso (o rifiutato: calling → False): go2rtc riapre /av 4 ms dopo.
    Il call_ended arriva solo dopo stop_media: la pausa parte da in_call/calling → False."""
    _fine_chiamata(hub, monkeypatch, flag)
    asyncio.run(hub.stream_closed())  # /av finito: l'ultimo spettatore esce
    assert _apri(hub) is False
    assert hub.chiamate == []


def test_auto_call_fallito_e_retry_non_chiamano(hub, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(hub_mod.time, "monotonic", lambda: t[0])

    async def _occupata(target=None, silence_limit=None):
        hub.chiamate.append(target)
        return False, "486 Busy Here"  # fallito prima ancora di `calling`

    monkeypatch.setattr(sip, "do_call", _occupata)
    assert _apri(hub) is True and hub.chiamate == [None]
    for gap in (0, 10, 20, 29):  # retry di go2rtc e dello stream worker
        t[0] += gap
        asyncio.run(hub.stream_closed())
        assert _apri(hub) is False
    assert hub.chiamate == [None]


def test_dopo_la_pausa_si_richiama(hub, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(hub_mod.time, "monotonic", lambda: t[0])
    _fine_chiamata(hub, monkeypatch, "calling")
    t[0] += hub_mod.AUTO_CALL_COOLDOWN + 1
    assert _apri(hub) is True
    assert hub.chiamate == [None]


def test_registrazione_non_conta_come_fine_chiamata(hub):
    hub._on_sip_state_change()  # es. registered cambia, nessuna chiamata
    assert _apri(hub) is True and hub.chiamate == [None]


def test_fine_dello_squillo_non_fa_chiamare(hub):
    """Finita l'anteprima dello squillo /av si chiude e go2rtc lo riapre."""
    hub._stream_viewers = 1  # l'anteprima era aperta
    asyncio.run(hub._handle_broadcast("ring_ended", "Chiamata cancellata"))
    asyncio.run(hub.stream_closed())
    assert _apri(hub) is False and hub.chiamate == []


def test_squillo_senza_video_aperto_non_mette_in_pausa(hub):
    """Chi apre la camera dopo uno squillo non visto deve vedere subito."""
    asyncio.run(hub._handle_broadcast("ring_ended", "Squillo scaduto"))
    assert _apri(hub) is True and hub.chiamate == [None]


def test_in_chiamata_la_pausa_non_blocca_lo_stream(hub, monkeypatch):
    """Rispondi / Vedi esterno subito dopo: lo stream deve aspettare il video."""
    _fine_chiamata(hub, monkeypatch)
    monkeypatch.setattr(sip, "calling", True)
    assert _apri(hub) is True
    assert hub.chiamate == []


# ─── Stato, "Rispondi" e microfono ─────────────────────────────────────────

def test_stato_in_chiamata_vince_sullo_squillo(hub, monkeypatch):
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.pending_incoming, "active", True)
    assert hub.status == "in_call"


def test_rispondi_a_chiamata_in_corso_non_tocca_il_dialogo(hub, monkeypatch):
    risposte = []

    async def _answer():
        risposte.append(True)
        return True, "200"

    monkeypatch.setattr(sip, "do_answer_incoming", _answer)
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.pending_incoming, "active", True)
    ok, _ = asyncio.run(hub.async_answer())
    assert not ok and risposte == []


def test_parlare_prende_la_chiamata_del_messaggio_di_assenza(hub, monkeypatch):
    riagganci = []

    async def _hangup():
        riagganci.append(True)

    monkeypatch.setattr(hub, "async_hangup", _hangup)

    async def _run():
        async def _messaggio():  # come _away_message: suona, poi riaggancia
            await asyncio.sleep(10)
            await hub.async_hangup()
        task = hub._away_task = asyncio.create_task(_messaggio())
        hub._auto_called = True
        await asyncio.sleep(0)
        hub.claim_call()  # primo pacchetto del microfono
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and hub._away_task is None

    asyncio.run(_run())
    assert riagganci == []
    assert hub._auto_called is False  # e il video che si chiude non la riaggancia


# ─── La pausa vale per le riaperture di riflesso, non per chi apre dopo ──────

def test_apertura_dall_utente_entro_il_minuto_chiama(hub, monkeypatch):
    """Risposto dalla card (WebCodecs, niente /av), riagganciato, e la camera aperta dalla
    dashboard o da HomeKit dopo 20 s: nessuno spettatore se n'è appena andato, si chiama."""
    t = [1000.0]
    monkeypatch.setattr(hub_mod.time, "monotonic", lambda: t[0])
    _fine_chiamata(hub, monkeypatch)
    t[0] += 20
    assert _apri(hub) is True and hub.chiamate == [None]


def test_riapertura_dopo_la_finestra_breve_chiama(hub, monkeypatch):
    """L'ultimo spettatore è uscito da più di QUICK_REOPEN_S: non è go2rtc che si riconnette."""
    t = [1000.0]
    monkeypatch.setattr(hub_mod.time, "monotonic", lambda: t[0])
    _fine_chiamata(hub, monkeypatch)
    asyncio.run(hub.stream_closed())
    assert _apri(hub) is False                        # riflesso: 4 ms dopo
    asyncio.run(hub.stream_closed())
    t[0] += hub_mod.QUICK_REOPEN_S + 1
    assert _apri(hub) is True and hub.chiamate == [None]
