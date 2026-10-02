"""SRTP — vettori di test ufficiali RFC 3711 e round-trip protect/unprotect.

I vettori di §B.3 verificano la KDF (e quindi AES-CM) in modo indipendente dalla
libreria crypto usata: servono a garantire che la migrazione da `cryptography` a
`pycryptodome` non abbia cambiato un solo byte del keystream.
"""
from __future__ import annotations

import base64

from custom_components.vimar_intercom.srtp import SRTPContext, _kdf

# RFC 3711 §B.3 — Key Derivation Test Vectors (index 0, key_derivation_rate 0)
MASTER_KEY = bytes.fromhex("E1F97A0D3E018BE0D64FA32C06DE4139")
MASTER_SALT = bytes.fromhex("0EC675AD498AFEEBB6960B3AABE6")

EXPECTED_CIPHER_KEY = bytes.fromhex("C61E7A93744F39EE10734AFE3FF7A087")
EXPECTED_SALT = bytes.fromhex("30CBBC08863D8C85D49DB34A9AE1")
EXPECTED_AUTH_KEY = bytes.fromhex("CEBE321F6FF7716B6FD4AB49AF256A156D38BAA4")


def test_kdf_cipher_key_matches_rfc3711():
    assert _kdf(MASTER_KEY, MASTER_SALT, 0x00, 16) == EXPECTED_CIPHER_KEY


def test_kdf_salt_matches_rfc3711():
    assert _kdf(MASTER_KEY, MASTER_SALT, 0x02, 14) == EXPECTED_SALT


def test_kdf_auth_key_matches_rfc3711():
    assert _kdf(MASTER_KEY, MASTER_SALT, 0x01, 20) == EXPECTED_AUTH_KEY


def _ctx() -> SRTPContext:
    return SRTPContext(base64.b64encode(MASTER_KEY + MASTER_SALT).decode())


def test_context_derives_the_three_session_keys():
    ctx = _ctx()
    assert ctx.cipher_key == EXPECTED_CIPHER_KEY
    assert ctx.salt == EXPECTED_SALT
    assert ctx.auth_key == EXPECTED_AUTH_KEY


def _rtp(seq: int, payload: bytes = b"payload-di-prova") -> bytes:
    """Pacchetto RTP minimo: V=2, PT=0 (PCMU), SSRC fisso, nessun CSRC."""
    return (
        bytes([0x80, 0x00])
        + seq.to_bytes(2, "big")
        + (12345 * seq).to_bytes(4, "big")   # timestamp
        + bytes.fromhex("DEADBEEF")          # SSRC
        + payload
    )


def test_protect_then_unprotect_restituisce_il_pacchetto_originale():
    tx, rx = _ctx(), _ctx()
    pkt = _rtp(1000)
    assert rx.unprotect(tx.protect(pkt)) == pkt


def test_protect_cifra_il_payload_ma_non_l_header():
    tx = _ctx()
    pkt = _rtp(1001)
    protected = tx.protect(pkt)
    assert protected[:12] == pkt[:12]            # header in chiaro
    assert protected[12:-10] != pkt[12:]         # payload cifrato
    assert len(protected) == len(pkt) + 10       # + auth tag da 80 bit


def test_unprotect_scarta_un_pacchetto_manomesso():
    tx, rx = _ctx(), _ctx()
    protected = bytearray(tx.protect(_rtp(1002)))
    protected[15] ^= 0x01                        # flip di un bit nel payload
    assert rx.unprotect(bytes(protected)) is None


def test_sequenza_di_pacchetti_in_ordine():
    tx, rx = _ctx(), _ctx()
    for seq in range(2000, 2010):
        pkt = _rtp(seq)
        assert rx.unprotect(tx.protect(pkt)) == pkt


def test_pacchetto_troppo_corto_non_solleva_eccezioni():
    assert _ctx().unprotect(b"\x80\x00\x00\x01") is None


def _rtp_ssrc(seq: int, ssrc: int) -> bytes:
    return bytes([0x80, 0x00]) + seq.to_bytes(2, "big") + bytes(4) + ssrc.to_bytes(4, "big") + b"x" * 20


