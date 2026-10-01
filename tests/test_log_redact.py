"""Oscuramento delle credenziali nel buffer di debug.

Il buffer che alimenta `/api/vimar_intercom/debug` cattura i record DEBUG a
prescindere da come è configurato `logger:`. Una riga di diagnostica scritta
senza pensarci diventerebbe una credenziale leggibile via HTTP: questi test
fissano la rete di sicurezza che sta in mezzo.
"""
from __future__ import annotations

import pytest

lr = pytest.importorskip("custom_components.vimar_intercom.log_redact")


# ─── la forma del payload del QR di abbinamento ──────────────────────────

def test_password_del_qr_non_sopravvive():
    out = lr.redact("QR payload: ID=12345\nPWD=segretissima\nDOMAIN=x.y")
    assert "segretissima" not in out
    assert "ID=12345" in out, "gli altri campi restano leggibili"


@pytest.mark.parametrize("chiave", ["PWD", "pwd", "password", "passwd", "ha1", "secret"])
def test_tutti_i_nomi_che_portano_un_segreto(chiave):
    assert "valoresegreto" not in lr.redact(f"{chiave}=valoresegreto")


def test_il_nome_della_chiave_resta_visibile():
    """Serve a capire cosa stava succedendo: si oscura il valore, non il campo."""
    assert "ha1" in lr.redact("ha1=0123456789abcdef0123456789abcdef")


# ─── digest SIP: da lì si attacca la password offline ──────────────────────

@pytest.mark.parametrize("header", ["Authorization", "authorization", "Proxy-Authorization"])
def test_header_di_autorizzazione(header):
    riga = f'{header}: Digest username="101", realm="r", response="a1b2c3d4e5f6a1b2"'
    out = lr.redact(riga)
    assert "a1b2c3d4e5f6a1b2" not in out
    assert header.split(":")[0].lower() in out.lower()


def test_response_digest_isolata():
    assert "deadbeefcafe1234" not in lr.redact('response="deadbeefcafe1234"')


# ─── token della rubrica: va trattato come la password SIP ─────────────────

def test_token_in_get_init_status_reply():
    body = 'GET_INIT_STATUS_REPLY;[{"PARAM":"dnd","VALUE":"1"},{"PARAM":"token","VALUE":"abc123def"}]'
    out = lr.redact(body)
    assert "abc123def" not in out
    assert '"PARAM":"dnd","VALUE":"1"' in out, "i parametri non sensibili restano"


# ─── niente falsi positivi: il log deve restare utile ─────────────────────

def test_una_riga_innocua_non_viene_toccata():
    riga = "Door command: uri=sip:55001@x.ipvdes.vimar.cloud body=OPEN_2F registered=True"
    assert lr.redact(riga) == riga


def test_la_parola_token_in_prosa_non_attiva_l_oscuramento():
    riga = "Registered push token for iPhone di Lorenzo"
    assert lr.redact(riga) == riga


# ─── robustezza: gira dentro un logging handler ─────────────────────────

def test_non_solleva_mai(monkeypatch):
    """Un'eccezione qui farebbe perdere la riga, o peggio la lascerebbe passare."""
    monkeypatch.setattr(lr, "_ASSIGN", None)  # rompe la regex di proposito
    assert lr.redact("PWD=x") == lr.MASK


@pytest.mark.parametrize("vuoto", ["", None])
def test_input_vuoto(vuoto):
    assert lr.redact(vuoto) == vuoto


# ─── i quattro casi che passavano in chiaro (review del 27/09) ────────────

@pytest.mark.parametrize("riga, segreto", [
    ("sip_password=abc123", "abc123"),
    ('password="my secret"', "my secret"),
    ("token: 'x y z'", "x y z"),
    ("{'sip_password': 'secret1'}", "secret1"),
    ('{"sip_password": "secret 2", "sip_user": "12345"}', "secret 2"),
    ("pn_token=abcd", "abcd"),
])
def test_casi_che_passavano_in_chiaro(riga, segreto):
    out = lr.redact(riga)
    assert segreto not in out
    assert "***" in out


