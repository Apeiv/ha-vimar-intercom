"""The TLS framer and the relay's keepalive pongs.

A lone CRLF (the keepalive pong, RFC 5626 §4.4.1) used to stay in the buffer
in front of the next message, which then lost its first line and was dropped.
"""
import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")

OPTIONS = b"OPTIONS sip:a SIP/2.0\r\nCall-ID: x\r\nContent-Length: 0\r\n\r\n"


def msg(body=b""):
    return (b"MESSAGE sip:a SIP/2.0\r\nCall-ID: m\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body)


def test_a_keepalive_pong_does_not_eat_the_next_message():
    out, rest = sip._split_stream(b"\r\n" + OPTIONS)
    assert out and out[0].startswith("OPTIONS") and rest == b""


def test_pongs_between_messages_are_skipped():
    out, _rest = sip._split_stream(OPTIONS + b"\r\n\r\n" + msg(b"hello"))
    assert [m.split(" ", 1)[0] for m in out] == ["OPTIONS", "MESSAGE"]


def test_a_partial_message_waits_for_the_rest():
    out, rest = sip._split_stream(msg(b"0123456789")[:-3])
    assert out == [] and rest.endswith(b"0123456")
