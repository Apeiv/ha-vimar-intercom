"""Voce verso la targa (pacer 20 ms) e inoltro del video a ffmpeg."""
from __future__ import annotations

import asyncio
import base64
import os
import struct

import pytest

from custom_components.vimar_intercom import av_stream
from custom_components.vimar_intercom import media_handler as mh
from custom_components.vimar_intercom.srtp import SRTPContext


class _Tr:
    def __init__(self):
        self.out = []

    def sendto(self, data, addr):
        self.out.append(data)


@pytest.fixture
def audio(monkeypatch):
    ap = mh.RTPAudioProtocol()
    ap.transport = _Tr()
    ap.remote_addr = ("192.0.2.1", 4000)
    ap.tx_enabled = True
    monkeypatch.setattr(mh, "audio_proto", ap)
    monkeypatch.setattr(mh, "_silence_limit", None)  # chiamata risposta: silenzio senza limite
    return ap


def _run_tx(seconds: float):
    async def go():
        t = asyncio.create_task(mh._tx_loop())
        await asyncio.sleep(seconds)
        t.cancel()
    asyncio.run(go())


def test_blocchi_del_browser_diventano_pacchetti_da_20ms(audio):
    """La card manda 341 campioni (48 kHz / 2048): prima un pacchetto da 341 B."""
    for _ in range(3):
        mh.send_audio(b"\x10\x00" * 341)
    _run_tx(0.3)
    pkts = audio.transport.out
    assert pkts and all(len(p) == 12 + 160 for p in pkts)
    ts = [struct.unpack_from("!I", p, 4)[0] for p in pkts]
    seq = [struct.unpack_from("!H", p, 2)[0] for p in pkts]
    assert all((b - a) & 0xFFFFFFFF == 160 for a, b in zip(ts, ts[1:]))
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(seq, seq[1:]))
    assert all(p[1] == 0 for p in pkts)  # PT 0, PCMU
    # 1023 campioni: 6 pacchetti pieni di voce vera, poi silenzio di keepalive.
    assert pkts[5][12:] != mh.SILENCE_ULAW
    assert pkts[6][12:] == mh.SILENCE_ULAW


def test_senza_voce_rtp_di_silenzio(audio):
    """Come l'app ufficiale: senza voce in coda si manda comunque silenzio PCMU
    ogni 20 ms, altrimenti la targa chiude "Vedi esterno" a ~10 s (verificato
    sul campo il 2026-09-29 con l'autoaccensione a 20 s)."""
    _run_tx(0.25)
    pkts = audio.transport.out
    assert pkts and all(len(p) == 12 + 160 for p in pkts)
    assert all(p[12:] == mh.SILENCE_ULAW for p in pkts)


def test_anteprima_dello_squillo_non_trasmette(audio):
    audio.tx_enabled = False
    _run_tx(0.1)
    assert audio.transport.out == []


def test_mai_rtp_in_chiaro_dentro_una_sessione_srtp(audio):
    audio.srtp_rx = SRTPContext(base64.b64encode(os.urandom(30)).decode())
    audio.send_rtp(b"\xff" * 160)
    assert audio.transport.out == []


@pytest.mark.parametrize("direction, tx", [("sendrecv", True), ("sendonly", False), ("inactive", False)])
def test_direzione_della_targa(audio, monkeypatch, direction, tx):
    monkeypatch.setattr(mh, "video_proto", None)
    monkeypatch.setattr(mh, "_stun_task", None)
    monkeypatch.setattr(mh, "_audio_task", None)
    monkeypatch.setattr(mh, "_tx_task", None)

    async def go():
        await mh.setup_media({"conn": "192.0.2.1", "audio": {
            "port": 4000, "ip": "192.0.2.1", "fmts": ["0"], "dir": direction}, "video": {}})
        ok = audio.tx_enabled
        await mh.stop_media()
        return ok
    assert asyncio.run(go()) is tx


