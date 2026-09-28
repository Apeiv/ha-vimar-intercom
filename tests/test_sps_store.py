"""SPS/PPS della targa persistenti nello storage di HA: dopo un riavvio la prima /av
parte con sprop-parameter-sets nell'SDP invece di aspettare l'SPS in banda."""
from __future__ import annotations

import asyncio
import base64

from custom_components.vimar_intercom import av_stream
from custom_components.vimar_intercom import media_handler as media

SPS = bytes([0x67, 0x42, 0x80, 0x1F, 0xDA, 0x01, 0x40, 0x16, 0xE8, 0x40])
PPS = bytes([0x68, 0xCE, 0x38, 0x80])


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


def test_round_trip_e_sprop_alla_prima_av(monkeypatch, tmp_path):
    async def s():
        # Primo avvio: storage vuoto, la targa manda SPS+PPS → salvati una volta sola.
        store = FakeStore()
        vp = media.RTPVideoProtocol()
        monkeypatch.setattr(media, "video_proto", vp)
        await media.restore_sps_pps(store)
        assert vp.sps_pps() is None and store.data is None
        vp._emit_nal(SPS)
        assert store.saves == 0, "salvato con il solo SPS"
        vp._emit_nal(PPS)
        assert store.data == {"sps": b64(SPS), "pps": b64(PPS)} and store.saves == 1
        vp._emit_nal(SPS)
        vp._emit_nal(PPS)
        assert store.saves == 1, "risalvati gli stessi"

        # Riavvio di HA: protocollo nuovo, stesso storage → sprop già nella prima /av.
        store2 = FakeStore(store.data)
        vp2 = media.RTPVideoProtocol()
        monkeypatch.setattr(media, "video_proto", vp2)
        await media.restore_sps_pps(store2)
        assert vp2.sps_pps() == (SPS, PPS)
        monkeypatch.setattr(av_stream, "_AV_SDP_PATH", str(tmp_path / "av.sdp"))
        with open(av_stream._write_av_sdp()) as f:
            assert f"sprop-parameter-sets={b64(SPS)},{b64(PPS)}" in f.read()

        # La targa ne manda uno diverso: sovrascritto.
        pps2 = PPS + b"\x10"
        vp2._emit_nal(pps2)
        assert store2.data == {"sps": b64(SPS), "pps": b64(pps2)} and store2.saves == 1

    asyncio.run(s())


def test_storage_rotto_o_vuoto_non_ferma_l_avvio(monkeypatch):
    async def s():
        for data in (None, {}, {"sps": "!!", "pps": 3}, {"sps": None, "pps": None}):
            vp = media.RTPVideoProtocol()
            monkeypatch.setattr(media, "video_proto", vp)
            await media.restore_sps_pps(FakeStore(data))
            assert vp.sps_pps() is None, data

    asyncio.run(s())
