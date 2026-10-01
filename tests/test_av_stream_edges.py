"""/av (av_stream) where things go wrong: ffmpeg that does not start or exits at
once, a slow client, a pump that fails, a kill that fails, and the silence that
starts the AAC encoder. No real ffmpeg: processes are faked."""
from __future__ import annotations

import asyncio
import logging
import os
import stat
import struct
import types

import pytest

from custom_components.vimar_intercom import av_stream as av
from custom_components.vimar_intercom import media_handler as media


class _Stderr:
    def __init__(self, lines=(), error=None):
        self._lines = [line.encode() + b"\n" for line in lines]
        self._error = error

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._error:
            raise self._error
        return b""

    def close(self):
        pass


class _Proc:
    def __init__(self, rc=None, lines=(), stdout=None, kill_error=None):
        self.rc = rc
        self.stderr = _Stderr(lines)
        self.stdout = stdout
        self.killed = False
        self._kill_error = kill_error

    def poll(self):
        return self.rc

    def kill(self):
        if self._kill_error:
            raise self._kill_error
        self.killed = True
        self.rc = -9

    def wait(self, t=None):
        return self.rc


class _Sock:
    def __init__(self, error=None):
        self.sent = []
        self._error = error

    def sendto(self, data, addr):
        if self._error:
            raise self._error
        self.sent.append((data, addr))


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(av, "av_ffmpeg_proc", None)
    monkeypatch.setattr(av, "_stderr_task", None)
    monkeypatch.setattr(av, "_av_pump", None)
    monkeypatch.setattr(av, "_idle_stop", None)
    monkeypatch.setattr(av, "_av_stopping", False)
    monkeypatch.setattr(av, "_av_clients", set())
    monkeypatch.setattr(av, "_av_lock", asyncio.Lock())
    monkeypatch.setattr(av, "_write_av_sdp", lambda: "x.sdp")
    monkeypatch.setattr(media, "audio_proto", None)
    monkeypatch.setattr(media, "video_proto", None)


def _popen(monkeypatch, *procs):
    queue = list(procs)

    def popen(cmd, **kw):
        proc = queue.pop(0)
        if isinstance(proc, Exception):
            raise proc
        return proc

    monkeypatch.setattr(av.subprocess, "Popen", popen)


# ─── the silence that starts the AAC encoder ─────────────────────────────────

def test_three_silent_packets_follow_the_start_of_ffmpeg():
    audio = types.SimpleNamespace(ffmpeg_av_sock=_Sock(), av_rtp=av.AvRtp(160))
    av._seed_silence(audio)
    assert len(audio.ffmpeg_av_sock.sent) == 3
    for i, (pkt, addr) in enumerate(audio.ffmpeg_av_sock.sent):
        assert addr == ("127.0.0.1", av.FFMPEG_AV_AUDIO_PORT)
        seq, ts = struct.unpack_from("!HI", pkt, 2)
        assert (seq, ts) == (i, i * 160) and pkt[1] & 0x7F == 0  # PCMU
        assert pkt[12:] == media.SILENCE_ULAW


def test_a_send_error_on_the_silence_is_ignored():
    audio = types.SimpleNamespace(ffmpeg_av_sock=_Sock(OSError("closed")), av_rtp=av.AvRtp(160))
    av._seed_silence(audio)  # does not raise
    assert audio.ffmpeg_av_sock.sent == []


def test_a_new_ffmpeg_forwards_the_call_audio_and_seeds_it(monkeypatch, fresh):
    audio = types.SimpleNamespace(ffmpeg_av_sock=_Sock(), av_rtp=None, forward_av=False)
    monkeypatch.setattr(media, "audio_proto", audio)
    _popen(monkeypatch, _Proc())

    async def listening(proc, timeout=0.5):
        return True

    monkeypatch.setattr(av, "_wait_until_ffmpeg_listens", listening)

    async def run():
        await av._start_av_ffmpeg_locked()
        forwarding = audio.forward_av
        await av._stop_av_ffmpeg_locked()
        return forwarding

    assert asyncio.run(run()) is True
    assert audio.av_rtp.ts_step == 160 and len(audio.ffmpeg_av_sock.sent) == 3
    assert audio.forward_av is False, "the stop turns the forwarding off"


