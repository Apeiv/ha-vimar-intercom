"""SPS/PPS della targa persistenti nello storage di HA: dopo un riavvio la prima /av
parte con sprop-parameter-sets nell'SDP invece di aspettare l'SPS in banda."""
from __future__ import annotations

import asyncio
import base64

from custom_components.vimar_intercom import av_stream, frame_grabber
from custom_components.vimar_intercom import media_handler as media

SPS = bytes([0x67, 0x42, 0x80, 0x1F, 0xDA, 0x01, 0x40, 0x16, 0xE8, 0x40])
PPS = bytes([0x68, 0xCE, 0x38, 0x80])
IDR = bytes([0x65, 0x01, 0x02, 0x03])  # tipo 5, senza SPS/PPS davanti (come sul campo)


def b64(n: bytes) -> str:
    return base64.b64encode(n).decode()


class FakeStore:
    """homeassistant.helpers.storage.Store, solo ciò che usiamo."""

    def __init__(self, data=None):
        self.data, self.saves = data, 0

    async def async_load(self):
        return self.data

    def async_delay_save(self, data_func, delay=0):
        self.data, self.saves = data_func(), self.saves + 1


def test_round_trip_per_targa_e_sprop_alla_prima_av(monkeypatch, tmp_path):
    async def s():
        # Primo avvio: storage vuoto, la targa 55001 manda SPS+PPS → salvati una volta sola.
        store = FakeStore()
        vp = media.RTPVideoProtocol()
        monkeypatch.setattr(media, "video_proto", vp)
        await media.restore_sps_pps(store)
        vp.set_panel("55001")
        assert vp.sps_pps() is None and store.data is None
        vp._emit_nal(SPS)
        assert store.saves == 0, "salvato con il solo SPS"
        vp._emit_nal(PPS)
        assert store.data == {"panels": {"55001": {"sps": b64(SPS), "pps": b64(PPS)}}} and store.saves == 1
        vp._emit_nal(SPS)
        vp._emit_nal(PPS)
        assert store.saves == 1, "risalvati gli stessi"

        # Un'altra targa chiama: niente SPS/PPS di quella prima; i suoi vanno a parte.
        pps2 = PPS + b""
        vp.set_panel("55002")
        assert vp.sps_pps() == (SPS, PPS)
        vp._emit_nal(SPS)
        vp._emit_nal(pps2)
        assert store.saves == 2 and store.data["panels"]["55002"]["pps"] == b64(pps2)
        vp.set_panel("55001")
        assert vp.sps_pps() == (SPS, PPS)

        # Riavvio di HA: protocollo nuovo, stesso storage → sprop già nella prima /av.
        store2 = FakeStore(store.data)
        vp2 = media.RTPVideoProtocol()
        monkeypatch.setattr(media, "video_proto", vp2)
        await media.restore_sps_pps(store2)
        vp2.set_panel("55001")
        assert vp2.sps_pps() == (SPS, PPS)
        with open(av_stream._write_av_sdp()) as f:
            assert f"sprop-parameter-sets={b64(SPS)},{b64(PPS)}" in f.read()
        vp2.set_panel("55002")
        assert vp2.sps_pps() == (SPS, pps2)

        # La targa ne manda uno diverso: sovrascritto solo il suo.
        pps3 = PPS + b" "
        vp2._emit_nal(pps3)
        assert store2.data["panels"] == {"55001": {"sps": b64(SPS), "pps": b64(PPS)},
                                         "55002": {"sps": b64(SPS), "pps": b64(pps3)}} and store2.saves == 1

    asyncio.run(s())


def _finto_ffmpeg_cattura_stdin(monkeypatch):
    """create_subprocess_exec finto che registra i byte scritti su stdin del grabber:
    basta a vedere se un NAL è stato messo in coda o scartato, senza ffmpeg vero.
    stdout non finisce mai da solo (altrimenti _grab esce e annulla il feeder subito,
    prima che scriva niente): lo stop() del test ferma tutto col cancel del task."""
    written = bytearray()

    class _Pipe:
        async def read(self, n):
            await asyncio.sleep(0.05)
            return b"\x00"

        def write(self, b):
            written.extend(b)

        async def drain(self):
            pass

        def close(self):
            pass

    class _Proc:
        returncode = 0

        def __init__(self):
            self.stdin, self.stdout = _Pipe(), _Pipe()

    async def _exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(frame_grabber.asyncio, "create_subprocess_exec", _exec)
    return written


