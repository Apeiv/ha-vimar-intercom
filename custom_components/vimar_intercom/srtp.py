"""SRTP — AES_CM_128_HMAC_SHA1_80 and _32 encrypt/decrypt (RFC 3711).

AES-CTR nativo via pycryptodome (già fra i requirements del manifest): nessun
loop Python e nessuna dipendenza non dichiarata. Il contatore è il blocco da
128 bit completo, come richiede AES-CM di RFC 3711.
"""

import base64
import hashlib
import hmac
import struct

from Crypto.Cipher import AES


def _aes_ctr(key: bytes, iv: bytes):
    """Cifrario AES-CTR con contatore a 128 bit inizializzato a `iv`.

    With an empty nonce the whole 128-bit block is the counter, as
    ``Counter.new(128, initial_value=...)`` built it; this form skips the
    Counter object: 19 µs against 25 µs per 1200-byte packet on a Pi 5.
    """
    return AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv)


def _aes_cm_keystream(key: bytes, iv: bytes, length: int) -> bytes:
    """Keystream AES-CM (Counter Mode), RFC 3711 §4.1.1."""
    return _aes_ctr(key, iv).encrypt(bytes(length))


def _aes_cm_xor(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Cifra/decifra AES-CM (XOR con il keystream)."""
    return _aes_ctr(key, iv).encrypt(data)


def _kdf(master_key: bytes, master_salt: bytes, label: int, length: int) -> bytes:
    """SRTP Key Derivation Function (RFC 3711 §4.3.1)."""
    key_id = (label << 48).to_bytes(7, "big")
    salt = master_salt[:14]
    key_id_padded = b"\x00" * 7 + key_id
    x = bytes(a ^ b for a, b in zip(key_id_padded, salt, strict=False))
    iv = x + b"\x00\x00"
    return _aes_cm_keystream(master_key, iv, length)


def _iv_base(salt: bytes) -> int:
    """The session salt as the 128-bit integer the packet IV is XORed into."""
    return int.from_bytes(salt + b"\x00\x00", "big")


def _packet_iv(salt_int: int, ssrc: int, index: int) -> bytes:
    """IV for AES-CM (RFC 3711 §4.1.1): salt XOR (SSRC << 64) XOR (index << 16).

    One integer XOR instead of a byte-by-byte comprehension: 0.3 µs against
    2.0 µs per packet.
    """
    return (salt_int ^ (ssrc << 64) ^ (index << 16)).to_bytes(16, "big")


def _truncated_hmac(base: hmac.HMAC, data: bytes, length: int) -> bytes:
    """HMAC-SHA1 of ``data`` from a keyed HMAC object, cut to ``length`` bytes.

    ``copy()`` of an object keyed once is cheaper than ``hmac.new`` per packet,
    which pads the key and hashes two blocks again each time.
    """
    h = base.copy()
    h.update(data)
    return h.digest()[:length]


# Supported SDES crypto suites and their SRTP authentication tag length (bytes).
SUITE_TAG_LEN = {
    "AES_CM_128_HMAC_SHA1_80": 10,
    "AES_CM_128_HMAC_SHA1_32": 4,
}


class SRTPContext:
    """SRTP encryption/decryption context for one direction."""

    AUTH_TAG_LEN = 10  # 80-bit HMAC-SHA1

    def __init__(self, master_key_b64: str, suite: str = "AES_CM_128_HMAC_SHA1_80"):
        """Initialize from base64-encoded inline key (30 bytes = 16 key + 14 salt).

        suite: the SDES crypto suite (RFC 4568); _32 only shortens the SRTP
        authentication tag to 32 bits.
        """
        if suite not in SUITE_TAG_LEN:
            raise ValueError(f"unsupported SRTP suite: {suite}")
        self.AUTH_TAG_LEN = SUITE_TAG_LEN[suite]
        raw = base64.b64decode(master_key_b64)
        if len(raw) < 30:
            raise ValueError(f"SRTP key too short: {len(raw)} bytes (need 30)")
        self.master_key = raw[:16]
        self.master_salt = raw[16:30]

        # Derive session keys
        self.cipher_key = _kdf(self.master_key, self.master_salt, 0x00, 16)
        self.auth_key = _kdf(self.master_key, self.master_salt, 0x01, 20)
        self.salt = _kdf(self.master_key, self.master_salt, 0x02, 14)
        self._salt_int = _iv_base(self.salt)
        self._hmac = hmac.new(self.auth_key, digestmod=hashlib.sha1)

        # ROC (Rollover Counter) per SSRC, come libsrtp: un flusso che riparte
        # con un SSRC nuovo (encoder riavviato, relay che cambia sorgente) ha il
        # suo ROC e la sua sequenza. Con uno stato unico il primo pacchetto del
        # nuovo flusso poteva ricevere il ROC sbagliato, e da lì in poi ogni
        # pacchetto falliva l'autenticazione (lo stato si aggiorna solo sui buoni).
        self._streams: dict[int, tuple[int, int]] = {}  # ssrc -> (roc, last_seq)

    def _estimate_index(self, ssrc: int, seq: int) -> tuple[int, int]:
        """(ROC, indice) più vicino all'ultimo indice buono del flusso (RFC 3711 §3.3.1)."""
        state = self._streams.get(ssrc)
        if state is None:
            return 0, seq
        roc, last_seq = state
        last_idx = (roc << 16) | last_seq
        _, r = min((abs(((r << 16) | seq) - last_idx), r)
                   for r in (roc, roc + 1, roc - 1) if r >= 0)
        return r, (r << 16) | seq

    def _update_roc(self, ssrc: int, seq: int, roc: int):
        """Dopo un pacchetto buono: avanza lo stato del flusso se l'indice è nuovo."""
        state = self._streams.get(ssrc)
        if state is None or (roc << 16) | seq > (state[0] << 16) | state[1]:
            self._streams[ssrc] = (roc, seq)

    def _compute_iv(self, ssrc: int, packet_index: int) -> bytes:
        """Compute IV for AES-CM encryption (RFC 3711 §4.1)."""
        return _packet_iv(self._salt_int, ssrc, packet_index)

    def _compute_auth_tag(self, rtp_packet: bytes, roc: int) -> bytes:
        """HMAC-SHA1 over (packet || ROC), truncated to 80 bits."""
        return _truncated_hmac(self._hmac, rtp_packet + struct.pack("!I", roc), self.AUTH_TAG_LEN)

    def unprotect(self, srtp_packet: bytes) -> bytes | None:
        """Decrypt SRTP packet → plain RTP packet. Returns None on auth failure."""
        if len(srtp_packet) < 12 + self.AUTH_TAG_LEN:
            return None

        auth_tag = srtp_packet[-self.AUTH_TAG_LEN:]
        authenticated_portion = srtp_packet[:-self.AUTH_TAG_LEN]

        # Parse RTP header
        cc = authenticated_portion[0] & 0x0F
        hdr_len = 12 + cc * 4

        if authenticated_portion[0] & 0x10:  # X bit
            if len(authenticated_portion) > hdr_len + 4:
                ext_len = struct.unpack_from("!HH", authenticated_portion, hdr_len)
                hdr_len += 4 + ext_len[1] * 4

        if len(authenticated_portion) <= hdr_len:
            return None

        seq = struct.unpack_from("!H", authenticated_portion, 2)[0]
        ssrc = struct.unpack_from("!I", authenticated_portion, 8)[0]

        est_roc, idx = self._estimate_index(ssrc, seq)

        # Verify auth tag with estimated ROC
        expected_tag = self._compute_auth_tag(authenticated_portion, est_roc)
        if not hmac.compare_digest(auth_tag, expected_tag):
            return None

        # Auth passed — update ROC state
        self._update_roc(ssrc, seq, est_roc)

        # Decrypt payload — single native AES-CTR call
        header = authenticated_portion[:hdr_len]
        encrypted_payload = authenticated_portion[hdr_len:]
        iv = self._compute_iv(ssrc, idx)
        decrypted = _aes_cm_xor(self.cipher_key, iv, encrypted_payload)

        return header + decrypted

    def protect(self, rtp_packet: bytes) -> bytes:
        """Encrypt plain RTP packet → SRTP packet."""
        cc = rtp_packet[0] & 0x0F
        hdr_len = 12 + cc * 4

        if rtp_packet[0] & 0x10:  # X bit
            if len(rtp_packet) > hdr_len + 4:
                ext_len = struct.unpack_from("!HH", rtp_packet, hdr_len)
                hdr_len += 4 + ext_len[1] * 4

        seq = struct.unpack_from("!H", rtp_packet, 2)[0]
        ssrc = struct.unpack_from("!I", rtp_packet, 8)[0]

        est_roc, idx = self._estimate_index(ssrc, seq)
        self._update_roc(ssrc, seq, est_roc)

        # Encrypt payload — single native AES-CTR call
        header = rtp_packet[:hdr_len]
        payload = rtp_packet[hdr_len:]
        iv = self._compute_iv(ssrc, idx)
        encrypted = _aes_cm_xor(self.cipher_key, iv, payload)

        srtp_no_tag = header + encrypted
        auth_tag = self._compute_auth_tag(srtp_no_tag, est_roc)

        return srtp_no_tag + auth_tag


class SRTCPContext:
    """SRTCP for one direction (RFC 3711 §3.4).

    Not SRTP with another key: the format and what is authenticated differ.

    * the session keys come from other labels (3 encryption, 4 authentication,
      5 salt) of the same master key as the ``a=crypto`` line;
    * the first record's header stays in the clear, SSRC included: encryption
      starts at byte 8;
    * a 32-bit word goes at the end, with the E bit (encrypted) and a 31-bit
      index that grows by one per packet sent and does not restart on rekey;
    * the authentication tag covers that word too, and the rollover counter is
      NOT appended as in SRTP;
    * with ``AES_CM_128_HMAC_SHA1_80`` the RTCP tag stays 80 bits even when the
      RTP one is 32 (§5.2).

    Used by the HomeKit doorbell for the RTCP it exchanges with the phone.
    """

    AUTH_TAG_LEN = 10
    _E_BIT = 0x80000000
    #: Received indices remembered for replay protection (libsrtp keeps 128).
    REPLAY_WINDOW = 128

    def __init__(self, master_key_b64: str):
        raw = base64.b64decode(master_key_b64)
        if len(raw) < 30:
            raise ValueError(f"SRTCP key too short: {len(raw)} bytes (need 30)")
        self.master_key = raw[:16]
        self.master_salt = raw[16:30]
        self.cipher_key = _kdf(self.master_key, self.master_salt, 0x03, 16)
        self.auth_key = _kdf(self.master_key, self.master_salt, 0x04, 20)
        self.salt = _kdf(self.master_key, self.master_salt, 0x05, 14)
        self._salt_int = _iv_base(self.salt)
        self._hmac = hmac.new(self.auth_key, digestmod=hashlib.sha1)
        self.index = 0
        # Replay protection for what we receive (RFC 3711 §3.3.2): the highest
        # index seen, and a bitmap of the REPLAY_WINDOW indices below it
        # (bit n set = index max - n already received).
        self._rx_max: int | None = None
        self._rx_seen = 0

    def _replay_delta(self, index: int) -> int:
        """How far behind the highest index this one is (negative: newer).
        The 31-bit index wraps, so the difference is taken modulo 2^31."""
        d = (self._rx_max - index) & 0x7FFFFFFF
        return d - (1 << 31) if d & 0x40000000 else d

    def _is_replay(self, index: int) -> bool:
        if self._rx_max is None:
            return False
        d = self._replay_delta(index)
        if d < 0:
            return False
        return d >= self.REPLAY_WINDOW or bool(self._rx_seen >> d & 1)

    def _mark_received(self, index: int) -> None:
        if self._rx_max is None:
            self._rx_max, self._rx_seen = index, 1
            return
        d = self._replay_delta(index)
        mask = (1 << self.REPLAY_WINDOW) - 1
        if d < 0:
            # A jump past the whole window forgets it. Shifted by the jump
            # itself, a jump of 2^30 built a 128 MB integer first.
            if -d >= self.REPLAY_WINDOW:
                self._rx_seen = 1
            else:
                self._rx_seen = ((self._rx_seen << -d) | 1) & mask
            self._rx_max = index
        else:
            self._rx_seen |= 1 << d

    def _compute_iv(self, ssrc: int, index: int) -> bytes:
        return _packet_iv(self._salt_int, ssrc, index)

    def unprotect(self, packet: bytes) -> bytes | None:
        """SRTCP to plain RTCP, or ``None`` if it does not authenticate or is
        a replay (an index already received, or older than the window).

        Without the replay check, anyone on the path could send one captured
        NACK again and again, and every copy made us resend its packets."""
        if len(packet) < 8 + 4 + self.AUTH_TAG_LEN:
            return None
        tag = packet[-self.AUTH_TAG_LEN:]
        signed = packet[:-self.AUTH_TAG_LEN]
        e_index = struct.unpack("!I", signed[-4:])[0]
        if self._is_replay(e_index & ~self._E_BIT):
            return None
        expected = _truncated_hmac(self._hmac, signed, self.AUTH_TAG_LEN)
        if not hmac.compare_digest(tag, expected):
            return None
        self._mark_received(e_index & ~self._E_BIT)
        header, payload = signed[:8], signed[8:-4]
        if not e_index & self._E_BIT:
            return header + payload
        ssrc = struct.unpack("!I", packet[4:8])[0]
        iv = self._compute_iv(ssrc, e_index & ~self._E_BIT)
        return header + _aes_cm_xor(self.cipher_key, iv, payload)

    def protect(self, rtcp_packet: bytes) -> bytes:
        """Plain RTCP to SRTCP, encrypted and authenticated."""
        self.index = (self.index + 1) & 0x7FFFFFFF
        ssrc = struct.unpack_from("!I", rtcp_packet, 4)[0]
        header, payload = rtcp_packet[:8], rtcp_packet[8:]
        iv = self._compute_iv(ssrc, self.index)
        encrypted = _aes_cm_xor(self.cipher_key, iv, payload)
        body = header + encrypted + struct.pack("!I", self._E_BIT | self.index)
        return body + _truncated_hmac(self._hmac, body, self.AUTH_TAG_LEN)