# ─── ffmpeg that does not start ──────────────────────────────────────────────

def test_an_sdp_that_cannot_be_written_starts_no_ffmpeg(monkeypatch, fresh, caplog):
    def broken():
        raise OSError("read-only")

    monkeypatch.setattr(av, "_write_av_sdp", broken)
    _popen(monkeypatch)  # any Popen would fail the pop
    with caplog.at_level(logging.ERROR, logger=av.__name__):
        asyncio.run(av._start_av_ffmpeg_locked())
    assert av.av_ffmpeg_proc is None
    assert "AV SDP write error: read-only" in caplog.text


def test_an_ffmpeg_that_cannot_be_launched_leaves_no_process(monkeypatch, fresh, caplog):
    _popen(monkeypatch, FileNotFoundError("ffmpeg"))
    with caplog.at_level(logging.ERROR, logger=av.__name__):
        assert asyncio.run(av.av_subscribe()) is None
    assert av.av_ffmpeg_proc is None and not av._av_clients
    assert "AV ffmpeg start error" in caplog.text


def test_an_ffmpeg_that_exits_at_once_reports_why(monkeypatch, fresh, caplog):
    proc = _Proc(rc=1, lines=["[udp @ 0x1] bind failed: Address in use"])
    _popen(monkeypatch, proc)

    async def not_listening(proc, timeout=0.5):
        return False

    monkeypatch.setattr(av, "_wait_until_ffmpeg_listens", not_listening)
    with caplog.at_level(logging.ERROR, logger=av.__name__):
        assert asyncio.run(av.av_subscribe()) is None, "no client for a dead ffmpeg"
    assert "exited immediately during startup (rc=1): [udp @ 0x1] bind failed" in caplog.text


def test_a_failed_stderr_reader_does_not_hide_the_exit(monkeypatch, fresh, caplog):
    _popen(monkeypatch, _Proc(rc=1))

    async def not_listening(proc, timeout=0.5):
        return False

    async def broken_reader(proc, tail):
        raise RuntimeError("reader died")

    monkeypatch.setattr(av, "_wait_until_ffmpeg_listens", not_listening)
    monkeypatch.setattr(av, "_read_av_ffmpeg_stderr", broken_reader)
    with caplog.at_level(logging.ERROR, logger=av.__name__):
        asyncio.run(av._start_av_ffmpeg_locked())
    assert "exited immediately during startup (rc=1): no stderr output" in caplog.text


# ─── clients ─────────────────────────────────────────────────────────────────

def test_ending_a_full_client_still_delivers_the_end():
    clients: set = set()
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    q.put_nowait(b"a")
    q.put_nowait(b"b")
    clients.add(q)
    av.end_client(q, clients)
    assert q not in clients
    assert [q.get_nowait(), q.get_nowait()] == [b"b", None]


def test_a_client_too_slow_for_the_fanout_is_ended_not_corrupted():
    clients: set = set()
    slow: asyncio.Queue = asyncio.Queue(maxsize=1)
    slow.put_nowait(b"old")
    fast: asyncio.Queue = asyncio.Queue()
    clients.update({slow, fast})
    av.fanout(b"new", clients)
    assert clients == {fast} and fast.get_nowait() == b"new"
    assert slow.get_nowait() is None, "no partial TS: the slow client just ends"


class _Stdout:
    def __init__(self, error):
        self._error = error

    def read1(self, n):
        raise self._error


def test_a_failing_pump_ends_its_clients(fresh):
    q: asyncio.Queue = asyncio.Queue()
    av._av_clients.add(q)
    proc = _Proc(stdout=_Stdout(ValueError("read of closed file")))

    async def run():
        av._av_pump = asyncio.current_task()
        await av._av_pump_run(proc)

    asyncio.run(run())
    assert q.get_nowait() is None and not av._av_clients


def test_an_old_pump_leaves_the_new_ffmpegs_clients_alone(fresh):
    q: asyncio.Queue = asyncio.Queue()
    av._av_clients.add(q)
    proc = _Proc(stdout=_Stdout(OSError("closed")))
    asyncio.run(av._av_pump_run(proc))  # _av_pump is another task (None here)
    assert q.empty() and q in av._av_clients


