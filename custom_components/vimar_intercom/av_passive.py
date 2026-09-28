"""/av?autocall=0&idle=image: un MPEG-TS continuo per Scrypted, go2rtc e Frigate.

A riposo il fotogramma di standby (standby.png), durante squillo o chiamata il video
della targa. Un solo libx264 (640x480, 10 fps, IDR ogni secondo, baseline) per tutti i
client: nato col primo, fermato con l'ultimo, così i parametri del codec non cambiano
mai a metà stream. Python gli passa i fotogrammi grezzi a ritmo fisso: lo standby, o
l'ultimo decodificato dalla targa. Il live arriva dal fan-out MPEG-TS di av_stream
(come un client passivo qualunque) e lo decodifica un secondo ffmpeg, vivo solo finché
c'è video. Niente chiamate né spettatori per l'hub: il passivo non tocca la targa.
Solo video: l'audio della targa resta su /av e sulla card.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Callable

from . import av_stream

_LOGGER = logging.getLogger(__name__)

W, H, FPS = 640, 480, 10
_FRAME = W * H * 3 // 2  # yuv420p
_SCALE = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:-1:-1"
_STANDBY_PNG = os.path.join(os.path.dirname(__file__), "standby.png")

_standby: bytes | None = None
_live: bytes | None = None          # ultimo fotogramma della targa, None a riposo
_clients: set[asyncio.Queue] = set()
_lock = asyncio.Lock()
_task: asyncio.Task | None = None


async def standby_frame() -> bytes:
    """Lo standby in yuv420p WxH, reso una volta da standby.png e tenuto in memoria."""
    global _standby
    if _standby is None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", _STANDBY_PNG, "-frames:v", "1",
            "-vf", _SCALE, "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = await proc.communicate()
        if len(out) != _FRAME:
            raise RuntimeError(f"standby.png: ffmpeg ha reso {len(out)} byte, attesi {_FRAME}")
        _standby = out
    return _standby


async def subscribe(is_live: Callable[[], bool], on_live: Callable[[], None]) -> asyncio.Queue | None:
    """Aggancia un client; None se ffmpeg non parte. `is_live`: c'è video della targa
    (hub.video_active); `on_live`: chiesto un keyframe quando il decoder si aggancia."""
    global _task
    async with _lock:
        if _task is None:
            try:
                standby = await standby_frame()
                enc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-loglevel", "error",
                    "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{W}x{H}", "-r", str(FPS),
                    "-i", "pipe:0", "-an",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-profile:v", "baseline", "-g", str(FPS), "-keyint_min", str(FPS),
                    "-sc_threshold", "0",
                    "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
                    "pipe:1",
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except (OSError, RuntimeError) as e:
                _LOGGER.error("Stream passivo continuo: ffmpeg non parte (%s)", e)
                return None
            _task = asyncio.create_task(_run(enc, standby, is_live, on_live))
            _LOGGER.info("Stream passivo continuo avviato (%dx%d @%d fps)", W, H, FPS)
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        _clients.add(q)
        return q


async def unsubscribe(q: asyncio.Queue) -> None:
    global _task
    async with _lock:
        _clients.discard(q)
        if not _clients and _task:
            # Prima di aspettare: HA cancella l'handler di /av quando il client se ne va,
            # anche mentre siamo qui. _run si chiude da sé una volta cancellato.
            task, _task = _task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            _LOGGER.info("Stream passivo continuo fermato")


async def _run(enc, standby: bytes, is_live, on_live) -> None:
    """Un fotogramma ogni 1/FPS all'encoder, finché c'è un client."""
    global _live
    pump = asyncio.create_task(_pump(enc.stdout))
    decoder: asyncio.Task | None = None
    loop = asyncio.get_running_loop()
    t0, n = loop.time(), 0
    cancelled = False
    try:
        while not pump.done():
            if is_live() and (decoder is None or decoder.done()):
                decoder = asyncio.create_task(_decode(is_live, on_live))
            enc.stdin.write(_live or standby)
            await enc.stdin.drain()
            n += 1
            await asyncio.sleep(max(0.0, t0 + n / FPS - loop.time()))
        _LOGGER.warning("Stream passivo continuo: encoder uscito")
    except (BrokenPipeError, ConnectionResetError):
        _LOGGER.warning("Stream passivo continuo: encoder uscito")
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if decoder:
            decoder.cancel()
            await asyncio.gather(decoder, return_exceptions=True)
        enc.stdin.close()
        if enc.returncode is None:
            enc.kill()
            await enc.wait()
        # Mai aspettare la pipe dopo il kill: con un wrapper di ffmpeg (shim) muore
        # il wrapper, il figlio tiene la pipe aperta e l'attesa non finirebbe mai.
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        if not cancelled:  # morto da solo: i client vanno chiusi (cancellato = nessun client)
            for q in list(_clients):
                av_stream.end_client(q, _clients)


async def _pump(stdout) -> None:
    while chunk := await stdout.read(65536):
        av_stream.fanout(chunk, _clients)


async def _decode(is_live, on_live) -> None:
    """Finché c'è video: client del fan-out di av_stream → ffmpeg → fotogrammi WxH in _live."""
    global _live
    try:
        while is_live():
            q = await av_stream.av_subscribe()
            if q is None:
                await asyncio.sleep(1)
                continue
            try:
                on_live()  # IDR subito: si decodifica dal primo fotogramma, non al prossimo
                # analyzeduration 1 µs: il TS di /av porta anche l'AAC, e senza un tetto
                # ffmpeg sondava fino a probesize (~1,5 s in più al primo fotogramma live).
                dec = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-loglevel", "error", "-probesize", "32768", "-analyzeduration", "1",
                    "-fpsprobesize", "0", "-threads", "1", "-f", "mpegts", "-i", "pipe:0", "-an",
                    "-vf", _SCALE, "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                feeder = asyncio.create_task(_feed(q, dec.stdin))
                try:
                    while frame := await dec.stdout.readexactly(_FRAME):
                        _live = frame
                except asyncio.IncompleteReadError:
                    pass  # fine del TS (chiamata finita) o ffmpeg morto: si riprova se serve
                finally:
                    feeder.cancel()
                    await asyncio.gather(feeder, return_exceptions=True)
                    if dec.returncode is None:
                        dec.kill()
                        await dec.wait()
            finally:
                await av_stream.av_unsubscribe(q)
    finally:
        _live = None


async def _feed(q: asyncio.Queue, stdin) -> None:
    try:
        while (chunk := await q.get()) is not None:
            stdin.write(chunk)
            await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stdin.close()
