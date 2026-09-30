"""/av?idle=image (av_passive) with fake ffmpeg processes: what goes into the
encoder, what reaches the clients, and the cleanup when the encoder dies, the
last client leaves or the live decoder ends. No ffmpeg needed (CI has none)."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.vimar_intercom import av_passive, av_stream
from custom_components.vimar_intercom import media_handler as media


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k, val in dict(_clients=set(), _task=None, _enc=None, _live=None, _standby=None,
                       _pcm=bytearray()).items():
        monkeypatch.setattr(av_passive, k, val)
    monkeypatch.setattr(av_passive, "_lock", asyncio.Lock())
    monkeypatch.setattr(media, "pcm_taps", [])


class _Stdin:
    def __init__(self, broken=False):
        self.writes: list[bytes] = []
        self.closed = False
        self.broken = broken

    def write(self, data):
        if self.broken:
            raise BrokenPipeError
        self.writes.append(bytes(data))

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True


class _Stdout:
    """Returns the queued chunks; b"" (EOF) once `eof` is set."""

    def __init__(self, chunks=(), frames=()):
        self.chunks = list(chunks)
        self.frames = list(frames)
        self.eof = asyncio.Event()

    async def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        await self.eof.wait()
        return b""

    async def readexactly(self, n):
        for _ in range(3):  # let the feeder run, as a real pipe would
            await asyncio.sleep(0)
        if self.frames:
            return self.frames.pop(0)
        raise asyncio.IncompleteReadError(b"", n)


class _Proc:
    def __init__(self, stdout=None, out=b"", stuck=False, broken_stdin=False):
        self.args = ()
        self.stdin = _Stdin(broken_stdin)
        self.stdout = stdout or _Stdout()
        self.returncode = None
        self.killed = False
        self._out = out
        self._stuck = stuck

    async def communicate(self):
        self.returncode = 0
        return self._out, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self._stuck:
            raise asyncio.TimeoutError
        return self.returncode


def _spawner(monkeypatch, *procs):
    """asyncio.create_subprocess_exec returning `procs` in order, args recorded."""
    queue = list(procs)

    async def spawn(*args, **kwargs):
        proc = queue.pop(0)
        if isinstance(proc, Exception):
            raise proc
        proc.args = args
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return queue


class _Writer:
    def __init__(self):
        self.data: list[bytes] = []
        self.closed = False

    def write(self, data):
        self.data.append(bytes(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


# ─── standby frame ───────────────────────────────────────────────────────────

def test_standby_frame_is_rendered_once_and_then_kept(monkeypatch):
    frame = bytes(av_passive._FRAME)
    remaining = _spawner(monkeypatch, _Proc(out=frame))
    assert asyncio.run(av_passive.standby_frame()) == frame
    assert asyncio.run(av_passive.standby_frame()) == frame  # no second ffmpeg
    assert remaining == []


def test_a_standby_frame_of_the_wrong_size_is_an_error(monkeypatch):
    _spawner(monkeypatch, _Proc(out=b"short"))
    with pytest.raises(RuntimeError, match="5 byte"):
        asyncio.run(av_passive.standby_frame())
    assert av_passive._standby is None


# ─── subscribe / unsubscribe ─────────────────────────────────────────────────

def test_subscribe_returns_none_when_ffmpeg_does_not_start(monkeypatch):
    async def standby():
        return b"s"

    monkeypatch.setattr(av_passive, "standby_frame", standby)
    _spawner(monkeypatch, OSError("no ffmpeg"))
    assert asyncio.run(av_passive.subscribe(lambda: False, lambda: None)) is None
    assert av_passive._task is None and not av_passive._clients


def test_subscribe_returns_none_when_the_standby_cannot_be_rendered(monkeypatch):
    async def standby():
        raise RuntimeError("bad png")

    monkeypatch.setattr(av_passive, "standby_frame", standby)
    assert asyncio.run(av_passive.subscribe(lambda: False, lambda: None)) is None


def test_the_encoder_takes_audio_on_a_local_port_and_video_on_stdin(monkeypatch):
    async def standby():
        return b"s"

    async def fake_run(*a):
        await asyncio.Event().wait()

    monkeypatch.setattr(av_passive, "standby_frame", standby)
    monkeypatch.setattr(av_passive, "_run", fake_run)
    monkeypatch.setattr(av_passive, "_free_port", lambda: 40123)
    enc = _Proc()
    _spawner(monkeypatch, enc)

    async def run():
        q = await av_passive.subscribe(lambda: False, lambda: None)
        assert q is not None and av_passive._enc is enc
        await av_passive.stop()

    asyncio.run(run())
    args = enc.args
    assert args[0] == "ffmpeg"
    assert "tcp://127.0.0.1:40123?listen=1" in args and "pipe:0" in args
    assert args[args.index("-c:v") + 1] == "libx264" and args[-1] == "pipe:1"
    assert enc.killed  # stopped before _run could close it


def test_a_second_client_does_not_start_a_second_encoder(monkeypatch):
    async def standby():
        return b"s"

    async def fake_run(*a):
        await asyncio.Event().wait()

    monkeypatch.setattr(av_passive, "standby_frame", standby)
    monkeypatch.setattr(av_passive, "_run", fake_run)
    remaining = _spawner(monkeypatch, _Proc(), _Proc())

    async def run():
        a = await av_passive.subscribe(lambda: False, lambda: None)
        task = av_passive._task
        b = await av_passive.subscribe(lambda: False, lambda: None)
        assert av_passive._task is task and len(remaining) == 1
        await av_passive.unsubscribe(a)
        assert av_passive._task is task  # b is still watching
        await av_passive.unsubscribe(b)
        assert av_passive._task is None

    asyncio.run(run())


def test_stopping_without_an_encoder_does_nothing():
    asyncio.run(av_passive._stop_task())
    assert av_passive._task is None


def test_free_port_is_a_bindable_loopback_port():
    port = av_passive._free_port()
    assert 0 < port < 65536


def test_pcm_from_the_panel_is_capped_at_400_ms():
    av_passive._tap(b"\x01" * av_passive._CHUNK * 3)
    av_passive._tap(b"\x02" * av_passive._CHUNK * 3)
    assert len(av_passive._pcm) == 4 * av_passive._CHUNK
    assert av_passive._pcm[-1] == 2 and av_passive._pcm[0] == 1  # the oldest dropped


# ─── audio input ─────────────────────────────────────────────────────────────

def test_audio_input_connects_to_the_encoder_port():
    async def run():
        got = asyncio.Event()

        async def on_conn(reader, writer):
            got.set()
            writer.close()

        server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        w = await av_passive._audio_in(port)
        await asyncio.wait_for(got.wait(), 5)
        connected = got.is_set()
        w.close()
        server.close()
        await server.wait_closed()
        return connected

    assert asyncio.run(run())


def test_audio_input_gives_up_when_ffmpeg_never_listens(monkeypatch):
    tries = []

    async def refuse(host, port):
        tries.append(port)
        raise ConnectionRefusedError

    real_sleep = asyncio.sleep

    async def no_wait(t):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "open_connection", refuse)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    with pytest.raises(OSError, match="non ascolta su 40999"):
        asyncio.run(av_passive._audio_in(40999))
    assert len(tries) == 50


# ─── the encoder loop ────────────────────────────────────────────────────────

def _run_env(monkeypatch, audio=None, audio_error=None):
    writer = audio or _Writer()

    async def audio_in(port):
        if audio_error:
            raise audio_error
        return writer

    monkeypatch.setattr(av_passive, "_audio_in", audio_in)
    return writer


def test_at_rest_the_encoder_gets_the_standby_and_silence(monkeypatch):
    writer = _run_env(monkeypatch)
    enc = _Proc()
    client: asyncio.Queue = asyncio.Queue()
    av_passive._clients.add(client)

    async def run():
        enc.stdout.chunks = [b"\x47ts"]
        task = asyncio.create_task(av_passive._run(enc, b"STANDBY", lambda: False, lambda: None, 1))
        while len(enc.stdin.writes) < 2:
            await asyncio.sleep(0.01)
        enc.stdout.eof.set()  # the encoder exits on its own
        await asyncio.wait_for(task, 5)

    asyncio.run(run())
    assert set(enc.stdin.writes) == {b"STANDBY"}
    assert writer.data[0] == av_passive._SILENCE and writer.closed
    assert client.get_nowait() == b"\x47ts"
    assert client.get_nowait() is None, "a dead encoder ends its clients"
    assert enc.killed and enc.stdin.closed
    assert media.pcm_taps == []


def test_while_live_the_encoder_gets_the_panel_frame_and_its_audio(monkeypatch):
    writer = _run_env(monkeypatch)
    enc = _Proc()
    decoders = []

    async def fake_decode(is_live, on_live):
        decoders.append(1)
        av_passive._live = b"LIVE"
        await asyncio.Event().wait()

    monkeypatch.setattr(av_passive, "_decode", fake_decode)
    pcm = b"\x05\x00" * (av_passive._CHUNK // 4)  # half a chunk

    async def run():
        task = asyncio.create_task(av_passive._run(enc, b"STANDBY", lambda: True, lambda: None, 1))
        await asyncio.sleep(0)
        for tap in list(media.pcm_taps):
            tap(pcm)
        while b"LIVE" not in enc.stdin.writes:
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert decoders == [1], "one decoder while it keeps running"
    first_audio = next(d for d in writer.data if d != av_passive._SILENCE)
    assert first_audio == pcm + av_passive._SILENCE[len(pcm):], "missing audio is silence"
    assert enc.killed and media.pcm_taps == []


def test_a_cancelled_encoder_loop_leaves_the_clients_alone(monkeypatch):
    _run_env(monkeypatch)
    enc = _Proc()
    client: asyncio.Queue = asyncio.Queue()
    av_passive._clients.add(client)

    async def run():
        task = asyncio.create_task(av_passive._run(enc, b"S", lambda: False, lambda: None, 1))
        while not enc.stdin.writes:
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert client.empty() and client in av_passive._clients


def test_an_encoder_that_never_listens_ends_the_clients(monkeypatch):
    _run_env(monkeypatch, audio_error=OSError("ffmpeg non ascolta"))
    enc = _Proc()
    enc.returncode = 1  # already gone: no kill
    client: asyncio.Queue = asyncio.Queue()
    av_passive._clients.add(client)
    asyncio.run(av_passive._run(enc, b"S", lambda: False, lambda: None, 1))
    assert client.get_nowait() is None and not enc.killed and enc.stdin.closed


# ─── the live decoder ────────────────────────────────────────────────────────

def test_the_decoder_keeps_the_latest_panel_frame_until_the_video_ends(monkeypatch):
    frame = b"F" * 8
    dec = _Proc(stdout=_Stdout(frames=[frame]))
    _spawner(monkeypatch, dec)
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(b"\x47ts")
    q.put_nowait(None)
    subs = [None, q]
    unsubscribed = []

    async def av_subscribe():
        return subs.pop(0)

    async def av_unsubscribe(queue):
        unsubscribed.append(queue)

    real_sleep = asyncio.sleep

    async def no_wait(t):
        await real_sleep(0)

    monkeypatch.setattr(av_stream, "av_subscribe", av_subscribe)
    monkeypatch.setattr(av_stream, "av_unsubscribe", av_unsubscribe)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    seen_live = []
    live = iter([True, True])

    def is_live():
        seen_live.append(av_passive._live)
        return next(live, False)

    keyframes = []
    asyncio.run(av_passive._decode(is_live, lambda: keyframes.append(1)))
    assert seen_live[-1] == frame, "the decoded frame is what the encoder shows"
    assert av_passive._live is None, "at the end of the video: back to standby"
    assert keyframes == [1] and unsubscribed == [q]
    assert dec.stdin.writes == [b"\x47ts"] and dec.stdin.closed
    assert dec.killed
    assert "-an" in dec.args and "mpegts" in dec.args


def test_a_stuck_decoder_does_not_hang_the_live_view(monkeypatch):
    dec = _Proc(stuck=True)
    _spawner(monkeypatch, dec)
    q: asyncio.Queue = asyncio.Queue()

    async def av_subscribe():
        return q

    async def av_unsubscribe(queue):
        pass

    monkeypatch.setattr(av_stream, "av_subscribe", av_subscribe)
    monkeypatch.setattr(av_stream, "av_unsubscribe", av_unsubscribe)
    live = iter([True])
    asyncio.run(av_passive._decode(lambda: next(live, False), lambda: None))
    assert dec.killed and av_passive._live is None


def test_a_decoder_that_already_exited_is_not_killed(monkeypatch):
    dec = _Proc()
    dec.returncode = 0
    _spawner(monkeypatch, dec)

    async def av_subscribe():
        return asyncio.Queue()

    async def av_unsubscribe(queue):
        pass

    monkeypatch.setattr(av_stream, "av_subscribe", av_subscribe)
    monkeypatch.setattr(av_stream, "av_unsubscribe", av_unsubscribe)
    live = iter([True])
    asyncio.run(av_passive._decode(lambda: next(live, False), lambda: None))
    assert not dec.killed


def test_stopping_an_encoder_that_already_exited_does_not_kill_it(monkeypatch):
    enc = _Proc()
    enc.returncode = 1

    async def run():
        av_passive._task = asyncio.create_task(asyncio.sleep(0))
        av_passive._enc = enc
        await av_passive._stop_task()

    asyncio.run(run())
    assert not enc.killed and av_passive._enc is None


def test_the_feeder_stops_quietly_on_a_broken_pipe():
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(b"x")
    stdin = _Stdin(broken=True)
    asyncio.run(av_passive._feed(q, stdin))
    assert stdin.closed