# ─── stop ────────────────────────────────────────────────────────────────────

def test_a_failing_kill_still_ends_the_clients(monkeypatch, fresh):
    proc = _Proc(kill_error=ProcessLookupError())
    monkeypatch.setattr(av, "av_ffmpeg_proc", proc)
    q: asyncio.Queue = asyncio.Queue()
    av._av_clients.add(q)
    asyncio.run(av.stop_av_ffmpeg())
    assert av.av_ffmpeg_proc is None
    assert q.get_nowait() is None and not av._av_clients


def test_the_grace_ends_quietly_when_a_client_came_back(monkeypatch, fresh):
    proc = _Proc()
    monkeypatch.setattr(av, "av_ffmpeg_proc", proc)
    monkeypatch.setattr(av, "AV_IDLE_GRACE", 0)
    av._av_clients.add(asyncio.Queue())

    async def run():
        av._idle_stop = asyncio.current_task()
        await av._stop_when_idle()

    asyncio.run(run())
    assert not proc.killed and av.av_ffmpeg_proc is proc


# ─── stderr and /proc ────────────────────────────────────────────────────────

def test_the_stderr_reader_ends_on_a_read_error_and_skips_blank_lines(fresh):
    proc = _Proc()
    proc.stderr = _Stderr(["   ", "Error opening input"], error=ValueError("closed"))
    tail: list = []
    asyncio.run(av._read_av_ffmpeg_stderr(proc, tail))
    assert tail == ["Error opening input"]


def test_malformed_proc_net_udp_lines_are_skipped(monkeypatch, tmp_path):
    udp = tmp_path / "udp"
    udp.write_text(
        "  sl  local_address\n"
        "   1:\n"
        "   2: nocolon 00000000:0000\n"
        "   3: 0100007F:ZZZZ 00000000:0000\n"
        "   4: 0100007F:1C20 00000000:0000\n")
    monkeypatch.setattr(av, "_PROC_NET_UDP", (str(udp),))
    assert av._bound_udp_ports() == {7200}


# ─── the SDP file is private and does not outlive ffmpeg ─────────────────────

def test_the_sdp_is_a_private_file_with_an_unpredictable_name(monkeypatch):
    monkeypatch.setattr(media, "video_proto", None)
    paths = [av._write_av_sdp() for _ in range(2)]
    try:
        assert paths[0] != paths[1]
        for p in paths:
            assert "m=video" in open(p).read()
            assert os.path.basename(p) != "vimar_intercom_av.sdp"
            if os.name != "nt":
                assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    finally:
        for p in paths:
            os.unlink(p)


def test_restarting_the_av_ffmpeg_removes_the_old_sdp(monkeypatch):
    monkeypatch.setattr(av, "av_ffmpeg_proc", None)
    monkeypatch.setattr(av, "_av_stopping", False)
    monkeypatch.setattr(av, "_av_clients", set())
    monkeypatch.setattr(av, "_stderr_task", None)
    monkeypatch.setattr(av, "_av_pump", None)
    monkeypatch.setattr(av, "_idle_stop", None)
    monkeypatch.setattr(av, "_av_lock", asyncio.Lock())
    monkeypatch.setattr(av, "_av_sdp_file", None)
    monkeypatch.setattr(media, "audio_proto", None)
    monkeypatch.setattr(media, "video_proto", None)
    _popen(monkeypatch, _Proc(), _Proc())

    async def listening(proc, timeout=0.5):
        return True

    monkeypatch.setattr(av, "_wait_until_ffmpeg_listens", listening)

    async def run():
        await av._start_av_ffmpeg_locked()
        first = av._av_sdp_file
        assert os.path.exists(first)
        await av._start_av_ffmpeg_locked()
        second = av._av_sdp_file
        assert second != first and os.path.exists(second)
        await av._stop_av_ffmpeg_locked()
        return first, second

    first, second = asyncio.run(run())
    assert not os.path.exists(first) and not os.path.exists(second)
    assert av._av_sdp_file is None