def test_inoltro_video_a_ffmpeg_col_pt_96(monkeypatch):
    vp = mh.RTPVideoProtocol()
    vp.remote_addr = ("192.0.2.1", 4002)
    sent = []
    vp.ffmpeg_av_sock = type("S", (), {"sendto": lambda self, d, a: sent.append(d)})()
    vp.forward_av = True
    vp.frame_sink = lambda nal: None
    pkt = struct.pack("!BBHII", 0x80, 0x80 | 99, 1, 0, 7) + b"\x41\x00"
    vp.datagram_received(pkt, None)
    assert sent[0][1] == 0x80 | 96 and sent[0][2:] == pkt[2:]


def test_flusso_ripartito_resta_continuo_per_ffmpeg():
    """La targa riparte (SSRC nuovo, seq indietro): ffmpeg deve vedere un flusso solo,
    altrimenti scarta tutto come «received too late» e /av resta fermo."""
    av = av_stream.AvRtp(3000)
    out = [av.fix(struct.pack("!BBHII", 0x80, 99, s, t, x) + b"A", 96)
           for s, t, x in ((500, 9000, 7), (501, 12000, 7), (100, 0, 8), (101, 3000, 8))]
    hdr = [struct.unpack_from("!HII", o, 2) for o in out]
    assert [h[0] for h in hdr] == [500, 501, 502, 503]
    assert [h[1] for h in hdr] == [9000, 12000, 15000, 18000]
    assert {h[2] for h in hdr} == {7} and all(o[1] == 96 for o in out)


def test_pacchetto_vecchio_ristrasmesso_non_sposta_il_flusso():
    """Visto sulla 40515: seq 103, 2, 104 con lo stesso SSRC. Il 2 è un pacchetto
    vecchio rimandato, non un riavvio: seq e timestamp verso ffmpeg restano quelli."""
    av = av_stream.AvRtp(3000)
    out = [av.fix(struct.pack("!BBHII", 0x80, 96, s, t, 7) + b"A", 96)
           for s, t in ((103, 30000), (2, 0), (104, 33000))]
    hdr = [struct.unpack_from("!HI", o, 2) for o in out]
    assert hdr == [(103, 30000), (2, 0), (104, 33000)]


def test_audio_ricevuto_con_header_extension():
    ap = mh.RTPAudioProtocol()
    ap.remote_addr = ("192.0.2.1", 4000)
    ext = b"\xbe\xde\x00\x01" + b"\x10\xaa\x00\x00"  # 1 parola di estensione
    pkt = struct.pack("!BBHII", 0x90, 0, 1, 0, 5) + ext + b"\xff" * 160
    ap.datagram_received(pkt, None)
    assert ap.audio_buffer.get_nowait() == b"\x00\x00" * 160


def test_silence_ulaw_is_shared_and_0xff():
    from custom_components.vimar_intercom import media_handler as m
    assert m.SILENCE_ULAW == b"\xff" * 160


def test_silenzio_della_vista_si_ferma_dopo_view_keepalive(audio, monkeypatch):
    monkeypatch.setattr(mh, "_silence_limit", 0.2)
    _run_tx(0.6)
    n = len(audio.transport.out)
    assert 8 <= n <= 14, n  # ~0,2 s / 20 ms, non i ~30 di 0,6 s


def test_chiamata_risposta_il_silenzio_non_si_ferma(audio):
    _run_tx(0.6)
    assert len(audio.transport.out) >= 25


def test_voce_vera_toglie_il_limite_della_vista(audio, monkeypatch):
    monkeypatch.setattr(mh, "_silence_limit", 0.1)
    mh.claim_voice()
    _run_tx(0.4)
    assert len(audio.transport.out) >= 15


def test_view_keepalive_zero_niente_silenzio_ma_la_voce_passa(audio, monkeypatch):
    monkeypatch.setattr(mh, "_silence_limit", 0)
    _run_tx(0.2)
    assert audio.transport.out == []
    mh.send_audio(b"\x10\x00" * 320)
    _run_tx(0.2)
    assert audio.transport.out and all(p[12:] != mh.SILENCE_ULAW for p in audio.transport.out[:1])


def test_view_keepalive_predefinito_locale_zero_cloud_120():
    from custom_components.vimar_intercom import runtime as R
    assert R.view_keepalive_default(True) == 0 and R.view_keepalive_default(False) == 120
