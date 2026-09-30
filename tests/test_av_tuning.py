"""/av tuning carried over from the fork: the ffmpeg input options, waiting for
ffmpeg's UDP ports instead of a fixed 0.3 s, and a grace before stopping ffmpeg
when the last client leaves during a call. No real ffmpeg: processes are faked."""
from __future__ import annotations

import asyncio
import os
import socket
import types

import pytest

from custom_components.vimar_intercom import av_stream as av
from custom_components.vimar_intercom import media_handler as media

PORTS = {av.FFMPEG_AV_VIDEO_PORT, av.FFMPEG_AV_AUDIO_PORT}


class FakeProc:
    def __init__(self, cmd=None, **kw):
        self.cmd = cmd
        self.rc = None
        r, w = os.pipe()
        self._w = w
        self.stdout = os.fdopen(r, "rb")
        self.stderr = types.SimpleNamespace(readline=lambda: b"", close=lambda: None)
        self.killed = False

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True
        self.rc = -9
        os.close(self._w)

    def wait(self, t=None):
        return self.rc


class FakeVideo:
    def __init__(self, live=True):
        self.remote_addr = ("192.0.2.9", 9200) if live else None
        self.forward_av = False
        self.av_rtp = None
        self.replayed = 0

    def sps_pps(self):
        return None

    def replay_gop(self):
        self.replayed += 1


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(av, "av_ffmpeg_proc", None)
    monkeypatch.setattr(av, "_stderr_task", None)
    monkeypatch.setattr(av, "_av_pump", None)
    monkeypatch.setattr(av, "_idle_stop", None)
    monkeypatch.setattr(av, "_av_clients", set())
    monkeypatch.setattr(av, "_av_lock", asyncio.Lock())
    monkeypatch.setattr(av, "_write_av_sdp", lambda: "x.sdp")
    monkeypatch.setattr(media, "audio_proto", None)
    procs = []

    def popen(cmd, **kw):
        procs.append(FakeProc(cmd))
        return procs[-1]

    monkeypatch.setattr(av.subprocess, "Popen", popen)
    return procs


# ─── the ffmpeg command ──────────────────────────────────────────────────────

def test_the_input_has_a_large_receive_buffer_and_no_mux_preload(monkeypatch, fresh):
    async def listening(proc, timeout=0.5):
        return True

    monkeypatch.setattr(av, "_wait_until_ffmpeg_listens", listening)
    monkeypatch.setattr(media, "video_proto", None)

    async def run():
        await av._start_av_ffmpeg_locked()
        await av._stop_av_ffmpeg_locked()

    asyncio.run(run())
    cmd = fresh[0].cmd
    i = cmd.index("-i")
    assert cmd[cmd.index("-buffer_size") + 1] == "655360"
    assert cmd.index("-buffer_size") < i, "an input option: before -i"
    assert cmd[cmd.index("-muxpreload") + 1] == "0"
    assert cmd[cmd.index("-muxdelay") + 1] == "0"


# ─── waiting for ffmpeg's ports ──────────────────────────────────────────────

def test_bound_ports_are_read_from_proc_net_udp(monkeypatch, tmp_path):
    udp = tmp_path / "udp"
    udp.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"
        "   1: 0100007F:4B1A 00000000:0000 07 00000000:00000000 00:00000000 00000000     0\n"
        "   2: 00000000:1C20 00000000:0000 07 00000000:00000000 00:00000000 00000000     0\n")
    monkeypatch.setattr(av, "_PROC_NET_UDP", (str(udp), str(tmp_path / "missing")))
    assert av._bound_udp_ports() == {0x4B1A, 7200}
    monkeypatch.setattr(av, "_PROC_NET_UDP", (str(tmp_path / "missing"),))
    assert av._bound_udp_ports() is None


@pytest.mark.skipif(not os.path.exists("/proc/net/udp"), reason="Linux /proc only")
def test_a_real_bound_socket_is_seen():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        assert s.getsockname()[1] in av._bound_udp_ports()
    finally:
        s.close()


def test_forwarding_starts_as_soon_as_the_ports_are_bound(monkeypatch, fresh):
    vp = FakeVideo()
    monkeypatch.setattr(media, "video_proto", vp)
    reads = []

    def bound():
        reads.append(vp.forward_av)
        return PORTS if len(reads) >= 3 else set()

    monkeypatch.setattr(av, "_bound_udp_ports", bound)

    async def run():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await av._start_av_ffmpeg_locked()
        elapsed = loop.time() - t0
        await av._stop_av_ffmpeg_locked()
        return elapsed

    elapsed = asyncio.run(run())
    assert reads == [False, False, False], "no RTP before ffmpeg listens"
    assert vp.replayed == 1, "then the cached GOP is replayed"
    assert elapsed < 0.25, f"no fixed 0.3 s wait any more ({elapsed:.3f} s)"


