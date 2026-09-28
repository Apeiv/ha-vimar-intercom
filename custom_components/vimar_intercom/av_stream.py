"""Vimar Intercom — /av: RTP della chiamata → un ffmpeg → MPEG-TS per go2rtc, lo stream worker di HA e HomeKit."""

import asyncio
import base64
import collections
import logging
import os
import struct
import subprocess
import tempfile

from . import media_handler as media
from .const import FFMPEG_AV_AUDIO_PORT, FFMPEG_AV_VIDEO_PORT

_LOGGER = logging.getLogger(__name__)

av_ffmpeg_proc = None
_stderr_task: asyncio.Task | None = None


class AvRtp:
    """RTP verso l'ffmpeg di /av come un flusso solo, continuo.

    Se la targa riparte (SSRC nuovo: encoder o relay riavviato) ffmpeg scarta
    ogni pacchetto «received too late» e /av resta fermo fino alla fine della
    chiamata. Qui SSRC, seq e timestamp ripartono da dove erano rimasti; il
    primo flusso passa invariato. Solo l'SSRC conta: una seq che torna indietro
    è un pacchetto vecchio rimandato (40515), non un riavvio."""

    def __init__(self, ts_step: int):
        self.ts_step = ts_step
        self.ssrc = self.out_ssrc = self.last = None
        self.seq_off = self.ts_off = 0

    def fix(self, rtp: bytes, pt: int) -> bytes:
        seq, ts, ssrc = struct.unpack_from('!HII', rtp, 2)
        if self.last is not None and ssrc != self.ssrc:
            self.seq_off = (self.last[0] + 1 - seq) & 0xFFFF
            self.ts_off = (self.last[1] + self.ts_step - ts) & 0xFFFFFFFF
        if self.out_ssrc is None:
            self.out_ssrc = ssrc
        self.ssrc = ssrc
        seq, ts = (seq + self.seq_off) & 0xFFFF, (ts + self.ts_off) & 0xFFFFFFFF
        self.last = (seq, ts)
        return (rtp[:1] + bytes([(rtp[1] & 0x80) | pt])
                + struct.pack('!HII', seq, ts, self.out_ssrc) + rtp[12:])


_AV_SDP_PATH = os.path.join(tempfile.gettempdir(), "vimar_intercom_av.sdp")

# Serializza start/stop dell'ffmpeg AV: due /av concorrenti non devono
# lanciare due processi che si contendono le stesse porte UDP.
_av_lock = asyncio.Lock()


def _write_av_sdp():
    """Write the ffmpeg input SDP to disk (blocking — run in executor).

    PT/porte devono combaciare con ciò che RTPVideo/RTPAudioProtocol
    inoltrano su 127.0.0.1 (FFMPEG_AV_VIDEO_PORT / FFMPEG_AV_AUDIO_PORT).
    """
    # Con SPS/PPS già visti ffmpeg decodifica dal primo IDR, invece di
    # aspettarne uno che li porti in banda (sulla 40515 ~8 s, oltre la chiamata).
    sprop = ""
    if media.video_proto and (ps := media.video_proto.sps_pps()):
        sprop = ";sprop-parameter-sets=" + ",".join(base64.b64encode(n).decode() for n in ps)
    sdp = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=AV\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        f"m=audio {FFMPEG_AV_AUDIO_PORT} RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        f"m=video {FFMPEG_AV_VIDEO_PORT} RTP/AVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        f"a=fmtp:96 profile-level-id=42801F;packetization-mode=1{sprop}\r\n"
    )
    with open(_AV_SDP_PATH, "w") as f:
        f.write(sdp)
    return _AV_SDP_PATH


