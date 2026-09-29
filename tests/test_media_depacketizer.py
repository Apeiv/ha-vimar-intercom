"""Depacketizer H.264 (RTP) e codec μ-law — funzioni pure di media_handler.

Fixture RTP sintetiche: verifichiamo che single-NAL, STAP-A e FU-A vengano
riassemblati nei NAL corretti e inviati nell'ordine SPS→PPS→IDR. Nessuna
dipendenza da Home Assistant: usiamo direttamente RTPVideoProtocol e
intercettiamo i NAL prodotti tramite _queue_nal.
"""
from __future__ import annotations

import shutil
import socket
import struct
import subprocess

import pytest

from custom_components.vimar_intercom import media_handler as mh
from custom_components.vimar_intercom.media_handler import (
    RTPVideoProtocol, ulaw_decode, ulaw_encode,
)

START_CODE = b"\x00\x00\x00\x01"


@pytest.fixture(autouse=True)
def _senza_hub(monkeypatch):
    # Un hub creato da un test prima lascia il suo request_keyframe (asyncio.create_task):
    # qui non c'è un loop.
    monkeypatch.setattr(mh, "request_keyframe", None)


def _mk_proto():
    """RTPVideoProtocol che raccoglie i NAL emessi (bypassa asyncio queue/WS)."""
    p = RTPVideoProtocol.__new__(RTPVideoProtocol)
    # Stato minimo necessario a _depacketize/_emit_nal.
    p._fua_buf = bytearray()
    p._fua_started = False
    p._fua_expected_seq = None
    p._last_sps = None
    p._last_pps = None
    p.panel = None
    p._ps_by_panel = {}
    p._sps_pps_sent = False
    p._pending_idr = None
    p._drop_until_idr = False
    p._nal_count = 0
    p._nal_types = {}
    p.pkt_count = 0
    # _emit_nal fa un guard su ws_send_bytes/_nal_queue: rendili truthy.
    p._nal_queue = object()
    mh.ws_send_bytes = (lambda *_a, **_k: None)
    emitted: list[bytes] = []
    # _queue_nal è il punto unico da cui passano tutti i NAL ordinati.
    p._queue_nal = lambda nal, _out=emitted: _out.append(nal)
    return p, emitted


def _rtp(payload: bytes, seq: int) -> bytes:
    hdr = struct.pack("!BBHII", 0x80, 96, seq, 0, 0x1234)
    return hdr + payload


def test_single_nal_emitted():
    p, out = _mk_proto()
    p._sps_pps_sent = True  # consenti P-frame diretti
    nal = bytes([0x41, 0xAA, 0xBB])  # type=1 (P-frame)
    p._depacketize(nal, 10)
    assert out == [nal]


def test_stap_a_splits_sps_pps():
    p, out = _mk_proto()
    sps = bytes([0x67, 0x42, 0x80, 0x1F])  # type 7
    pps = bytes([0x68, 0xCE, 0x3C])         # type 8
    stap = bytes([0x78])  # STAP-A header (type 24)
    stap += struct.pack("!H", len(sps)) + sps
    stap += struct.pack("!H", len(pps)) + pps
    p._depacketize(stap, 20)
    # SPS+PPS devono uscire (in ordine) dopo l'aggregazione
    assert sps in out and pps in out
    assert out.index(sps) < out.index(pps)


def test_fua_reassembly():
    p, out = _mk_proto()
    p._sps_pps_sent = True  # permetti l'emissione dell'IDR ricostruito
    # IDR (type 5) frammentato in 2 pacchetti FU-A.
    # FU indicator: F|NRI da NAL orig (0x60) | type 28
    frag_payload = bytes(range(20))
    half = len(frag_payload) // 2
    # start
    pkt1 = bytes([0x7C, 0x80 | 5]) + frag_payload[:half]  # S=1, type=5
    # end
    pkt2 = bytes([0x7C, 0x40 | 5]) + frag_payload[half:]  # E=1, type=5
    p._depacketize(pkt1, 30)
    p._depacketize(pkt2, 31)
    # Un solo NAL ricostruito: header 0x65 (F|NRI 0x60 | type 5) + payload
    assert len(out) == 1
    assert out[0][0] == 0x65
    assert out[0][1:] == frag_payload