def test_on_timeout_it_forwards_anyway(monkeypatch, fresh):
    vp = FakeVideo()
    monkeypatch.setattr(media, "video_proto", vp)
    monkeypatch.setattr(av, "_bound_udp_ports", set)
    monkeypatch.setattr(av._wait_until_ffmpeg_listens, "__defaults__", (0.05,))

    async def run():
        await av._start_av_ffmpeg_locked()
        forwarding = vp.forward_av
        await av._stop_av_ffmpeg_locked()
        return forwarding

    assert asyncio.run(run()) is True
    assert vp.replayed == 1


def test_without_proc_net_udp_the_old_fixed_wait_is_kept(monkeypatch):
    slept = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(av, "_bound_udp_ports", lambda: None)
    monkeypatch.setattr(av.asyncio, "sleep", sleep)
    assert asyncio.run(av._wait_until_ffmpeg_listens(FakeProc())) is False
    assert slept == [0.3]


def test_an_ffmpeg_that_exits_ends_the_wait(monkeypatch):
    proc = FakeProc()
    calls = []

    def bound():
        calls.append(1)
        proc.rc = 1  # exits (bind failed, say) while we wait
        return set()

    monkeypatch.setattr(av, "_bound_udp_ports", bound)
    assert asyncio.run(av._wait_until_ffmpeg_listens(proc, timeout=5)) is False
    assert calls == [1]


# ─── the grace after the last client ─────────────────────────────────────────

@pytest.fixture
def grace(monkeypatch, fresh):
    monkeypatch.setattr(av, "AV_IDLE_GRACE", 0.05)

    async def listening(proc, timeout=0.5):
        return True

    monkeypatch.setattr(av, "_wait_until_ffmpeg_listens", listening)
    return fresh


def test_the_last_client_leaving_during_the_call_starts_the_grace(monkeypatch, grace):
    monkeypatch.setattr(media, "video_proto", FakeVideo(live=True))

    async def run():
        q = await av.av_subscribe()
        await av.av_unsubscribe(q)
        straight_after = grace[0].killed
        await asyncio.sleep(0.15)
        return straight_after

    assert asyncio.run(run()) is False, "not stopped as the last client leaves"
    assert grace[0].killed, "stopped once the grace is over"
    assert av.av_ffmpeg_proc is None


def test_a_client_back_within_the_grace_finds_ffmpeg_running(monkeypatch, grace):
    monkeypatch.setattr(media, "video_proto", FakeVideo(live=True))

    async def run():
        q = await av.av_subscribe()
        await av.av_unsubscribe(q)
        await asyncio.sleep(0.01)
        q2 = await av.av_subscribe()
        await asyncio.sleep(0.15)  # past the first grace
        alive = not grace[0].killed
        await av.stop_av_ffmpeg()
        return q2, alive

    q2, alive = asyncio.run(run())
    assert q2 is not None and alive
    assert len(grace) == 1, "one ffmpeg, reused"


def test_without_the_calls_video_it_stops_at_once(monkeypatch, grace):
    monkeypatch.setattr(media, "video_proto", FakeVideo(live=False))

    async def run():
        q = await av.av_subscribe()
        await av.av_unsubscribe(q)
        return grace[0].killed

    assert asyncio.run(run()) is True
    assert av._idle_stop is None


def test_the_end_of_the_call_stops_ffmpeg_during_the_grace(monkeypatch, grace):
    monkeypatch.setattr(media, "video_proto", FakeVideo(live=True))
    monkeypatch.setattr(av, "AV_IDLE_GRACE", 10.0)

    async def run():
        q = await av.av_subscribe()
        await av.av_unsubscribe(q)
        task = av._idle_stop
        await av.stop_av_ffmpeg()  # what media.stop_media does at the end of the call
        await asyncio.sleep(0)
        return task

    task = asyncio.run(run())
    assert grace[0].killed
    assert task.cancelled() and av._idle_stop is None


def test_the_output_timestamps_never_start_below_zero(monkeypatch, fresh):
    """AAC's priming puts the first audio packet at -1920 (90 kHz) when the
    audio leads the video; wrapped in MPEG-TS, HA's stream worker saw
    "Timestamp discontinuity detected: last dts = 8589932672, dts = 0"."""
    async def listening(proc, timeout=0.5):
        return True

    monkeypatch.setattr(av, "_wait_until_ffmpeg_listens", listening)
    monkeypatch.setattr(media, "video_proto", None)

    async def run():
        await av._start_av_ffmpeg_locked()
        await av._stop_av_ffmpeg_locked()

    asyncio.run(run())
    cmd = fresh[0].cmd
    offset = float(cmd[cmd.index("-output_ts_offset") + 1])
    assert offset * 90000 > 1920, "more than the AAC priming"
    assert cmd.index("-i") < cmd.index("-output_ts_offset") < cmd.index("-f"), "an output option"
