"""Foto della chiamata e dell'anteprima dello squillo.

Un ffmpeg per chiamata (o anteprima) decodifica i NAL H.264 che arrivano da
`video_proto.frame_sink`, dal primo SPS in poi (quindi anche il primo IDR), e
tiene l'ultimo JPEG. Chi chiede una foto non aspetta il prossimo IDR (di notte,
a scena ferma, anche >8 s).
"""

import asyncio
import logging
import subprocess

_LOGGER = logging.getLogger(__name__)

last_jpeg: bytes | None = None
_grabber: asyncio.Task | None = None


async def wait_frame(timeout: float = 6) -> bytes | None:
    """L'ultimo JPEG, aspettando il primo se il video è appena partito: la targa
    manda un IDR ogni ~3 s e il primo può perdersi (sotto il timeout di HA, 10 s)."""
    for _ in range(int(timeout / 0.1)):
        if last_jpeg or not _grabber or _grabber.done():  # ffmpeg morto: inutile aspettare
            break
        await asyncio.sleep(0.1)
    return last_jpeg


def start(video_proto) -> None:
    global _grabber
    # Non stop(): un re-INVITE con SDP nuovo riavvia il grabber a metà chiamata, e la
    # foto già presa resta finché non ne esce una nuova (la chiamata è la stessa).
    _cancel(video_proto)
    q: asyncio.Queue = asyncio.Queue(maxsize=600)
    # SPS/PPS della chiamata prima: ffmpeg decodifica dal primo IDR anche se la
    # targa manda l'SPS in banda solo dopo (40515: ~6 s).
    started = bool(ps := video_proto.sps_pps())
    for nal in ps or ():
        q.put_nowait(nal)

    def sink(nal: bytes) -> None:
        nonlocal started
        started = started or nal[0] & 0x1F == 7  # ffmpeg deve partire da un SPS
        if started and not q.full():
            q.put_nowait(nal)

    video_proto.frame_sink = sink
    _grabber = asyncio.create_task(_grab(q))


def _cancel(video_proto) -> None:
    global _grabber
    if video_proto:
        video_proto.frame_sink = None
    if _grabber:
        _grabber.cancel()
        _grabber = None


def stop(video_proto) -> None:
    """Fine della chiamata (o dell'anteprima): niente foto vecchie per quella dopo."""
    global last_jpeg
    _cancel(video_proto)
    last_jpeg = None


async def _grab(q: asyncio.Queue) -> None:
    global last_jpeg
    try:
        proc = await asyncio.create_subprocess_exec(
            # Solo i fotogrammi completi (IDR, ~ogni 3 s): autonomi, quindi puliti
            # anche dopo una perdita di pacchetti, che sui P-frame dà immagini
            # sgranate. probesize minimo e un thread: il JPEG esce appena arriva l'IDR.
            "ffmpeg", "-loglevel", "error", "-probesize", "4096", "-analyzeduration", "0",
            "-threads", "1", "-skip_frame", "nokey",
            "-f", "h264", "-i", "pipe:0", "-pix_fmt", "yuvj420p",
            "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-flush_packets", "1", "pipe:1",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        _LOGGER.warning("Foto: ffmpeg non avviabile (%s)", e)
        return

    async def feed():
        try:
            while True:
                proc.stdin.write(b"\x00\x00\x00\x01" + await q.get())
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    feeder = asyncio.create_task(feed())
    buf = b""
    try:
        while chunk := await proc.stdout.read(65536):
            buf += chunk
            while (end := buf.find(b"\xff\xd9")) != -1:
                start = buf.find(b"\xff\xd8")
                # Anche il primo IDR, spesso scuro (la telecamera regola l'esposizione):
                # con un IDR ogni ~3 s, o >8 s di notte, scartarlo lasciava una vista
                # da ~10 s senza foto. Quello dopo lo sostituisce.
                if 0 <= start < end:
                    last_jpeg = buf[start:end + 2]
                buf = buf[end + 2:]
    finally:
        feeder.cancel()
        proc.stdin.close()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
