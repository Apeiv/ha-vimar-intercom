"""Frame grabber: dai NAL H.264 della chiamata esce subito l'ultimo JPEG, e il clip MP4
dello squillo (record) copia gli stessi NAL dal primo IDR."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from harness.media import clip_audio_codec, clip_info

from custom_components.vimar_intercom import frame_grabber, media_handler

# Solo il test con ffmpeg vero salta su Windows: le pipe asincrone di ffmpeg
# trattengono l'output a intermittenza; l'integrazione gira su Linux (HA), dove è affidabile.
ffmpeg_vero = [
    pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg non installato"),
    pytest.mark.skipif(sys.platform == "win32", reason="pipe asincrone di Windows"),
]


def _nals() -> list[bytes]:
    """40 fotogrammi (4 s a 10 fps, IDR ogni 10), un NAL per fotogramma (niente slice),
    SPS/PPS solo in testa: gli IDR dopo il primo si decodificano solo con quelli in cache."""
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", "4", "-g", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "baseline", "-threads", "1",
         "-bsf:v", "h264_mp4toannexb", "-f", "h264", "pipe:1"],
        check=True, capture_output=True).stdout
    return [n for n in re.split(b"\x00\x00\x00\x01|\x00\x00\x01", raw) if n]


@ffmpeg_vero[0]
@ffmpeg_vero[1]
def test_grabber_tiene_l_ultimo_jpeg(monkeypatch):
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda: None)

    async def run():
        frame_grabber.start(proto)
        for nal in _nals():
            proto.frame_sink(nal)
        for _ in range(100):
            if frame_grabber.last_jpeg:
                break
            await asyncio.sleep(0.05)
        frame = frame_grabber.last_jpeg
        frame_grabber.stop(proto)
        await asyncio.sleep(0.1)  # lascia chiudere ffmpeg
        return frame

    frame = asyncio.run(run())
    assert frame and frame[:2] == b"\xff\xd8" and frame[-2:] == b"\xff\xd9"
    assert frame_grabber.last_jpeg is None  # fine chiamata: niente foto vecchie


# ─── ffmpeg finto: un JPEG per ogni IDR, senza processo ──────────────────────────

JPEG = b"\xff\xd8" + b"J" * 20 + b"\xff\xd9"


def _finto_ffmpeg(monkeypatch, jpegs: list[bytes]):
    """create_subprocess_exec che restituisce un processo con `jpegs` già su stdout."""
    class _Pipe:
        def __init__(self, data):
            self._data = data

        async def read(self, n):
            data, self._data = self._data, b""
            return data

        def write(self, b):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class _Proc:
        returncode = 0

        def __init__(self):
            self.stdin, self.stdout = _Pipe(b""), _Pipe(b"".join(jpegs))

    async def _exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(frame_grabber.asyncio, "create_subprocess_exec", _exec)


def test_il_primo_idr_e_la_foto_finche_non_ne_arriva_un_altro(monkeypatch):
    """Con un IDR ogni ~3 s (di notte >8 s) scartare il primo lasciava una vista da ~10 s
    senza foto; il secondo, quando arriva, lo sostituisce."""
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda: None)

    async def run(jpegs):
        _finto_ffmpeg(monkeypatch, jpegs)
        frame_grabber.start(proto)
        await asyncio.sleep(0.05)
        return frame_grabber.last_jpeg

    assert asyncio.run(run([JPEG])) == JPEG
    assert asyncio.run(run([JPEG, JPEG.replace(b"J", b"K")])) == JPEG.replace(b"J", b"K")
    frame_grabber.stop(proto)
    assert frame_grabber.last_jpeg is None


def test_reinvite_riavvia_il_grabber_senza_perdere_la_foto(monkeypatch):
    """Un re-INVITE con SDP nuovo rifà setup_media → frame_grabber.start a metà chiamata:
    la foto già presa resta finché non ne esce una nuova; stop() (fine chiamata) la toglie."""
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda: None)

    async def run():
        _finto_ffmpeg(monkeypatch, [JPEG])
        frame_grabber.start(proto)
        await asyncio.sleep(0.05)
        assert frame_grabber.last_jpeg == JPEG
        _finto_ffmpeg(monkeypatch, [])
        frame_grabber.start(proto)          # re-INVITE: nessun JPEG ancora dal nuovo ffmpeg
        await asyncio.sleep(0.05)
        assert frame_grabber.last_jpeg == JPEG
        frame_grabber.stop(proto)
        assert frame_grabber.last_jpeg is None

    asyncio.run(run())
async def _feed(proto, nals, fps=25, skip=0):
    """I NAL a ritmo reale (un fotogramma ogni 1/fps), saltando i primi `skip` NAL."""
    loop = asyncio.get_running_loop()
    t0, i = loop.time(), 0
    for n in nals[skip:]:
        proto.frame_sink(n)
        if n[0] & 0x1F in (1, 5):
            i += 1
            await asyncio.sleep(max(0.0, t0 + i / fps - loop.time()))


async def _feed_pcm(seconds: float):
    """PCM finto ogni 20 ms (come i pacchetti PCMU decodificati dalla targa), passato a
    media_handler.pcm_tap se e finché frame_grabber lo tiene agganciato."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        if media_handler.pcm_tap:
            media_handler.pcm_tap(b"\x00\x01" * 160)  # 320 B = 20 ms a 8 kHz, 16 bit, mono
        await asyncio.sleep(0.02)


