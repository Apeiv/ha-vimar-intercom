"""The group coming out of the transcoder: where a new view starts.

A view that opens first receives the last transcoded group, from the SPS on.
It must be recognized correctly: if the group does not yet hold a whole
keyframe, the phone has nothing to decode.
"""
import struct

import pytest

tc = pytest.importorskip("custom_components.vimar_intercom.homekit_transcode")


def rtp(payload: bytes, seq: int = 1) -> bytes:
    return bytes([0x80, 96]) + struct.pack("!HII", seq, 0, 1) + payload


STAP_SPS_PPS = rtp(b"\x78" + b"\x00\x02\x67\x42" + b"\x00\x02\x68\xce")
IDR_START = rtp(b"\x7c\x85" + b"\xaa" * 20)
IDR_MID = rtp(b"\x7c\x05" + b"\xbb" * 20)
IDR_END = rtp(b"\x7c\x45" + b"\xcc" * 20)
IDR_SINGLE = rtp(b"\x65" + b"\xdd" * 20)
P_FRAME = rtp(b"\x41" + b"\xee" * 20)


class TestNalTypes:
    def test_single(self):
        assert tc._nal_types(P_FRAME) == [1]

    def test_stap_a_looks_inside(self):
        assert tc._nal_types(STAP_SPS_PPS) == [7, 8]

    def test_fu_a_reports_the_fragmented_type(self):
        assert tc._nal_types(IDR_START) == [5]


class TestEncodedGop:
    def test_a_whole_fragmented_keyframe_counts(self):
        g = tc.EncodedGop()
        for p in (STAP_SPS_PPS, IDR_START, IDR_MID):
            g.add(p)
        assert not g.has_keyframe, "the end of the IDR is still missing"
        g.add(IDR_END)
        assert g.has_keyframe

    def test_an_unfragmented_keyframe_counts(self):
        g = tc.EncodedGop()
        g.add(STAP_SPS_PPS)
        g.add(IDR_SINGLE)
        assert g.has_keyframe

    def test_the_group_restarts_at_every_sps(self):
        g = tc.EncodedGop()
        for p in (STAP_SPS_PPS, IDR_SINGLE, P_FRAME, P_FRAME):
            g.add(p)
        g.add(STAP_SPS_PPS)
        assert g.packets == [STAP_SPS_PPS] and not g.has_keyframe

    def test_a_group_that_never_ends_is_capped(self):
        """An encoder that stops sending keyframes made the group grow for the
        whole call. Past the cap it is dropped until the next SPS."""
        g = tc.EncodedGop()
        g.add(STAP_SPS_PPS)
        g.add(IDR_SINGLE)
        for _ in range(tc.GOP_MAX_PACKETS + 10):
            g.add(P_FRAME)
        assert len(g.packets) <= tc.GOP_MAX_PACKETS
        assert not g.has_keyframe, "what is left has no keyframe to start from"
        g.add(STAP_SPS_PPS)
        g.add(IDR_SINGLE)
        assert g.packets == [STAP_SPS_PPS, IDR_SINGLE] and g.has_keyframe

    def test_a_tail_without_its_start_is_not_a_keyframe(self):
        """Only the end of the IDR (someone lost the start): it does not count."""
        g = tc.EncodedGop()
        g.add(STAP_SPS_PPS)
        g.add(IDR_END)
        assert not g.has_keyframe


class FakeFfmpeg:
    returncode = None
    stderr = None

    def terminate(self):
        self.returncode = -15

    kill = terminate

    async def wait(self):
        return self.returncode


def test_the_panel_video_is_read_on_loopback_from_a_private_sdp(monkeypatch):
    """ffmpeg 8.1 bound the RTCP port of an SDP input on 0.0.0.0, and the SDP
    had a guessable name in /tmp that open() would follow as a symlink."""
    import asyncio
    import os

    seen = {}

    async def spawn(*args, **_kw):
        path = args[args.index("-i") + 1]
        seen["args"], seen["mode"] = args, os.stat(path).st_mode & 0o777
        with open(path) as f:
            seen["sdp"] = f.read()
        return FakeFfmpeg()

    monkeypatch.setattr(tc.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tc, "port_bound", lambda _port: True)

    async def scenario():
        t = tc.Transcoder(None, None)
        assert await t.start(lambda: ([], []))
        path = seen["args"][seen["args"].index("-i") + 1]
        await t.stop()
        return t, path

    t, path = asyncio.run(scenario())
    i = seen["args"].index("-i")
    assert seen["args"][i - 2:i] == ("-localaddr", "127.0.0.1")
    assert seen["mode"] == 0o600 and f"/vimar_intercom_transcode_{t._in_port}.sdp" not in path
    assert f"m=video {t._in_port} " in seen["sdp"]
    assert not os.path.exists(path)


def test_only_our_encoder_may_feed_the_phones():
    t = tc.Transcoder(None, None)
    got = []
    t.add_sink(got.append)
    pkt = rtp(b"\x41" + b"\x00" * 10)
    t._from_encoder(pkt, ("127.0.0.1", 40000))
    t._from_encoder(pkt, ("127.0.0.1", 40001))   # another local process
    t._feed.close()
    assert got == [pkt]
