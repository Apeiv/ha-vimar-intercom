"""SRTP and the RTCP probe at their edges: suites, keys, header extensions,
short packets, an SRTCP decoder, a port that does not bind."""
from __future__ import annotations

import asyncio
import base64
import logging
import struct

import pytest

from custom_components.vimar_intercom import rtcp
from custom_components.vimar_intercom.srtp import SRTPContext

KEY = base64.b64encode(bytes(range(30))).decode()


def _rtp(seq: int, payload: bytes = b"voice-payload", ssrc: int = 0xDEADBEEF, first: int = 0x80) -> bytes:
    return bytes([first, 0x00]) + seq.to_bytes(2, "big") + bytes(4) + ssrc.to_bytes(4, "big") + payload


# ─── SRTP ─────────────────────────────────────────────────────────────────────

def test_an_unknown_crypto_suite_is_refused():
    with pytest.raises(ValueError, match="unsupported SRTP suite"):
        SRTPContext(KEY, suite="AES_256_CM_HMAC_SHA1_80")


def test_a_key_shorter_than_30_bytes_is_refused():
    with pytest.raises(ValueError, match="too short"):
        SRTPContext(base64.b64encode(bytes(20)).decode())


def test_the_32_bit_suite_appends_a_4_byte_tag_and_round_trips():
    tx = SRTPContext(KEY, suite="AES_CM_128_HMAC_SHA1_32")
    rx = SRTPContext(KEY, suite="AES_CM_128_HMAC_SHA1_32")
    pkt = _rtp(7)
    out = tx.protect(pkt)
    assert len(out) == len(pkt) + 4
    assert rx.unprotect(out) == pkt


def test_a_header_extension_stays_in_clear_and_round_trips():
    # X bit set, one 32-bit word of extension after the fixed header.
    ext = struct.pack("!HH", 0xBEDE, 1) + b"\x10\xAA\x00\x00"
    pkt = _rtp(3, payload=ext + b"secret-audio", first=0x90)
    out = SRTPContext(KEY).protect(pkt)
    assert out[:12 + 8] == pkt[:12 + 8], "header and extension are not encrypted"
    assert b"secret-audio" not in out
    assert SRTPContext(KEY).unprotect(out) == pkt


def test_an_extension_flag_without_room_for_the_extension_still_round_trips():
    # X bit set but only 4 bytes after the header: too short to hold an
    # extension, so they are treated as payload on both sides.
    pkt = _rtp(4, payload=b"abcd", first=0x90)
    out = SRTPContext(KEY).protect(pkt)
    assert out[12:16] != b"abcd"
    assert SRTPContext(KEY).unprotect(out) == pkt


def test_a_packet_whose_csrc_list_leaves_no_payload_is_dropped():
    # CC=15 announces 60 bytes of CSRC: the packet is shorter than its own header.
    ctx = SRTPContext(KEY)
    pkt = ctx.protect(_rtp(1, payload=b"x" * 8, first=0x8F))
    assert SRTPContext(KEY).unprotect(pkt) is None


def test_a_late_packet_still_decrypts_and_the_stream_goes_on():
    tx, rx = SRTPContext(KEY), SRTPContext(KEY)
    p10, p9, p11 = (tx.protect(_rtp(s)) for s in (10, 9, 11))
    assert rx.unprotect(p10) == _rtp(10)
    assert rx.unprotect(p9) == _rtp(9), "an out-of-order packet is not an auth failure"
    assert rx.unprotect(p11) == _rtp(11)


# ─── RTCP probe ───────────────────────────────────────────────────────────────

SR = struct.pack("!BBHI", 0x80, 200, 6, 42) + bytes(20)


class _Decoder:
    """Stands in for an SRTCP decoder: 'good' packets decrypt to a plain SR."""

    def __init__(self, key):
        self.key = key

    def unprotect(self, data):
        return SR if data.endswith(b"good") else None


def test_an_srtcp_decoder_decrypts_the_records_and_counts_failures(monkeypatch, caplog):
    monkeypatch.setattr(rtcp.srtp, "SRTCPContext", _Decoder, raising=False)
    probe = rtcp.RTCPProbe("audio", encrypted=True, key="k")
    assert probe.srtcp_rx.key == "k"
    with caplog.at_level(logging.DEBUG, logger="custom_components.vimar_intercom.rtcp"):
        probe.datagram_received(SR + b"bad", ("192.0.2.1", 55731))
        probe.datagram_received(SR + b"good", ("192.0.2.1", 55731))
    assert "[srtcp ok=0 failed=1]" in caplog.text
    assert "[srtcp ok=1 failed=1]: SR" in caplog.text


def test_a_decoder_that_rejects_the_key_leaves_srtcp_undecoded(monkeypatch):
    def _bad(key):
        raise ValueError("bad key")
    monkeypatch.setattr(rtcp.srtp, "SRTCPContext", _bad, raising=False)
    probe = rtcp.RTCPProbe("video", encrypted=True, key="k")
    assert probe.srtcp_rx is None
    probe.datagram_received(SR, ("192.0.2.1", 57101))
    assert probe.count == 1 and probe.remote_ssrc == 42


def test_a_runt_packet_keeps_the_last_ssrc():
    probe = rtcp.RTCPProbe("audio")
    probe.datagram_received(SR, ("192.0.2.1", 1))
    probe.datagram_received(b"\x80", ("192.0.2.2", 2))
    assert probe.count == 2 and probe.remote_ssrc == 42
    assert probe.remote_addr == ("192.0.2.2", 2)


def test_after_the_first_packets_only_one_in_log_every_is_logged(caplog):
    probe = rtcp.RTCPProbe("audio")
    probe.count = rtcp.LOG_FIRST
    with caplog.at_level(logging.DEBUG, logger="custom_components.vimar_intercom.rtcp"):
        probe.datagram_received(SR, ("192.0.2.1", 1))
    assert caplog.text == ""
    probe.count = rtcp.LOG_EVERY - 1
    with caplog.at_level(logging.DEBUG, logger="custom_components.vimar_intercom.rtcp"):
        probe.datagram_received(SR, ("192.0.2.1", 1))
    assert f"#{rtcp.LOG_EVERY} " in caplog.text


def test_close_twice_and_punch_without_transport_do_nothing():
    probe = rtcp.RTCPProbe("audio")
    probe.close()
    rtcp.punch(probe, "192.0.2.1", 5000)
    rtcp.punch(None, "192.0.2.1", 5000)
    assert probe.transport is None


def test_a_failed_punch_is_logged_not_raised(caplog):
    class _T:
        def sendto(self, data, addr):
            raise OSError("network unreachable")

    probe = rtcp.RTCPProbe("audio")
    probe.transport = _T()
    with caplog.at_level(logging.DEBUG, logger="custom_components.vimar_intercom.rtcp"):
        rtcp.punch(probe, "192.0.2.1", 5000)
    assert "punch failed: network unreachable" in caplog.text


def test_a_port_that_does_not_bind_gives_no_probe(monkeypatch):
    closed = []

    class _Sock:
        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            raise OSError("address in use")

        def close(self):
            closed.append(True)

    async def _run():
        # Patched inside the loop: the loop's own sockets are already made.
        monkeypatch.setattr(rtcp.socket, "socket", lambda *a: _Sock())
        return await rtcp.open_probe("audio", 5001, ("192.0.2.1", 5000))

    assert asyncio.run(_run()) is None
    assert closed == [True]
