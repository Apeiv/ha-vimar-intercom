"""Frame grabber: dai NAL H.264 della chiamata esce subito l'ultimo JPEG, e il clip MP4
dello squillo (record) copia gli stessi NAL dal primo IDR."""
from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from harness.media import clip_audio_codec, clip_info

from custom_components.vimar_intercom import av_passive, frame_grabber, media_handler

# Solo il test con ffmpeg vero salta su Windows: le pipe asincrone di ffmpeg
# trattengono l'output a intermittenza; l'integrazione gira su Linux (HA), dove è affidabile.
ffmpeg_vero = [
    pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg non installato"),
    pytest.mark.skipif(sys.platform == "win32", reason="pipe asincrone di Windows"),
]


def _nals(*x264: str) -> list[bytes]:
    """40 fotogrammi (4 s a 10 fps; di default IDR ogni 10, baseline: `x264` li sostituisce),
    un NAL per fotogramma (niente slice), SPS/PPS solo in testa: gli IDR dopo il primo si
    decodificano solo con quelli in cache."""
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1",
         *(x264 or ("-g", "10", "-profile:v", "baseline")),
         "-bsf:v", "h264_mp4toannexb", "-f", "h264", "pipe:1"],
        check=True, capture_output=True).stdout
    return [n for n in re.split(b"\x00\x00\x00\x01|\x00\x00\x01", raw) if n]


@ffmpeg_vero[0]
@ffmpeg_vero[1]
def test_grabber_tiene_l_ultimo_jpeg(monkeypatch):
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda own_only=False: None)

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


@ffmpeg_vero[0]
@ffmpeg_vero[1]
def test_foto_dal_primo_idr_con_riordino_e_un_solo_idr():
    """#129: un solo IDR per squillo (40517) e un decoder che riordina (qui B-frame): la
    foto deve uscire dal primo IDR, con lo stdin di ffmpeg ancora aperto."""
    nals = _nals("-g", "1000", "-sc_threshold", "0", "-profile:v", "main", "-bf", "1")
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda own_only=False: None)

    async def run():
        frame_grabber.start(proto)
        try:
            await _feed(proto, nals[:12], fps=10)  # SPS, PPS, SEI, IDR e 8 P/B: ~1 s
            return await frame_grabber.wait_frame(timeout=3)
        finally:
            frame_grabber.stop(proto)
            await asyncio.sleep(0.1)  # lascia chiudere ffmpeg

    frame = asyncio.run(run())
    assert frame and frame[:2] == b"\xff\xd8"


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
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda own_only=False: None)

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
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda own_only=False: None)

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
    ogni tap agganciato in media_handler.pcm_taps (es. frame_grabber)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        for tap in media_handler.pcm_taps:
            tap(b"\x00\x01" * 160)  # 320 B = 20 ms a 8 kHz, 16 bit, mono
        await asyncio.sleep(0.02)


@ffmpeg_vero[0]
def test_clip_mp4_dal_primo_idr_con_durata_reale(tmp_path):
    """record() prima del video (come allo squillo: il 183 fa partire l'anteprima dopo);
    stop() chiude il file: MP4 con moov in testa, H.264 copiato (tutti i 40 fotogrammi),
    durata quella dell'orologio (i 40 a 25 fps = 1,6 s; l'H.264 grezzo non ha tempi). Col
    PCM della targa tappato durante la registrazione, il clip esce anche con l'audio (AAC)."""
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda own_only=False: None)
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
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda own_only=False: (sps, pps))
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
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda own_only=False: None)
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


def _finto_encoder_passivo(monkeypatch):
    """Il mux di av_passive (video+audio -> MPEG-TS) finto: al bug/fix interessa solo
    che _run() tappi media.pcm_taps per davvero, non che l'encoder produca un TS vero.
    Un solo ffmpeg reale per test su Windows (quello del clip): l'altro, con pipe
    stdout lette in continuo, è il pattern segnalato fragile lì (vedi sopra)."""
    real_exec = asyncio.create_subprocess_exec

    class _NoOp:
        def write(self, b):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class _Stdout:
        async def read(self, n):
            await asyncio.sleep(0.05)
            return b"\x00"  # mai vuoto: _pump non esce da solo, solo a cancel

    class _FakeProc:
        def __init__(self):
            self.stdin, self.stdout = _NoOp(), _Stdout()
            self.returncode = None

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode or 0

    async def _exec(*args, **kwargs):
        # Solo l'encoder di av_passive porta un ingresso "tcp://..." in coda: il resto
        # (i due ffmpeg del clip) passa al vero create_subprocess_exec.
        if any(isinstance(a, str) and a.startswith("tcp://") for a in args):
            return _FakeProc()
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)

    async def _fake_standby():
        return bytes(av_passive._FRAME)

    async def _fake_audio_in(port):
        return _NoOp()

    monkeypatch.setattr(av_passive, "standby_frame", _fake_standby)
    monkeypatch.setattr(av_passive, "_audio_in", _fake_audio_in)