def test_rollover_della_sequenza():
    tx, rx = _ctx(), _ctx()
    for seq in list(range(65530, 65536)) + list(range(0, 6)):
        pkt = _rtp(seq)
        assert rx.unprotect(tx.protect(pkt)) == pkt


def test_nuovo_ssrc_con_sequenza_lontana_si_autentica():
    """Un flusso che riparte (nuovo SSRC, sequenza altrove) ha ROC 0 dal mittente:
    con lo stato unico il ricevitore stimava ROC 1 e scartava tutto il flusso."""
    tx_a, tx_b, rx = _ctx(), _ctx(), _ctx()
    for seq in range(40000, 40010):
        assert rx.unprotect(tx_a.protect(_rtp_ssrc(seq, 1))) is not None
    for seq in range(5, 15):
        pkt = _rtp_ssrc(seq, 2)
        assert rx.unprotect(tx_b.protect(pkt)) == pkt


def test_a_captured_packet_sent_again_is_dropped():
    """RFC 3711 §3.3.2: the same index twice inside the window is refused, so a
    captured voice or video packet cannot be replayed."""
    tx, rx = _ctx(), _ctx()
    first = tx.protect(_rtp(1))
    assert rx.unprotect(first) is not None
    assert rx.unprotect(first) is None
    for seq in range(2, 200):
        assert rx.unprotect(tx.protect(_rtp(seq))) is not None
    again = tx.protect(_rtp(150))
    assert rx.unprotect(again) is None


def test_the_stream_goes_on_after_a_packet_far_ahead_or_a_jump_back():
    """Cloud ring on a 40515 (2 Oct): one authentic packet numbered far ahead on
    the same SSRC moved the window there, and the live stream after it was
    refused as older than the window: no preview, no photo, no voice. The same
    for a panel or relay that restarts its numbers lower on the same SSRC. An
    index older than the window restarts the window instead."""
    tx, rx = _ctx(), _ctx()
    for seq in range(1000, 1010):
        assert rx.unprotect(tx.protect(_rtp(seq))) is not None
    assert rx.unprotect(tx.protect(_rtp(21000))) is not None
    for seq in range(1010, 1300):
        assert rx.unprotect(tx.protect(_rtp(seq))) is not None, seq
    for seq in range(5, 300):  # numbers restarted lower, same SSRC
        assert rx.unprotect(tx.protect(_rtp(seq))) is not None, seq
    dup = tx.protect(_rtp(300))
    assert rx.unprotect(dup) is not None and rx.unprotect(dup) is None, "still refuses a replay"
    assert rx.resyncs == 2


def test_late_packets_inside_the_window_are_still_accepted():
    tx, rx = _ctx(), _ctx()
    early, late = tx.protect(_rtp(10)), tx.protect(_rtp(11))
    assert rx.unprotect(late) is not None
    assert rx.unprotect(early) is not None
    # The reorder buffer holds up to 64 packets: one that late must still pass.
    late = tx.protect(_rtp(200))
    for seq in range(201, 301):
        assert rx.unprotect(tx.protect(_rtp(seq))) is not None
    assert rx.unprotect(late) is not None, "100 places late, inside the window"


def test_the_replay_window_follows_the_sequence_wrap_and_each_ssrc():
    tx, rx = _ctx(), _ctx()
    sent = [tx.protect(_rtp(s)) for s in (65534, 65535, 0, 1)]
    assert all(rx.unprotect(p) is not None for p in sent)
    assert all(rx.unprotect(p) is None for p in sent) and rx.replayed == 4
    tx2, rx2 = _ctx(), _ctx()
    pkts = {s: tx2.protect(_rtp(s)) for s in (65533, 65534, 65535, 0, 1)}
    for s in (65533, 65535, 0, 1, 65534):  # 65534 three places late, ROC 0 vs 1
        assert rx2.unprotect(pkts[s]) is not None, s
    assert rx2.unprotect(pkts[65534]) is None
    other = _ctx()
    assert rx.unprotect(other.protect(_rtp_ssrc(1, 2))) is not None
