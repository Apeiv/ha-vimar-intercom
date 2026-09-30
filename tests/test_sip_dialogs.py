"""Dialoghi SIP senza rete: `send` finto che risponde da una tabella.

* una risposta non all'INVITE (es. il 200 del CANCEL, stesso Call-ID) poteva aprire
  la chiamata;
* un re-INVITE della chiamata in corso diventava un nuovo squillo (180 + 603);
* BYE incrociati: il nostro "Riaggancia" aspettava 5 s una risposta che non arriva più;
* UDP locale: media verso indirizzi fuori LAN, o un ACK tardivo che riapre il media.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from custom_components.vimar_intercom import media_handler as mh
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom import sip_client as sip


def _risposta(req: str, code: int, reason: str, method: str | None = None, tag="tg", body="") -> str:
    hdr = dict(line.split(": ", 1) for line in req.split("\r\n\r\n")[0].split("\r\n")[1:] if ": " in line)
    cseq = hdr["CSeq"] if method is None else f"{hdr['CSeq'].split()[0]} {method}"
    ctype = "Content-Type: application/sdp\r\n" if body else ""
    return (f"SIP/2.0 {code} {reason}\r\nVia: {hdr['Via']}\r\n"
            f"From: {hdr['From']}\r\nTo: {hdr['To']};tag={tag}\r\n"
            f"Call-ID: {hdr['Call-ID']}\r\nCSeq: {cseq}\r\n"
            f"Contact: <sip:55001@1.2.3.4>\r\n{ctype}Content-Length: {len(body)}\r\n\r\n{body}")


@pytest.fixture
def rete(monkeypatch):
    inviati, on_send, media_aperti = [], {}, []

    async def _send(msg):
        inviati.append(msg)
        method = msg.split(" ", 1)[0]
        cid = msg.split("Call-ID: ", 1)[1].split("\r\n", 1)[0]
        q = sip.pending_responses.setdefault(cid, asyncio.Queue())
        for r in on_send.get(method, lambda m: [])(msg):
            q.put_nowait(r)

    async def _nop(*a, **k):
        pass

    async def _setup_media(remote, *a, **k):
        media_aperti.append(remote)

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "broadcast", _nop)
    monkeypatch.setattr(sip, "send_keyframe_request", _nop)
    monkeypatch.setattr(mh, "setup_media", _setup_media)
    monkeypatch.setattr(mh, "stop_media", _nop)
    monkeypatch.setattr(sip, "build_sdp", lambda offer=None, reuse_keys=False: "v=0\r\nSDP-NOSTRO\r\n")
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setattr(sip, "MY_IP", "10.0.0.2")
    monkeypatch.setattr(sip, "pending_responses", {})
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    monkeypatch.setattr(R, "INTERCOM", "sip:55001@impianto.example")
    monkeypatch.setitem(sip.pending_incoming, "active", False)
    monkeypatch.setitem(sip.pending_incoming, "cid", None)
    for k in sip.call_state:
        monkeypatch.setitem(sip.call_state, k, None)
    return inviati, on_send, media_aperti


def test_il_200_del_cancel_non_e_la_risposta_all_invite(rete):
    """Stesso Call-ID: senza guardare il CSeq un 200 non-INVITE apriva la chiamata."""
    inviati, on_send, _ = rete
    on_send["INVITE"] = lambda m: [_risposta(m, 100, "Trying"), _risposta(m, 200, "OK", "CANCEL"),
                                   _risposta(m, 486, "Busy Here")]
    ok, msg = asyncio.run(sip.do_call())
    assert (ok, msg) == (False, "486 Busy Here") and not sip.in_call


def test_chiamata_normale_connette(rete):
    inviati, on_send, _ = rete
    on_send["INVITE"] = lambda m: [_risposta(m, 100, "Trying"), _risposta(m, 200, "OK")]
    ok, msg = asyncio.run(sip.do_call())
    assert ok and sip.in_call and not sip.calling
    assert sip.call_state["local_sdp"] == "v=0\r\nSDP-NOSTRO\r\n"
    sip._set_in_call(False)


SDP_FUORI_LAN = "v=0\r\nc=IN IP4 8.8.8.8\r\nm=audio 4000 RTP/AVP 0\r\nm=video 4002 RTP/AVP 96\r\n"


def test_udp_locale_200_con_media_fuori_lan_viene_chiuso(rete, monkeypatch):
    """In UDP locale un 200 si falsifica: il media (anche il nostro microfono) non va
    verso Internet. La chiamata si chiude (ACK + BYE) invece di restare mezza aperta."""
    inviati, on_send, media_aperti = rete
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    on_send["INVITE"] = lambda m: [_risposta(m, 200, "OK", body=SDP_FUORI_LAN)]
    ok, msg = asyncio.run(sip.do_call())
    assert not ok and not sip.in_call and media_aperti == []
    assert [m.split(" ", 1)[0] for m in inviati] == ["INVITE", "ACK", "BYE"]


def test_ack_arrivato_a_chiamata_chiusa_non_riapre_il_media(rete, monkeypatch):
    """Offerta nell'ACK: il task parte dopo, e un BYE nel frattempo ha chiuso la chiamata."""
    _, _, media_aperti = rete
    sdp = "v=0\r\nc=IN IP4 10.0.0.9\r\nm=audio 4000 RTP/AVP 0\r\n"
    monkeypatch.setitem(sip.call_state, "call_id", "c1")
    asyncio.run(sip._media_from_ack(sdp, "c1"))            # in_call è già False: BYE arrivato
    monkeypatch.setattr(sip, "in_call", True)
    asyncio.run(sip._media_from_ack(sdp, "altra"))         # un'altra chiamata al suo posto
    assert media_aperti == []
    asyncio.run(sip._media_from_ack(sdp, "c1"))
    assert len(media_aperti) == 1