def test_le_virgolette_restano_e_gli_altri_campi_pure():
    out = lr.redact('{"sip_password": "a b", "sip_user": "12345"}')
    assert out == '{"sip_password": "***", "sip_user": "12345"}'


@pytest.mark.parametrize("riga", ["mypassword_hint=ok", "tokens=5", "sip_user=12345"])
def test_non_oscura_quello_che_non_e_un_segreto(riga):
    assert lr.redact(riga) == riga

# ─── SRTP keys ────────────────────────────────────────────────────────────────

SRTP_KEY = "WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz"


def test_the_srtp_key_in_an_sdp_line_is_hidden():
    out = lr.redact(f"[SDP <<<]   a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{SRTP_KEY}")
    assert SRTP_KEY not in out
    assert "AES_CM_128_HMAC_SHA1_80 inline:" in out, "the line stays readable"


@pytest.mark.parametrize("name", ["crypto_key", "a_srtp_key", "v_srtp_key"])
def test_the_srtp_key_in_a_parsed_sdp_dict_is_hidden(name):
    out = lr.redact(f"SDP: audio={{'port': 4000, '{name}': '{SRTP_KEY}'}}")
    assert SRTP_KEY not in out and "'port': 4000" in out


@pytest.mark.parametrize("name", ["key", "srtp-key", "master_key"])
def test_any_value_under_a_key_named_key_is_hidden(name):
    out = lr.redact(f"crypto=[{{'tag': '1', '{name}': 'abc123'}}] \"{name}\": \"xyz789\"")
    assert "abc123" not in out and "xyz789" not in out and "'tag': '1'" in out


def test_a_bare_srtp_sized_base64_after_key_is_hidden():
    assert SRTP_KEY not in lr.redact(f"key: {SRTP_KEY}")
    assert SRTP_KEY not in lr.redact(f"key={SRTP_KEY}")


def test_words_that_merely_contain_key_stay_readable():
    assert lr.redact("'keyframe': 12, 'monkey': 'x'") == "'keyframe': 12, 'monkey': 'x'"


# ─── crafted input cannot stall the event loop (#46) ─────────────────────

def _elapsed(text: str) -> float:
    import time
    start = time.perf_counter()
    lr.redact(text)
    return time.perf_counter() - start


def test_bare_line_feeds_take_linear_time():
    """When the Authorization prefix could cross newlines, 8000 bare LF after
    a status line took about 0.7 s, 64 KB of them tens of seconds."""
    assert _elapsed("SIP/2.0 200 OK\r\n" + "\n" * 8000) < 0.1


def test_a_long_text_is_cut_after_the_patterns_ran():
    out = lr.redact("password=hidden " + "x" * 100_000)
    assert "hidden" not in out
    assert len(out) < lr.MAX_LEN + 100 and out.endswith("characters cut]")
    assert _elapsed("\n" * 100_000) < 0.1


@pytest.mark.parametrize("shape", ['password="{}"', "{{'token': '{}'}}", '{{"PARAM":"token","VALUE":"{}"}}',
                                   "{{'srtp_key': '{}'}}"])
@pytest.mark.parametrize("shift", [-20, -8, -1, 0, 1])
def test_a_quoted_secret_across_the_cut_is_still_hidden(shape, shift):
    """The text is cut after the patterns ran: cut first, a quoted value lost its
    closing quote and its first characters came out in clear."""
    line = shape.format("TOPSECRET1234")
    pad = lr.MAX_LEN + shift - line.index("TOPSECRET")
    out = lr.redact(" " * pad + line + " " * 50)
    assert "TOPSECR" not in out


def test_long_hostile_text_stays_fast_without_an_input_cap():
    for text in ("\n" * 65_000, "\r\n " * 20_000, 'password="' * 6_000,
                 "authorization" + " " * 65_000):
        assert _elapsed(text) < 0.2


def test_the_header_is_still_hidden_with_spaces_or_tabs_before_it():
    out = lr.redact("INVITE sip:x SIP/2.0\r\n \tAuthorization: Digest response=\"abcdef0123\"\r\n"
                    "Proxy-Authorization:\tDigest nonce=\"n\"\r\n")
    assert "abcdef0123" not in out and "nonce" not in out
    assert "Authorization: ***" in out and "Proxy-Authorization:\t***" in out