def test_ulaw_roundtrip_is_close():
    # μ-law è lossy; verifichiamo che decode(encode(x)) resti vicino a x.
    import random
    random.seed(1)
    samples = [random.randint(-20000, 20000) for _ in range(64)]
    pcm = b"".join(struct.pack("<h", s) for s in samples)
    encoded = ulaw_encode(pcm)
    decoded = ulaw_decode(encoded)
    out = [struct.unpack_from("<h", decoded, i * 2)[0] for i in range(len(samples))]
    for orig, rt in zip(samples, out):
        # Tolleranza μ-law: errore relativo entro ~la banda del segmento.
        assert abs(orig - rt) <= max(256, abs(orig) * 0.10)


def test_ulaw_decode_table_len():
    assert len(mh._ULAW_DECODE) == 256


def _video_rx():
    """RTPVideoProtocol vero: i NAL escono da frame_sink (nessun WebSocket)."""
    p = RTPVideoProtocol()
    p.remote_addr = ("192.0.2.1", 4002)  # media aperto (setup_media)
    got: list[int] = []
    p.frame_sink = lambda nal: got.append(int.from_bytes(nal[1:3], "big"))
    calls = []
    orig = p._depacketize
    p._depacketize = lambda payload, seq: (calls.append(seq), orig(payload, seq))
    return p, got, calls


def _pkt(seq: int, ssrc: int = 0x1234, nal: int = 0x41) -> bytes:
    # NAL singolo (tipo 1, o `nal`) che porta il proprio numero di sequenza
    return struct.pack("!BBHII", 0x80, 96, seq, 0, ssrc) + bytes([nal]) + seq.to_bytes(2, "big")


def test_sequenza_che_torna_indietro_non_esce_fuori_ordine():
    """Campo: «FU-A seq gap: expected 37 got 23 (gap=65522)» a inizio chiamata."""
    p, got, calls = _video_rx()
    for seq in range(20, 37):
        p.datagram_received(_pkt(seq), p.remote_addr)
    for seq in range(23, 41):          # stessi numeri di nuovo (duplicati/replay)
        p.datagram_received(_pkt(seq), p.remote_addr)
    assert got == list(range(20, 41))  # in ordine, niente duplicati
    assert len(calls) < 100            # niente giro di 65 000 sequenze


def test_nuovo_ssrc_risincronizza():
    p, got, _ = _video_rx()
    for seq in range(40000, 40010):
        p.datagram_received(_pkt(seq, 1), p.remote_addr)
    for seq in range(5, 10):
        p.datagram_received(_pkt(seq, 2), p.remote_addr)
    assert got == list(range(40000, 40010)) + list(range(5, 10))