def test_invio_fallito_toglie_la_coda(rete, monkeypatch):
    """Connessione caduta: la coda delle risposte non resta in pending_responses."""
    async def _giu(msg):
        raise ConnectionResetError("TLS chiuso")
    monkeypatch.setattr(sip, "send", _giu)
    with pytest.raises(ConnectionResetError):
        asyncio.run(sip._send_request("OPTIONS x SIP/2.0\r\nCSeq: 1 OPTIONS\r\n\r\n", "cid-1"))
    assert "cid-1" not in sip.pending_responses


def test_bye_incrociati_riaggancia_subito(rete, monkeypatch):
    """La targa chiude mentre premiamo "Riaggancia": handle_incoming_bye toglie la coda del
    dialogo e la risposta al nostro BYE non arriverà più. Prima: 5 s di attesa."""
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.call_state, "call_id", "c1")

    async def scenario():
        hang = asyncio.create_task(sip.do_hangup())
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        sip.pending_responses.pop("c1")      # quello che fa handle_incoming_bye
        await asyncio.wait_for(hang, 5)
        return time.monotonic() - t0

    assert asyncio.run(scenario()) < 1


REINVITE = (
    "INVITE sip:60902@10.0.0.2:5070;transport=tls SIP/2.0\r\n"
    "Via: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bKre\r\n"
    "From: <sip:55001@x>;tag=suo\r\n"
    "To: <sip:60902@x>;tag=nostro\r\n"
    "Call-ID: call-attiva\r\n"
    "CSeq: 7 INVITE\r\n"
    "Content-Type: application/sdp\r\n"
    "\r\n"
    "v=0\r\nc=IN IP4 5.6.7.8\r\nm=audio 4000 RTP/AVP 0\r\nm=video 4002 RTP/AVP 96\r\n"
)


def test_reinvite_della_chiamata_riceve_200_non_uno_squillo(rete, monkeypatch):
    inviati, _, _ = rete
    eventi = []

    async def _bc(tipo, *a):
        eventi.append(tipo)

    monkeypatch.setattr(sip, "broadcast", _bc)
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.call_state, "call_id", "call-attiva")
    monkeypatch.setitem(sip.call_state, "local_sdp", "v=0\r\nSDP-NOSTRO\r\n")
    monkeypatch.setitem(sip.call_state, "remote_sdp",
                        sip.parse_sdp(REINVITE.split("\r\n\r\n", 1)[1]))

    asyncio.run(sip.handle_incoming_invite(REINVITE))

    assert len(inviati) == 1
    r = inviati[0]
    assert r.startswith("SIP/2.0 200 OK") and "CSeq: 7 INVITE" in r
    assert "To: <sip:60902@x>;tag=nostro\r\n" in r  # un solo tag, quello del dialogo
    assert "SDP-NOSTRO" in r
    assert "ring" not in eventi and not sip.ringing()


def test_reinvite_di_una_chiamata_risposta_non_e_una_ritrasmissione(rete, monkeypatch):
    """Chiamata in arrivo risposta da noi: stesso Call-ID dello squillo ma CSeq
    nuovo. Prima si rimandava il vecchio 200 OK (CSeq 1): transazione mai chiusa."""
    inviati, _, _ = rete
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setitem(sip.call_state, "call_id", "call-attiva")
    monkeypatch.setitem(sip.call_state, "local_sdp", "v=0\r\nSDP-NOSTRO\r\n")
    monkeypatch.setitem(sip.pending_incoming, "cid", "call-attiva")
    monkeypatch.setitem(sip.pending_incoming, "cseq", "1 INVITE")
    monkeypatch.setitem(sip.pending_incoming, "resp", "SIP/2.0 200 OK\r\nCSeq: 1 INVITE\r\n\r\n")

    asyncio.run(sip.handle_incoming_invite(REINVITE))
    assert "CSeq: 7 INVITE" in inviati[0]


def test_update_riceve_200(rete, monkeypatch):
    monkeypatch.setattr(sip, "incoming_requests", None)
    inviati, _, _ = rete
    upd = ("UPDATE sip:60902@x SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bKu\r\n"
           "From: <sip:55001@x>;tag=suo\r\nTo: <sip:60902@x>;tag=nostro\r\n"
           "Call-ID: call-attiva\r\nCSeq: 8 UPDATE\r\nContent-Length: 0\r\n\r\n")

    async def run():
        sip.incoming_requests = asyncio.Queue()
        sip.incoming_requests.put_nowait(upd)
        task = asyncio.create_task(sip.request_processor())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    assert inviati and inviati[0].startswith("SIP/2.0 200 OK") and "CSeq: 8 UPDATE" in inviati[0]
