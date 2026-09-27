"""SRTP — AES_CM_128_HMAC_SHA1_80 encrypt/decrypt (RFC 3711).

AES-CTR nativo via pycryptodome (già fra i requirements del manifest): nessun
loop Python e nessuna dipendenza non dichiarata. Il contatore è il blocco da
128 bit completo, come richiede AES-CM di RFC 3711.
"""

import base64
import hashlib
import hmac
import struct

from Crypto.Cipher import AES
from Crypto.Util import Counter


def _aes_ctr(key: bytes, iv: bytes):
    """Cifrario AES-CTR con contatore a 128 bit inizializzato a `iv`."""
    ctr = Counter.new(128, initial_value=int.from_bytes(iv, "big"))
    return AES.new(key, AES.MODE_CTR, counter=ctr)


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
    x = bytes(a ^ b for a, b in zip(key_id_padded, salt))
    iv = x + b"\x00\x00"
    return _aes_cm_keystream(master_key, iv, length)


class SRTPContext:
    """SRTP encryption/decryption context for one direction."""

    AUTH_TAG_LEN = 10  # 80-bit HMAC-SHA1

    def __init__(self, master_key_b64: str):
        """Initialize from base64-encoded inline key (30 bytes = 16 key + 14 salt)."""
        raw = base64.b64decode(master_key_b64)
        if len(raw) < 30:
            raise ValueError(f"SRTP key too short: {len(raw)} bytes (need 30)")
        self.master_key = raw[:16]
        self.master_salt = raw[16:30]

        # Derive session keys
        self.cipher_key = _kdf(self.master_key, self.master_salt, 0x00, 16)
        self.auth_key = _kdf(self.master_key, self.master_salt, 0x01, 20)
        self.salt = _kdf(self.master_key, self.master_salt, 0x02, 14)

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
        salt_padded = self.salt + b"\x00\x00"
        ssrc_index = (
            b"\x00\x00\x00\x00"
            + ssrc.to_bytes(4, "big")
            + packet_index.to_bytes(6, "big")
            + b"\x00\x00"
        )
        return bytes(a ^ b for a, b in zip(salt_padded, ssrc_index))

    def _compute_auth_tag(self, rtp_packet: bytes, roc: int) -> bytes:
        """HMAC-SHA1 over (packet || ROC), truncated to 80 bits."""
        data = rtp_packet + struct.pack("!I", roc)
        return hmac.new(self.auth_key, data, hashlib.sha1).digest()[:self.AUTH_TAG_LEN]

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