def _tono_ulaw(n: int = 160) -> bytes:
    """20 ms di PCMU vero (non silenzio: 0xFF/0x7F sono gli zeri della mu-law), un
    tono a 440 Hz codificato con lo stesso codec di media_handler."""
    pcm = struct.pack(f"<{n}h", *(int(6000 * math.sin(2 * math.pi * 440 * t / 8000)) for t in range(n)))
    return media_handler.ulaw_encode(pcm)


def test_pcm_taps_uno_si_ferma_altro_continua_a_ricevere():
    """item 1+5: media.pcm_taps è una lista, non più un solo slot con prev_tap incatenato
    -- il teardown di un tap (es. il clip dello squillo che finisce) resettava sempre lo
    slot al valore salvato all'aggancio, così se nel frattempo un altro tap si era agganciato
    sopra (es. lo stream passivo), quello restava senza audio. Con la lista, sganciare un tap
    tocca solo la propria voce: gli altri, agganciati con add_pcm_tap, continuano a ricevere
    il PCM dal punto in cui lo passa RTPAudioProtocol.datagram_received."""
    audio_proto = media_handler.RTPAudioProtocol()
    audio_proto.remote_addr = ("127.0.0.1", 4000)
    ricevuto_a, ricevuto_b = [], []
    tap_a, tap_b = ricevuto_a.append, ricevuto_b.append

    def invia(seq, ts):
        rtp = struct.pack("!BBHII", 0x80, 0, seq, ts, 1) + _tono_ulaw()
        audio_proto.datagram_received(rtp, ("127.0.0.1", 4000))

    media_handler.add_pcm_tap(tap_a)
    media_handler.add_pcm_tap(tap_b)
    try:
        invia(0, 0)
        assert len(ricevuto_a) == 1 and len(ricevuto_b) == 1
        media_handler.remove_pcm_tap(tap_a)  # tap_a si ferma, tap_b resta agganciato
        invia(1, 160)
        assert len(ricevuto_a) == 1, "fermato: non deve ricevere altro"
        assert len(ricevuto_b) == 2, "l'altro tap deve continuare a ricevere il PCM"
    finally:
        media_handler.remove_pcm_tap(tap_b)


@ffmpeg_vero[0]
def test_clip_audio_non_muto_con_passivo_attivo(tmp_path, monkeypatch):
    """Nota ring-2040: audio rx=918 alla targa ma AAC muto nel clip. Causa: av_passive
    (il continuo per Frigate/go2rtc) sovrascriveva media_handler.pcm_tap senza
    incatenarlo a quello già messo da frame_grabber per il clip dello squillo -- se il
    passivo si aggancia mentre il clip e' gia' in corso (es. Frigate riconnette a /av
    proprio durante uno squillo), l'audio vero arrivato via RTP finiva solo nel buffer
    del passivo e il clip restava silenzioso."""
    _finto_encoder_passivo(monkeypatch)
    proto = SimpleNamespace(frame_sink=None, sps_pps=lambda own_only=False: None)
    path = str(tmp_path / "squillo_20260928_204023_323.mp4")
    audio_proto = media_handler.RTPAudioProtocol()
    audio_proto.remote_addr = ("127.0.0.1", 4000)
    payload = _tono_ulaw()

    async def feed_pcmu(seconds: float):
        loop = asyncio.get_running_loop()
        end, seq, ts = loop.time() + seconds, 0, 0
        while loop.time() < end:
            rtp = struct.pack("!BBHII", 0x80, 0, seq, ts, 1) + payload
            audio_proto.datagram_received(rtp, ("127.0.0.1", 4000))
            seq, ts = (seq + 1) & 0xFFFF, (ts + 160) & 0xFFFFFFFF
            await asyncio.sleep(0.02)

    async def run():
        done = asyncio.get_running_loop().create_future()
        frame_grabber.record(path, 60, done.set_result)
        frame_grabber.start(proto)
        await asyncio.sleep(0.05)  # il clip parte e si aggancia a pcm_taps prima del passivo
        q = await av_passive.subscribe(lambda: False, lambda: None)
        assert q is not None
        await asyncio.sleep(0.05)  # av_passive tappa a sua volta (qui il bug)
        try:
            await asyncio.gather(_feed(proto, _nals(), fps=25), feed_pcmu(1.6))
            frame_grabber.stop(proto)
            return await asyncio.wait_for(done, 20)
        finally:
            await av_passive.unsubscribe(q)

    assert asyncio.run(run()) == path
    assert clip_audio_codec(path) == "aac"
    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=30).stderr
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", out)
    assert m and float(m.group(1)) > -60, f"audio del clip silenzioso: {out}"