def test_ring_2040_idr_senza_sps_pps_usa_la_cache_di_un_altra_targa(monkeypatch):
    """Nota ring-2040 (squillo reale del 2026-09-28): primo IDR 1,2 s dopo lo squillo,
    SENZA SPS/PPS in banda (arrivano solo 4 s dopo, come sulla 40515). La targa (60002)
    non aveva mai chiamato da quando lo storage era stato migrato al formato per targa
    (787bb87): nello storage c'era ancora la coppia vecchia (formato "piatto", quello che
    il codice scriveva prima di 787bb87). Senza un fallback la foto falliva pur con video
    che arrivava; con la coppia migrata l'IDR iniziale si mette subito in coda."""
    async def s():
        store = FakeStore({"sps": b64(SPS), "pps": b64(PPS)})  # formato vecchio, pre-787bb87
        vp = media.RTPVideoProtocol()
        monkeypatch.setattr(media, "video_proto", vp)
        await media.restore_sps_pps(store)

        vp.set_panel("60002")  # mai vista prima: niente di sua, solo il fallback migrato
        assert vp.sps_pps() == (SPS, PPS)

        written = _finto_ffmpeg_cattura_stdin(monkeypatch)
        frame_grabber.start(vp)
        await asyncio.sleep(0.05)
        assert bytes(written).count(PPS) == 1, "la coppia migrata va in coda subito, all'avvio"

        vp._emit_nal(IDR)  # campo: nessun SPS/PPS davanti
        await asyncio.sleep(0.05)
        assert IDR in bytes(written), "col fallback l'IDR iniziale non va scartato"

        # SPS/PPS in banda 4 s dopo: la cache si aggiorna solo per questa targa, la
        # coppia migrata resta buona per la prossima targa mai vista.
        SPS2, PPS2 = SPS + b"\x10", PPS + b"\x20"
        vp._emit_nal(SPS2)
        vp._emit_nal(PPS2)
        assert vp._ps_by_panel["60002"] == (SPS2, PPS2)
        assert vp._ps_by_panel[""] == (SPS, PPS)

        frame_grabber.stop(vp)

    asyncio.run(s())


def test_storage_rotto_o_vuoto_non_ferma_l_avvio(monkeypatch):
    async def s():
        for data in (None, {}, {"sps": "!!", "pps": 3}, {"panels": "x"}, {"panels": {"1": {"sps": "!!"}}}):
            vp = media.RTPVideoProtocol()
            monkeypatch.setattr(media, "video_proto", vp)
            await media.restore_sps_pps(FakeStore(data))
            vp.set_panel("1")
            assert vp.sps_pps() is None, data

    asyncio.run(s())


def test_clip_non_usa_gli_sps_pps_di_un_altra_targa(monkeypatch, tmp_path):
    """Recensione PR #30: con la coppia di un'altra targa (risoluzione diversa) il clip
    finiva con avcC sbagliato per tutta la durata. Il clip aspetta quelli in banda; la
    foto invece parte subito col fallback (sps_pps() senza own_only)."""
    async def s():
        vp = media.RTPVideoProtocol()
        monkeypatch.setattr(media, "video_proto", vp)
        vp._ps_by_panel = {"55001": (SPS, PPS)}
        vp.set_panel("60002")
        assert vp.sps_pps() == (SPS, PPS) and vp.sps_pps(own_only=True) is None

        clip = bytearray()

        class _Stdin:
            def write(self, b):
                clip.extend(b)

            async def drain(self):
                pass

            def close(self):
                pass

        class _Proc:
            returncode = 0
            stdin = _Stdin()

            async def wait(self):
                return 0

        async def _exec(*a, **k):
            return _Proc()

        monkeypatch.setattr(frame_grabber.asyncio, "create_subprocess_exec", _exec)
        frame_grabber._proto = vp
        frame_grabber._clip_req = (str(tmp_path / "c.mp4"), 60, lambda p: None)
        frame_grabber._start_clip()
        q = frame_grabber._clip_q
        q.put_nowait(IDR)
        await asyncio.sleep(0.05)
        assert not clip, "IDR senza SPS/PPS della targa: il clip non parte"
        SPS2, PPS2 = SPS + b"\x10", PPS + b"\x20"
        for n in (SPS2, PPS2, IDR):
            q.put_nowait(n)
        await asyncio.sleep(0.05)
        assert bytes(clip).startswith(b"\x00\x00\x00\x01" + SPS2)
        frame_grabber._end_clip()
        await asyncio.sleep(0.05)

    asyncio.run(s())
