"""Frame grabber: dai NAL H.264 della chiamata esce subito l'ultimo JPEG."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from custom_components.vimar_intercom import frame_grabber

# Solo il test con ffmpeg vero salta su Windows: le pipe asincrone di ffmpeg
# trattengono l'output a intermittenza; l'integrazione gira su Linux (HA), dove è affidabile.
ffmpeg_vero = [
    pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg non installato"),
    pytest.mark.skipif(sys.platform == "win32", reason="pipe asincrone di Windows"),
]


def _nals() -> list[bytes]:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", "4", "-g", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "baseline", "-tune", "zerolatency",
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