async def _start_av_ffmpeg_locked():
    """Start ffmpeg that reads H264+PCMU RTP and outputs MPEG-TS to pipe.

    Il chiamante deve tenere _av_lock. Ferma l'istanza precedente
    (attendendone la terminazione, così le porte UDP si liberano prima del
    nuovo bind), scrive l'SDP in executor, poi abilita il forward RTP verso
    ffmpeg SOLO dopo lo start.
    """
    global av_ffmpeg_proc, _stderr_task
    await _stop_av_ffmpeg_locked()

    loop = asyncio.get_running_loop()
    try:
        sdp_path = await loop.run_in_executor(None, _write_av_sdp)
    except Exception as e:
        _LOGGER.error("AV SDP write error: %s", e)
        return

    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-protocol_whitelist", "file,udp,rtp",
        "-fflags", "+genpts+discardcorrupt",
        # Senza, find_stream_info trattiene l'uscita ~1,9 s dopo il primo IDR: 20
        # fotogrammi per stimare gli fps, poi il first_dts che per l'H.264 arriva
        # solo dopo 7 fotogrammi decodificati (has_decode_delay_been_guessed).
        # Sul campo la targa chiude dopo ~10 s. Misurato: 1,88 s → 0,08 s.
        "-fpsprobesize", "0", "-max_ts_probe", "0",
        "-i", sdp_path,
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "mpegts",
        "pipe:1",
    ]
    try:  # Popen (fork + exec) fuori dall'event loop
        av_ffmpeg_proc = await loop.run_in_executor(
            None, lambda: subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    except Exception as e:
        _LOGGER.error("AV ffmpeg start error: %s", e)
        av_ffmpeg_proc = None
        return

    proc = av_ffmpeg_proc
    stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
    # Riferimento tenuto: un task senza riferimenti può essere raccolto a metà lettura.
    _stderr_task = stderr_task = asyncio.create_task(_read_av_ffmpeg_stderr(proc, stderr_tail))
    # Give ffmpeg a moment to bind the UDP recv ports before we start
    # pushing RTP at them (avoids the very first packets being dropped).
    await asyncio.sleep(0.3)
    if av_ffmpeg_proc and av_ffmpeg_proc.poll() is None:
        if media.video_proto:
            media.video_proto.av_rtp = AvRtp(3000)  # ffmpeg nuovo: flusso nuovo
            media.video_proto.forward_av = True
            media.video_proto.replay_gop()  # l'IDR arrivato prima non va perso
        if media.audio_proto:
            media.audio_proto.av_rtp = AvRtp(160)
            media.audio_proto.forward_av = True
        _LOGGER.info("AV ffmpeg started (MPEG-TS output), RTP forwarding enabled")
    else:
        # Il motivo sta nello stderr (es. «bind failed» con porte che si
        # sovrappongono, issue #8): aspettiamo che il lettore arrivi a EOF
        # e lo riportiamo, invece di un errore muto.
        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except Exception:  # noqa: BLE001 — timeout o lettore fallito
            pass
        _LOGGER.error(
            "AV ffmpeg exited immediately during startup (rc=%s): %s",
            proc.poll(), " | ".join(stderr_tail) or "no stderr output",
        )


# Un solo ffmpeg per tutti i client di /av (go2rtc e lo stream worker di HA lo
# aprono insieme): il primo lo avvia, l'ultimo lo ferma, tutto sotto _av_lock.
_av_clients: set[asyncio.Queue] = set()
_av_pump: asyncio.Task | None = None


def _av_end(q: asyncio.Queue) -> None:
    _av_clients.discard(q)
    if q.full():
        q.get_nowait()
    q.put_nowait(None)


async def _av_pump_run(proc) -> None:
    loop = asyncio.get_running_loop()
    try:
        while proc.poll() is None:
            chunk = await loop.run_in_executor(None, proc.stdout.read1, 4096)
            if not chunk:
                break
            for q in list(_av_clients):
                if q.full():
                    _av_end(q)  # client troppo lento: staccarlo, non corrompere il TS
                else:
                    q.put_nowait(chunk)
    except (OSError, ValueError) as e:
        _LOGGER.debug("AV pump ended: %s", e)
    finally:
        for q in list(_av_clients):
            _av_end(q)


async def av_subscribe() -> asyncio.Queue | None:
    """Aggancia un client allo stream MPEG-TS; None se ffmpeg non parte."""
    global _av_pump
    async with _av_lock:
        if _av_pump is None or _av_pump.done():
            await _start_av_ffmpeg_locked()
            proc = av_ffmpeg_proc
            if not proc or proc.poll() is not None:
                return None
            _av_pump = asyncio.create_task(_av_pump_run(proc))
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        _av_clients.add(q)
        return q


async def av_unsubscribe(q: asyncio.Queue) -> None:
    async with _av_lock:
        _av_clients.discard(q)
        if not _av_clients:
            await _stop_av_ffmpeg_locked()
            if _av_pump:
                await asyncio.wait([_av_pump])


async def stop_av_ffmpeg():
    async with _av_lock:
        await _stop_av_ffmpeg_locked()


async def _stop_av_ffmpeg_locked():
    """Actual stop — caller must hold _av_lock."""
    global av_ffmpeg_proc
    # Stop forwarding first so no more packets hit the (closing) ffmpeg.
    if media.video_proto:
        media.video_proto.forward_av = False
    if media.audio_proto:
        media.audio_proto.forward_av = False
    if av_ffmpeg_proc:
        proc = av_ffmpeg_proc
        av_ffmpeg_proc = None
        # kill subito: l'uscita è una pipe, non c'è file da chiudere bene. Con
        # terminate + attesa la fine chiamata arrivava all'hub 3 s dopo il BYE.
        try:
            proc.kill()
            await asyncio.get_running_loop().run_in_executor(None, proc.wait, 2)
        except Exception:  # noqa: BLE001 — già uscito
            pass
        _LOGGER.info("AV ffmpeg stopped")


async def _read_av_ffmpeg_stderr(proc, tail: collections.deque[str]):
    """Legge lo stderr di ffmpeg fino a EOF, tenendone la coda in `tail`.

    Legato al processo passato, non al globale, e senza guardare `poll()`:
    prima il ciclo usciva appena il processo era già morto, cioè proprio nel
    caso in cui lo stderr spiega il perché (issue #8).
    """
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, proc.stderr.readline)
        except Exception:  # noqa: BLE001
            break
        if not line:
            break
        text = line.decode(errors="replace").strip()
        if text:
            tail.append(text)
            _LOGGER.debug("AV ffmpeg: %s", text)
