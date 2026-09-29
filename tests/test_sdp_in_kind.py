"""An SDP answer mirrors the offer's media profile.

A 2F panel offers plain RTP and ignores SRTP; a 2FV2 relay offers RTP/SAVP
with a=crypto. Answering with the other profile claims to accept something we
will not use, so the plant setting only decides when we make the offer.
"""
import pytest

sip = pytest.importorskip("custom_components.vimar_intercom.sip_client")

SRTP_OFFER = (
    "v=0\r\nc=IN IP4 192.168.1.20\r\n"
    "m=audio 7078 RTP/SAVP 0\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz\r\n"
    "m=video 9078 RTP/SAVP 96\r\na=rtpmap:96 H264/90000\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz\r\n"
)
PLAIN_OFFER = (
    "v=0\r\nc=IN IP4 192.168.1.20\r\n"
    "m=audio 7078 RTP/AVP 0\r\n"
    "m=video 9078 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
)


@pytest.mark.parametrize("plant_setting", [False, True])
def test_an_srtp_offer_gets_an_srtp_answer(monkeypatch, plant_setting):
    monkeypatch.setattr(sip.R, "MEDIA_ENC", plant_setting)
    answer = sip.build_sdp(sip.parse_sdp(SRTP_OFFER))
    assert "m=audio" in answer and "RTP/SAVP" in answer
    assert answer.count("a=crypto:") == 2


@pytest.mark.parametrize("plant_setting", [False, True])
def test_a_plain_offer_gets_a_plain_answer(monkeypatch, plant_setting):
    monkeypatch.setattr(sip.R, "MEDIA_ENC", plant_setting)
    answer = sip.build_sdp(sip.parse_sdp(PLAIN_OFFER))
    assert "RTP/SAVP" not in answer and "a=crypto:" not in answer


@pytest.mark.parametrize("plant_setting", [False, True])
def test_our_own_offer_follows_the_plant_setting(monkeypatch, plant_setting):
    monkeypatch.setattr(sip.R, "MEDIA_ENC", plant_setting)
    answer = sip.build_sdp()
    assert ("RTP/SAVP" in answer) is plant_setting


KEY_A = "WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz"
KEY_B = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIz"
MIXED_OFFER = (
    "v=0\r\nc=IN IP4 192.168.1.20\r\n"
    "m=audio 7078 RTP/SAVP 0\r\n"
    f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{KEY_A}\r\n"
    "m=video 9078 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
)


def _mline(answer, kind):
    """The m= line and its attributes, up to the next m= line."""
    block = answer[answer.index(f"m={kind} "):]
    nxt = block.find("\r\nm=", 1)
    return block if nxt < 0 else block[:nxt + 2]


@pytest.mark.parametrize("plant_setting", [False, True])
def test_each_line_mirrors_its_own_profile(monkeypatch, plant_setting):
    monkeypatch.setattr(sip.R, "MEDIA_ENC", plant_setting)
    answer = sip.build_sdp(sip.parse_sdp(MIXED_OFFER))
    audio, video = _mline(answer, "audio"), _mline(answer, "video")
    assert "RTP/SAVP" in audio and "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:" in audio
    assert "RTP/AVP" in video and "a=crypto" not in video
    assert sip._local_crypto_key is not None
    assert sip._local_video_crypto_key is None


def test_the_answer_echoes_the_chosen_tag_and_suite():
    offer = (
        "v=0\r\nc=IN IP4 192.168.1.20\r\n"
        "m=audio 7078 RTP/SAVP 0\r\n"
        f"a=crypto:3 F8_128_HMAC_SHA1_80 inline:{KEY_B}\r\n"
        f"a=crypto:5 AES_CM_128_HMAC_SHA1_32 inline:{KEY_A}\r\n"
        f"a=crypto:6 AES_CM_128_HMAC_SHA1_80 inline:{KEY_B}\r\n"
        "m=video 9078 RTP/SAVP 96\r\na=rtpmap:96 H264/90000\r\n"
        f"a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:{KEY_B}|2^20|1:4\r\n"
    )
    parsed = sip.parse_sdp(offer)
    # The first supported suite wins, and its own key is the one used for RX.
    assert parsed["audio"]["crypto_suite"] == "AES_CM_128_HMAC_SHA1_32"
    assert parsed["audio"]["crypto_tag"] == "5"
    assert parsed["audio"]["crypto_key"] == KEY_A
    assert parsed["video"]["crypto_key"] == KEY_B
    assert len(parsed["audio"]["crypto"]) == 3

    answer = sip.build_sdp(parsed)
    audio, video = _mline(answer, "audio"), _mline(answer, "video")
    assert "a=crypto:5 AES_CM_128_HMAC_SHA1_32 inline:" in audio
    assert audio.count("a=crypto:") == 1
    assert "a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:" in video
    # Our own key, never the offer's.
    assert KEY_A not in answer and KEY_B not in answer


def test_a_crypto_line_on_a_plain_rtp_line_is_ignored():
    offer = ("v=0\r\nc=IN IP4 192.168.1.20\r\nm=audio 7078 RTP/AVP 0\r\n"
             f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{KEY_A}\r\n")
    parsed = sip.parse_sdp(offer)
    assert "crypto_key" not in parsed["audio"]
    assert "a=crypto" not in _mline(sip.build_sdp(parsed), "audio")


def test_an_srtp_line_without_a_supported_suite_is_refused():
    offer = ("v=0\r\nc=IN IP4 192.168.1.20\r\n"
             "m=audio 7078 RTP/SAVP 0\r\n"
             f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{KEY_A}\r\n"
             "m=video 9078 RTP/SAVP 96\r\na=rtpmap:96 H264/90000\r\n"
             f"a=crypto:1 AEAD_AES_256_GCM inline:{KEY_B}\r\n")
    answer = sip.build_sdp(sip.parse_sdp(offer))
    assert _mline(answer, "video").startswith("m=video 0 ")
    assert "a=crypto" not in _mline(answer, "video")
    assert "a=crypto:1 AES_CM_128_HMAC_SHA1_80" in _mline(answer, "audio")


def test_the_32_bit_suite_protects_and_unprotects():
    import base64
    import os
    import struct

    from custom_components.vimar_intercom.srtp import SRTPContext

    key = base64.b64encode(os.urandom(30)).decode()
    tx = SRTPContext(key, "AES_CM_128_HMAC_SHA1_32")
    rx = SRTPContext(key, "AES_CM_128_HMAC_SHA1_32")
    rtp = struct.pack("!BBHII", 0x80, 0, 1, 160, 0x1234) + b"\x55" * 160
    srtp = tx.protect(rtp)
    assert len(srtp) == len(rtp) + 4
    assert rx.unprotect(srtp) == rtp
    assert SRTPContext(key).unprotect(srtp) is None
