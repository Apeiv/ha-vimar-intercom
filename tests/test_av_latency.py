"""Latenza di /av: dal primo IDR della targa al primo fotogramma decodificabile che esce.

Dal campo (Tab 5S via cloud, 2026-09-27): il video in card arrivava ~3 s dopo il primo
IDR. Due cause, misurate qui con la targa finta che manda un GOP di 3 s come quella
vera: ffmpeg tratteneva l'uscita 1,9 s (probe), e se l'RTP arrivava prima del suo avvio
il primo IDR era perso (video al secondo IDR, +3 s). Con -s stampa la linea del tempo.

    python -m pytest -m media tests/test_av_latency.py -s
"""
from __future__ import annotations

import asyncio

import aiohttp
import pytest
from harness import media as hm
from harness.media import decodable_frames
from harness.rig import Rig, run

from custom_components.vimar_intercom import av_stream
from custom_components.vimar_intercom import media_handler as media

pytestmark = pytest.mark.media


class _Timeline:
    """Istanti (loop.time) degli eventi: RTP ricevuto, RTP inoltrato a ffmpeg, ffmpeg
    avviato, byte usciti da /av."""

    def __init__(self):
        self.rx: list[tuple[float, int, int]] = []    # (t, seq, tipo NAL; 5 = inizio IDR)
        self.fwd: list[tuple[float, int]] = []        # (t, seq originale)
        self.popen: float | None = None
        self.chunks: list[tuple[float, int]] = []     # (t, byte cumulati)
        self.buf = bytearray()

    def idr_rx(self):
        return [(t, s) for t, s, n in self.rx if n == 5]


def _nal_type(payload: bytes) -> int:
    t = payload[0] & 0x1F
    if t == 28:  # FU-A: conta solo lo start
        return payload[1] & 0x1F if payload[1] & 0x80 else 0
    return t


def _hook(monkeypatch, tl: _Timeline):
    loop = asyncio.get_running_loop()
    orig_rx = media.RTPVideoProtocol.datagram_received

    def rx(self, data, addr):
        if len(data) >= 13:
            rtp = self.srtp_rx.unprotect(data) if self.srtp_rx else data
            if rtp:
                tl.rx.append((loop.time(), int.from_bytes(rtp[2:4], "big"), _nal_type(rtp[12:])))
        orig_rx(self, data, addr)

    monkeypatch.setattr(media.RTPVideoProtocol, "datagram_received", rx)
    orig_popen = av_stream.subprocess.Popen

    def popen(*a, **k):
        tl.popen = loop.time()
        return orig_popen(*a, **k)

    monkeypatch.setattr(av_stream.subprocess, "Popen", popen)
    vp = media.video_proto
    real = vp.ffmpeg_av_sock

    class Sock:
        def sendto(self, data, addr):
            tl.fwd.append((loop.time(), int.from_bytes(data[2:4], "big")))
            return real.sendto(data, addr)

    vp.ffmpeg_av_sock = Sock()


async def _read_av(base: str, tl: _Timeline, seconds: float):
    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession() as s, s.get(base + "/api/vimar_intercom/av") as r:
        assert r.status == 200, r.status
        end = loop.time() + seconds
        async for chunk in r.content.iter_any():
            tl.buf += chunk
            tl.chunks.append((loop.time(), len(tl.buf)))
            if loop.time() > end:
                return


def _first_decodable(tl: _Timeline) -> float | None:
    """Istante del primo chunk con cui ffprobe decodifica almeno un fotogramma."""
    lo, hi = 0, len(tl.chunks)
    if not hi or decodable_frames(tl.buf[:tl.chunks[-1][1]]) == 0:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if decodable_frames(tl.buf[:tl.chunks[mid][1]]) >= 1:
            hi = mid
        else:
            lo = mid + 1
    return tl.chunks[lo][0]


async def _scenario(monkeypatch, media_delay: float) -> dict:
    """"Vedi esterno": /av si apre subito dopo l'INVITE (come la card), la targa risponde
    dopo 1 s e manda il media `media_delay` s dopo il 200 OK."""
    aus = hm.access_units(45)  # IDR ogni 3 s, come la targa
    monkeypatch.setattr(hm, "access_units", lambda gop=15: aus)
    async with Rig(monkeypatch, real_av=True, http=True) as rig:
        await rig.register()
        tl = _Timeline()
        _hook(monkeypatch, tl)

        async def on_invite(peer, inv):
            peer.reply(inv, 100, "Trying")
            peer.reply(inv, 180, "Ringing")
            await asyncio.sleep(1.0)
            peer.pending_invite = None
            peer.reply(inv, 200, "OK", body=peer.sdp())
            await asyncio.sleep(media_delay)
            rig.start_media(inv.body)
        rig.peer.on_invite = on_invite

        call = asyncio.create_task(rig.hub.async_call())
        await asyncio.sleep(0.1)
        reader = asyncio.create_task(_read_av(rig.base, tl, 6))
        assert (await call)[0]
        await reader
        await rig.hub.async_hangup()
    idr = tl.idr_rx()
    t0 = idr[0][0]
    fwd_seqs = {s for _, s in tl.fwd}
    return dict(
        t_rtp=tl.rx[0][0] - t0,
        t_popen=tl.popen - t0, t_fwd=tl.fwd[0][0] - t0 if tl.fwd else None,
        primo_idr_inoltrato=idr[0][1] in fwd_seqs,
        t_primo_byte=tl.chunks[0][0] - t0 if tl.chunks else None,
        t_decodabile=(fd - t0) if (fd := _first_decodable(tl)) is not None else None,
        idr=[round(t - t0, 2) for t, _ in idr],
        fotogrammi=decodable_frames(tl.buf),
    )


def _show(name, r):
    print(f"\n[{name}] (s dal primo IDR della targa)")
    for k, v in r.items():
        print(f"  {k:22} {round(v, 3) if isinstance(v, float) else v}")


def test_primo_idr_non_si_perde_se_l_rtp_arriva_prima_di_ffmpeg(monkeypatch):
    """La targa manda subito dopo il 200 OK, /av sta ancora avviando ffmpeg: il primo
    IDR era perso e il video partiva al secondo, 3 s dopo (misurato 4,9 s; ora 0,4 s:
    avvio + 0,3 s prima dell'inoltro, poi il GOP in cache). Tetto largo per la CI."""
    r = run(_scenario(monkeypatch, 0.0))
    _show("media subito al 200 OK", r)
    assert r["primo_idr_inoltrato"], r
    assert r["fotogrammi"] >= 15
    assert r["t_decodabile"] is not None and r["t_decodabile"] < 1.5, r


def test_ffmpeg_non_trattiene_il_primo_fotogramma(monkeypatch):
    """Dal campo: il primo RTP arriva 0,6 s dopo l'avvio di ffmpeg (inoltro già attivo).
    ffmpeg tratteneva l'uscita 1,9 s (probe: fps + first_dts dell'H.264); ora 0,08 s."""
    r = run(_scenario(monkeypatch, 0.9))
    _show("media 0,9 s dopo il 200 OK (campo)", r)
    assert r["primo_idr_inoltrato"], r
    assert r["t_decodabile"] is not None and r["t_decodabile"] < 1.0, r
