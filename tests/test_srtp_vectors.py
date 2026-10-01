"""Known-answer vectors for SRTP and SRTCP protect().

The per-packet code (IV, AES-CTR set-up, HMAC) was rewritten for speed. These
bytes were produced by the implementation it replaced, from the RFC 3711 B.3
master key: a change in the cipher, the counter, the IV layout or the tag
shows up here as a different packet, which the round-trip tests would not see.
"""
from __future__ import annotations

import base64
import struct

from custom_components.vimar_intercom.srtp import (
    SRTCPContext,
    SRTPContext,
    _iv_base,
    _packet_iv,
)

MASTER_KEY = bytes.fromhex("E1F97A0D3E018BE0D64FA32C06DE4139")
MASTER_SALT = bytes.fromhex("0EC675AD498AFEEBB6960B3AABE6")
KEY_B64 = base64.b64encode(MASTER_KEY + MASTER_SALT).decode()


def _rtp(seq: int, ssrc: int = 0xDEADBEEF, payload: bytes = b"payload-di-prova") -> bytes:
    return (bytes([0x80, 0x00]) + seq.to_bytes(2, "big") + (12345 * seq).to_bytes(4, "big")
            + ssrc.to_bytes(4, "big") + payload)


EXPECTED_80 = {
    1000: "800003e800bc5ea8deadbeefabc0381a129acddd7b7f21abae316573059d78d5f4c4b9d4ffd3",
    65535: "8000ffff3038cfc7deadbeefcc4338560527f71076e87742b0ee3622ae7801a455ba62d231e9",
    0: "8000000000000000deadbeef397daecd4aa747b71b8130c9382ec63562ec9acb5a442aabdd66",
    1: "8000000100003039deadbeefa5e886322134be8550888b3ce978a394840b72c797e04960f3e1",
}


def test_srtp_protect_matches_the_previous_implementation():
    tx = SRTPContext(KEY_B64)
    # In this order: the sequence wraps after 65535, so 0 and 1 get ROC 1.
    for seq, expected in EXPECTED_80.items():
        assert tx.protect(_rtp(seq)).hex() == expected, seq


def test_srtp_32_bit_tag_matches_the_previous_implementation():
    tx = SRTPContext(KEY_B64, "AES_CM_128_HMAC_SHA1_32")
    assert tx.protect(_rtp(7)).hex() == "800000070001518fdeadbeef506aefdfd94903a15ef1c50ae19c72fd6643f7e5"


def test_srtcp_protect_matches_the_previous_implementation():
    ctx = SRTCPContext(KEY_B64)
    rtcp = struct.pack("!BBHI", 0x80, 200, 6, 0xDEADBEEF) + bytes(range(24))
    assert ctx.protect(rtcp).hex() == (
        "80c80006deadbeef1ffb1796830a8b8bf9f8831da0dde87de23ee32581d0a6158"
        "0000001fea979a1b6177499a3f5")
    assert ctx.protect(rtcp).hex() == (
        "80c80006deadbeefaa0b97f3ce3d60953cbb4ec12ac22d34c3faea3b34a057c18"
        "00000029c6810bdb20289d3f069")


def test_the_integer_iv_is_the_byte_by_byte_one():
    """RFC 3711 §4.1.1: IV = (salt << 16) XOR (SSRC << 64) XOR (index << 16)."""
    ctx = SRTPContext(KEY_B64)
    salt_padded = ctx.salt + b"\x00\x00"
    for ssrc, index in ((0xDEADBEEF, 0), (0, 0xFFFFFFFFFFFF), (0xFFFFFFFF, 0x1_0000), (1, 65535)):
        ssrc_index = (b"\x00\x00\x00\x00" + ssrc.to_bytes(4, "big")
                      + index.to_bytes(6, "big") + b"\x00\x00")
        reference = bytes(a ^ b for a, b in zip(salt_padded, ssrc_index))
        assert _packet_iv(_iv_base(ctx.salt), ssrc, index) == reference
        assert ctx._compute_iv(ssrc, index) == reference
