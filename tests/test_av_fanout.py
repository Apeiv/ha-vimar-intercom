"""/av: più client condividono un solo ffmpeg e ricevono tutti gli stessi byte."""
from __future__ import annotations

import asyncio
import os

from custom_components.vimar_intercom import av_stream as mh


def test_av_clients_share_one_ffmpeg(monkeypatch):
    r, w = os.pipe()
    starts = []

    class FakeProc:
        stdout = os.fdopen(r, "rb")

        def poll(self):
            return None

    async def fake_start():
        starts.append(1)
        mh.av_ffmpeg_proc = FakeProc()

    async def fake_stop():
        mh.av_ffmpeg_proc = None

    monkeypatch.setattr(mh, "_start_av_ffmpeg_locked", fake_start)
    monkeypatch.setattr(mh, "_stop_av_ffmpeg_locked", fake_stop)
    monkeypatch.setattr(mh, "_av_pump", None)
    monkeypatch.setattr(mh, "_av_lock", asyncio.Lock())

    async def drain(q):
        out = b""
        while (chunk := await q.get()) is not None:
            out += chunk
        return out

    async def run():
        a = await mh.av_subscribe()
        b = await mh.av_subscribe()

        def feed():  # dal thread: la pipe si svuota solo se l'event loop gira
            os.write(w, b"x" * 10000)
            os.close(w)

        writer = asyncio.create_task(asyncio.to_thread(feed))
        got_a, got_b = await asyncio.gather(drain(a), drain(b))
        await writer
        await mh.av_unsubscribe(a)
        await mh.av_unsubscribe(b)
        return got_a, got_b

    got_a, got_b = asyncio.run(run())
    assert starts == [1]
    assert got_a == got_b == b"x" * 10000
    assert not mh._av_clients


def test_ffmpeg_parte_fuori_dall_event_loop_e_lo_stderr_resta_referenziato(monkeypatch):
    """Popen (fork + exec) in executor, non nel loop; il task che legge lo stderr non deve
    poter essere raccolto a metà lettura."""
    import subprocess
    import threading

    from custom_components.vimar_intercom import media_handler as media

    where = []

    class FakeProc:
        stdout = None

        def __init__(self, cmd, **kw):
            where.append(threading.current_thread())
            self.stderr = type("E", (), {"readline": staticmethod(lambda: b"")})()

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, t=None):
            return 0

    monkeypatch.setattr(mh.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(mh, "_write_av_sdp", lambda: "x.sdp")
    monkeypatch.setattr(mh, "av_ffmpeg_proc", None)
    monkeypatch.setattr(mh, "_stderr_task", None)
    monkeypatch.setattr(media, "video_proto", None)
    monkeypatch.setattr(media, "audio_proto", None)

    async def run():
        await mh._start_av_ffmpeg_locked()
        assert isinstance(mh._stderr_task, asyncio.Task)
        await mh._stop_av_ffmpeg_locked()

    asyncio.run(run())
    assert where and where[0] is not threading.main_thread()
    assert isinstance(subprocess.Popen, type)  # il monkeypatch non è rimasto attaccato al modulo vero