@ffmpeg_vero[0]
def test_clip_mp4_dal_primo_idr_con_durata_reale(tmp_path):
    """record() prima del video (come allo squillo: il 183 fa partire l'anteprima dopo);
    stop() chiude il file: MP4 con moov in testa, H.264 copiato (tutti i 40 fotogrammi),
    durata quella dell'orologio (i 40 a 25 fps = 1,6 s; l'H.264 grezzo non ha tempi). Col
    PCM della targa tappato durante la registrazione, il clip esce anche con l'audio (AAC)."""
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda: None)
    path = str(tmp_path / "squillo_20260927_101500_001.mp4")

    async def run():
        done = asyncio.get_running_loop().create_future()
        frame_grabber.record(path, 60, done.set_result)
        frame_grabber.start(proto)
        await asyncio.gather(_feed(proto, _nals(), fps=25), _feed_pcm(2))
        frame_grabber.stop(proto)
        return await asyncio.wait_for(done, 20)

    assert asyncio.run(run()) == path
    assert os.path.exists(path) and not os.path.exists(path + ".part")
    head = open(path, "rb").read(40)
    assert head[4:8] == b"ftyp" and b"moov" in head, "moov non in testa (faststart)"
    codec, dur, n = clip_info(path)
    assert codec == "h264" and n == 40, (codec, n)
    assert 1.3 < dur < 2.2, f"durata {dur} s: non è quella dell'orologio"
    assert clip_audio_codec(path) == "aac", "PCM tappato durante la registrazione: audio atteso"


@ffmpeg_vero[0]
def test_clip_parte_dal_primo_idr_senza_niente_prima(tmp_path):
    """Video già in corso e SPS/PPS in cache (chiamata prima): P-frame prima dell'IDR non
    entrano nel clip (sarebbero grigi), il clip parte dall'IDR con SPS/PPS davanti."""
    nals = _nals()
    sps, pps = nals[0], nals[1]
    assert (sps[0] & 0x1F, pps[0] & 0x1F) == (7, 8)
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda: (sps, pps))
    path = str(tmp_path / "squillo_20260927_101500_002.mp4")
    idr0 = next(i for i, n in enumerate(nals) if n[0] & 0x1F == 5)

    async def run():
        done = asyncio.get_running_loop().create_future()
        frame_grabber.start(proto)
        frame_grabber.record(path, 60, done.set_result)
        await _feed(proto, nals[idr0 + 1:], fps=50)  # dal primo P: niente SPS/PPS/IDR iniziali
        frame_grabber.stop(proto)
        return await asyncio.wait_for(done, 20)

    assert asyncio.run(run()) == path
    codec, dur, n = clip_info(path)
    assert codec == "h264" and n == 30, n  # dal secondo IDR (30 fotogrammi), non 39


@ffmpeg_vero[0]
def test_clip_finisce_da_solo_al_tetto_e_senza_video_niente_file(tmp_path):
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda: None)
    path = str(tmp_path / "squillo_20260927_101500_003.mp4")
    nals = _nals()

    async def run():
        done = asyncio.get_running_loop().create_future()
        frame_grabber.record(path, 0.6, done.set_result)
        frame_grabber.start(proto)
        t0 = time.monotonic()
        feeder = asyncio.create_task(_feed(proto, nals, fps=25))
        got = await asyncio.wait_for(done, 20)  # senza stop(): il tetto chiude il clip
        feeder.cancel()
        vuoto = asyncio.get_running_loop().create_future()
        frame_grabber.record(str(tmp_path / "squillo_20260927_101500_004.mp4"), 0.3, vuoto.set_result)
        frame_grabber.stop(proto)  # squillo finito prima del video: niente file
        return got, time.monotonic() - t0, await asyncio.wait_for(vuoto, 20)

    got, took, vuoto = asyncio.run(run())
    assert got == path and took < 3
    assert vuoto is None and sorted(os.listdir(tmp_path)) == [os.path.basename(path)]