def test_buco_nella_sequenza_si_salta():
    p, got, _ = _video_rx()
    p.datagram_received(_pkt(20), p.remote_addr)
    for seq in range(30, 40):          # 21..29 persi; IDR: i P dopo un buco si scartano
        p.datagram_received(_pkt(seq, nal=0x65), p.remote_addr)
    assert got == [20] + list(range(30, 40))[:len(got) - 1]
    assert got[-1] >= 34               # al più REORDER_BUF_SIZE pacchetti trattenuti


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg non installato")
def test_rtp_h264_vero_di_ffmpeg_con_pacchetti_scambiati_e_ripetuti(tmp_path):
    """RTP H.264 prodotto da ffmpeg (FU-A), con due pacchetti scambiati e un
    tratto ripetuto: i NAL ricostruiti devono decodificare senza errori."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(3)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=15",
         "-t", "2", "-g", "15", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "baseline",
         "-f", "rtp", "-payload_type", "96", f"rtp://127.0.0.1:{sock.getsockname()[1]}"],
        check=True)
    pkts = []
    try:
        while True:
            pkts.append(sock.recv(2000))
    except socket.timeout:
        pass
    assert len(pkts) > 30  # STAP-A (SPS+PPS), FU-A e NAL singoli
    pkts[10], pkts[11] = pkts[11], pkts[10]
    pkts[30:30] = pkts[20:27]              # replay all'indietro, come sul campo

    p = RTPVideoProtocol()
    p.remote_addr = ("192.0.2.1", 4002)
    nals: list[bytes] = []
    p.frame_sink = nals.append
    for d in pkts:
        p.datagram_received(d, p.remote_addr)
    out = tmp_path / "out.h264"
    out.write_bytes(b"".join(START_CODE + n for n in nals))
    res = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"],
                         capture_output=True, text=True)
    assert res.returncode == 0 and res.stderr == ""
    frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                            capture_output=True, text=True).stdout.strip()
    assert int(frames) == 30


def test_coda_ws_gop_corrente_e_client_lento():
    """La coda dei NAL verso il WebSocket: tiene il GOP corrente (SPS, PPS, IDR e i P
    dopo) per chi si collega a video in corso, mandato in coda a lui solo, in ordine
    coi NAL dal vivo; con un client lento (coda piena) si butta tutto e si riparte
    puliti dal prossimo IDR, senza P orfani in mezzo."""
    import asyncio

    async def s():
        p = RTPVideoProtocol()
        p._nal_queue = asyncio.Queue(maxsize=4)
        mh.ws_send_bytes = (lambda *_a, **_k: None)
        sps, pps, idr, pf = bytes([0x67, 1]), bytes([0x68, 1]), bytes([0x65, 1]), bytes([0x41, 1])
        for n in (sps, pps, idr, pf):
            p._queue_nal(n)
        assert [m[5:] for m in p._gop_msgs] == [sps, pps, idr, pf]
        sent = []

        async def send(m):
            sent.append(m[5:])
        p.replay_gop_ws(send)  # coda già piena (4): il replay non entra, ma non rompe
        assert p._nal_queue.qsize() == 4
        p._queue_nal(pf)  # coda piena con un P: si svuota, via i P fino al prossimo IDR
        assert p._nal_queue.qsize() == 0 and p._drop_until_idr and p._gop_msgs == []
        p._sps_pps_sent = True
        p._emit_nal(pf)
        assert p._nal_queue.qsize() == 0, "P orfano dopo il buco"
        p._last_sps, p._last_pps = sps, pps
        p._emit_nal(idr)  # SPS+PPS+IDR: si riparte
        assert not p._drop_until_idr and [m[5:] for m in p._gop_msgs] == [sps, pps, idr]
        p.replay_gop_ws(send)
        p.connection_made(None)  # avvia il sender sulla coda (ne fa una nuova)
        p._nal_queue.put_nowait((send, b"\x03\x00\x00\x00\x01" + pf))
        await asyncio.sleep(0.05)
        assert sent == [pf]
        p._nal_sender_task.cancel()
    asyncio.run(s())


def test_dopo_stop_media_l_rtp_in_ritardo_si_scarta_e_il_gop_parte_solo_dall_idr(monkeypatch):
    """Dal campo: dopo il nostro BYE la targa manda ancora RTP (~100 ms dal cloud). Fino a
    qui veniva depacketizzato («First video RTP» ricontato), mandato ai WS e, con un IDR,
    messo nel GOP che si rimanda alla card della chiamata dopo. Ora fra stop_media e
    setup_media si scarta tutto (video e audio); il GOP per il replay comincia solo con
    SPS→PPS→IDR, mai con un P."""
    import asyncio

    sps, pps, idr, pf = bytes([0x67, 1]), bytes([0x68, 1]), bytes([0x65, 1]), bytes([0x41, 1])

    async def s():
        vp, ap = RTPVideoProtocol(), mh.RTPAudioProtocol()
        vp._nal_queue = asyncio.Queue()
        mh.ws_send_bytes = (lambda *_a, **_k: None)
        for k, v in dict(video_proto=vp, audio_proto=ap, _stun_task=None, _audio_task=None, _tx_task=None).items():
            monkeypatch.setattr(mh, k, v)
        monkeypatch.setattr(mh.frame_grabber, "start", lambda vp: None)
        monkeypatch.setattr(mh.frame_grabber, "stop", lambda vp: None)
        sdp = {"conn": "192.0.2.1", "audio": {"port": 4000}, "video": {"port": 4002}}
        seq = 0

        def feed(*nals, ssrc=7):
            nonlocal seq
            for n in nals:
                seq += 1
                vp.datagram_received(struct.pack("!BBHII", 0x80, 96, seq, 0, ssrc) + n, vp.remote_addr)

        await mh.setup_media(sdp)
        feed(pf, pf)                         # P prima dell'IDR: non entrano nel GOP
        assert vp._gop_msgs == [] and vp.pkt_count == 2
        feed(sps, pps, idr, pf)
        assert [m[5:] for m in vp._gop_msgs] == [sps, pps, idr, pf]
        await mh.stop_media()
        assert vp.remote_addr is None and vp._gop_msgs == []
        queued = vp._nal_queue.qsize()       # i NAL della chiamata, già in coda per i WS
        feed(pf, sps, pps, idr, pf)          # in ritardo dalla targa: via
        ap.datagram_received(struct.pack("!BBHII", 0x80, 0, 1, 0, 5) + b"\xff" * 160, ap.remote_addr)
        assert vp.pkt_count == 0 and vp._gop_msgs == [] and vp._nal_queue.qsize() == queued
        assert ap.pkt_count == 0 and ap.audio_buffer.empty()
        await mh.setup_media(sdp)           # chiamata nuova: si riparte, dal suo IDR
        feed(pf, ssrc=8)
        assert vp.pkt_count == 1 and vp._gop_msgs == [] and vp._nal_queue.qsize() == queued
        feed(sps, pps, idr, ssrc=8)
        assert [m[5:] for m in vp._gop_msgs] == [sps, pps, idr]
        await mh.stop_media()
    asyncio.run(s())


def test_pacchetto_perso_niente_nal_col_buco_e_p_scartati_fino_all_idr(monkeypatch):
    """Dal campo (40515 via cloud, 2026-09-28): «FU-A seq gap: expected 91 got 92 (gap=1),
    continuing» e video smerigliato fino all'IDR dopo. Un frammento perso dentro un FU-A:
    quel NAL non esce mai; dopo una perdita i P (WS e foto) si scartano fino al prossimo
    IDR, che riparte; il keyframe si chiede subito, al più uno al secondo."""
    p, out = _mk_proto()
    p._sps_pps_sent = True
    p._keyframe_at = 0.0
    foto: list[bytes] = []
    p.frame_sink = foto.append
    asked: list[int] = []
    monkeypatch.setattr(mh, "request_keyframe", lambda: asked.append(1))
    mono = [1000.0]
    monkeypatch.setattr(mh.time, "monotonic", lambda: mono[0])
    pf = bytes([0x41, 0xAA])
    p._depacketize(pf, 89)
    p._depacketize(bytes([0x7C, 0x80 | 5]) + bytes(10), 90)   # IDR FU-A: start
    p._depacketize(bytes([0x7C, 5]) + bytes(10), 92)          # 91 perso
    p._depacketize(bytes([0x7C, 0x40 | 5]) + bytes(10), 93)   # end
    assert out == [pf] and foto == [pf], "NAL col buco emesso"
    assert asked == [1] and p._drop_until_idr
    p._depacketize(pf, 94)                                    # P che riferisce il buco
    p._depacketize(pf, 95)
    assert out == [pf] and foto == [pf]
    p._depacketize(bytes([0x7C, 0x80 | 5]) + bytes(4), 96)
    p._depacketize(bytes([0x7C, 0x40 | 5]) + bytes(4), 97)    # IDR intero: si riparte
    p._depacketize(pf, 98)
    assert [n[0] & 0x1F for n in out] == [1, 5, 1] and [n[0] & 0x1F for n in foto] == [1, 5, 1]
    assert not p._drop_until_idr
    # Seconda perdita entro 1 s: niente secondo INFO; dopo 1 s sì.
    p._depacketize(bytes([0x7C, 0x80 | 1]) + bytes(4), 99)
    p._depacketize(bytes([0x7C, 0x40 | 1]) + bytes(4), 101)   # 100 perso
    assert asked == [1]
    mono[0] += 1.0
    p._depacketize(bytes([0x7C, 0x80 | 1]) + bytes(4), 102)
    p._depacketize(bytes([0x7C, 0x40 | 1]) + bytes(4), 104)   # 103 perso
    assert asked == [1, 1] and len(out) == 3


def test_pacchetto_singolo_perso_fra_due_nal_scarta_i_p_fino_all_idr(monkeypatch):
    """Perso un pacchetto intero (NAL singolo, non FU-A): il depacketizer da solo non se
    ne accorge, ma il buco in uscita dal riordino sì: P via fino al prossimo IDR."""
    p, got, _ = _video_rx()
    asked: list[int] = []
    monkeypatch.setattr(mh, "request_keyframe", lambda: asked.append(1))
    for seq in (20, 21, 23, 24, 25, 26, 27, 28, 29):        # 22 perso
        p.datagram_received(_pkt(seq), p.remote_addr)
    assert got == [20, 21] and asked == [1]                  # i P dopo il buco: via
    p.datagram_received(_pkt(30, nal=0x65), p.remote_addr)
    p.datagram_received(_pkt(31), p.remote_addr)
    assert got == [20, 21, 30, 31]


def test_keyframe_vecchio_rimandato_non_azzera_il_gop_per_il_replay(monkeypatch):
    """La 40515 rimanda pacchetti vecchi di un keyframe (seq 103, 2, 104: stesso SSRC). Il
    GOP per il replay a ffmpeg si riempiva PRIMA del filtro di riordino: l'IDR vecchio
    azzerava `_gop`, e replay_gop mandava «IDR vecchio a pezzi + P nuovi»."""
    import asyncio

    sps, pps, idr, pf = bytes([0x67, 1]), bytes([0x68, 1]), bytes([0x65, 1]), bytes([0x41, 1])

    async def s():
        vp = RTPVideoProtocol()
        vp._nal_queue = asyncio.Queue()
        for k, v in dict(video_proto=vp, audio_proto=None, _stun_task=None, _audio_task=None, _tx_task=None).items():
            monkeypatch.setattr(mh, k, v)
        monkeypatch.setattr(mh.frame_grabber, "start", lambda vp: None)
        monkeypatch.setattr(mh.frame_grabber, "stop", lambda vp: None)
        await mh.setup_media({"conn": "192.0.2.1", "audio": {}, "video": {"port": 4002}})

        def pkt(seq, ts, nal):
            return struct.pack("!BBHII", 0x80, 96, seq, ts, 7) + nal

        for p in (pkt(1, 0, sps), pkt(2, 0, pps), pkt(3, 0, idr), pkt(4, 3000, pf), pkt(5, 6000, pf)):
            vp.datagram_received(p, vp.remote_addr)
        gop = list(vp._gop)
        assert [g[12:] for g in gop] == [sps, pps, idr, pf, pf]
        vp.datagram_received(pkt(3, 0, idr), vp.remote_addr)     # l'IDR di prima, rimandato
        vp.datagram_received(pkt(1, 0, sps), vp.remote_addr)
        assert vp._gop == gop, "il keyframe vecchio ha azzerato il GOP"
        vp.datagram_received(pkt(6, 9000, pf), vp.remote_addr)  # il flusso continua da dov'era
        assert [g[12:] for g in vp._gop] == [sps, pps, idr, pf, pf, pf]
        await mh.stop_media()
    asyncio.run(s())


def _seq_of(rtp: bytes) -> int:
    return struct.unpack_from("!H", rtp, 2)[0]


def test_the_gop_is_replayed_in_sequence_order():
    """_gop fills in arrival order; ffmpeg drops every packet older than the
    first one it sees ("RTP: dropping old packet received too late")."""
    p = RTPVideoProtocol()
    p._gop = [_pkt(seq) for seq in (101, 100, 103, 102)]
    forwarded = []
    p._forward_av = forwarded.append
    p.replay_gop()
    assert [_seq_of(r) for r in forwarded] == [100, 101, 102, 103]


def test_the_gop_order_survives_a_sequence_wrap():
    p = RTPVideoProtocol()
    p._gop = [_pkt(seq) for seq in (65535, 1, 65534, 0)]
    assert [_seq_of(r) for r in p.gop_in_sequence_order()] == [65534, 65535, 0, 1]


def test_an_empty_gop_replays_nothing():
    p = RTPVideoProtocol()
    p._gop = None
    assert p.gop_in_sequence_order() == []
