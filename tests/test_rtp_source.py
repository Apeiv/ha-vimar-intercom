"""Plain RTP is accepted only from the other end of the call."""
import struct

import pytest

media = pytest.importorskip("custom_components.vimar_intercom.media_handler")


def pcmu(seq=1):
    return struct.pack("!BBHII", 0x80, 0, seq, 160 * seq, 0x5555) + b"\xff" * 160


def test_plain_audio_from_the_call_is_taken_and_from_elsewhere_dropped():
    proto = media.RTPAudioProtocol()
    proto.remote_addr = ("198.51.100.7", 40000)
    proto.datagram_received(pcmu(1), ("198.51.100.7", 40002))   # relay, other port
    proto.datagram_received(pcmu(2), ("192.0.2.66", 40000))     # someone else
    assert proto.pkt_count == 1


def test_no_call_no_rtp():
    proto = media.RTPAudioProtocol()
    proto.datagram_received(pcmu(1), ("198.51.100.7", 40000))
    assert proto.pkt_count == 0


def h264(seq=1):
    return struct.pack("!BBHII", 0x80, 96, seq, 3000 * seq, 0x7777) + b"\x41\x9a\x00\x10"


def test_plain_video_from_the_call_is_taken_and_from_elsewhere_dropped():
    proto = media.RTPVideoProtocol()
    proto.remote_addr = ("198.51.100.7", 40002)
    proto.frame_sink = lambda nal: None
    proto.datagram_received(h264(1), ("198.51.100.7", 40010))   # relay, other port
    proto.datagram_received(h264(2), ("192.0.2.66", 40002))     # someone else
    assert proto.pkt_count == 1


def test_no_call_no_video():
    proto = media.RTPVideoProtocol()
    proto.datagram_received(h264(1), ("198.51.100.7", 40002))
    assert proto.pkt_count == 0


def test_a_dropped_source_is_logged_once_per_call(caplog):
    """At INFO: a silent stream from a relay on another address must be
    explainable from a default log."""
    proto = media.RTPAudioProtocol()
    proto.remote_addr = ("198.51.100.7", 40000)
    with caplog.at_level("INFO", logger=media.__name__):
        for seq in range(1, 4):
            proto.datagram_received(pcmu(seq), ("192.0.2.66", 40000))
        assert caplog.text.count("192.0.2.66") == 1
        proto.remote_addr = tuple(["198.51.100.7", 40000])  # the next call (new tuple)
        proto.datagram_received(pcmu(5), ("192.0.2.66", 40000))
    assert caplog.text.count("192.0.2.66") == 2
