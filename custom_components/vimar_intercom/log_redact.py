"""Oscuramento delle credenziali nelle righe di log bufferizzate.

Modulo **puro**: nessun import di Home Assistant, così resta testabile senza HA
(vedi `tests/test_smoke.py`).

Serve a `log_buffer.py` (buffer interno e inoltro al log di HA). Il buffer che alimenta
`/api/vimar_intercom/debug` cattura i record a livello DEBUG a prescindere da
come è configurato `logger:`, quindi una riga di diagnostica scritta senza
pensarci diventa una credenziale leggibile via HTTP. Questa è la rete di
sicurezza, non la regola: **le credenziali non vanno loggate**, punto. Se
qualcosa arriva fin qui è comunque un bug da correggere all'origine.

Cosa viene oscurato:

* assegnazioni `chiave=valore` / `chiave: valore` per i nomi che nel protocollo
  Vimar portano segreti (`pwd`, `password`, `ha1`, `token`, `pn-tok`, …) —
  la forma del payload del QR di abbinamento;
* gli header `Authorization` e `Proxy-Authorization`, che contengono il digest
  della password SIP: chi li intercetta può attaccarla offline;
* i campi `response=` e `cnonce=` di una challenge Digest lasciati soli;
* la forma `{"PARAM":"token","VALUE":"..."}` di `GET_INIT_STATUS_REPLY`, in
  entrambi gli ordini (il Tab 40507 manda VALUE prima di PARAM): il token
  della rubrica va trattato come la password SIP;
* le stesse chiavi tra virgolette, cioè JSON (`"token": "…"`, come in
  `action=status`) e repr di un dict (`'token': '…'`);
* un header Authorization finito dentro una riga sola (messaggio loggato con %r).
* SRTP keys: `inline:<key>` in an SDP line, any dict/JSON value under a key
  named `key` or `*_key`, and a 40-character base64 blob after `key:`/`key=`.

Il nome della chiave resta visibile — serve a capire cosa stava succedendo —
mentre il valore diventa `***`.
"""

from __future__ import annotations

import re

MASK = "***"

# Longest text redact() returns. A SIP message with its SDP is a few KB; a longer
# log line is cut, after the patterns have run (cutting first could leave the start
# of a quoted secret without the closing quote its pattern needs).
MAX_LEN = 16384

# Nomi di campo che nel protocollo Vimar portano un segreto.
_SECRET_KEYS = ("pwd", "passwd", "password", "secret", "ha1", "token", "auth", "pn-tok", "apikey", "api_key",
                "crypto_key", "srtp_key", "a_srtp_key", "v_srtp_key", "key_b64")

# La chiave può essere tra virgolette (JSON `"token": "…"`, repr di un dict
# `'token': '…'`): la virgoletta di chiusura della chiave fa parte del gruppo 1.
# Prima della chiave non si usa `\b`: `_` è un carattere di parola, e con `\b`
# `sip_password=…` passava in chiaro. Basta che non la preceda una lettera o una
# cifra: `sip_password`, `pn_token`, `"password"` sono coperti, `mypassword` no.
# Il valore tra virgolette arriva fino alla virgoletta di chiusura, spazi
# compresi (`password="my secret"`); senza virgolette, fino al primo separatore.
_ASSIGN = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:" + "|".join(re.escape(k) for k in _SECRET_KEYS) + r")[\"']?)(\s*[:=]\s*)"
    r"(?:\"([^\"]*)\"|'([^']*)'|([^\"'\s,;&}]+))"
)


def _mask_assign(m: re.Match) -> str:
    if m.group(3) is not None:
        val = f'"{MASK}"'
    elif m.group(4) is not None:
        val = f"'{MASK}'"
    else:
        val = MASK
    return f"{m.group(1)}{m.group(2)}{val}"


# Authorization / Proxy-Authorization: tutto il valore, fino a fine riga.
# Only horizontal whitespace around the name: with `\s*` the prefix crossed newlines,
# so a text of n bare line feeds cost O(n^2) (seconds for one crafted SIP datagram,
# on the event loop, #46).
_AUTH_HEADER = re.compile(r"(?im)^([ \t]*(?:proxy-)?authorization[ \t]*:[ \t]*).*$")

# Lo stesso header dentro una riga sola, tipicamente un messaggio SIP loggato
# con %r (i CRLF diventano `\\r\\n` letterali): fino al primo CRLF, reale o
# scritto, o alla virgoletta che chiude il repr.
_AUTH_INLINE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*:\s*)(?:digest|basic|bearer)\b.*?(?=\\r|\\n|[\r\n']|$)"
)

# Digest sparso in una riga che non è un header completo.
_DIGEST_FIELD = re.compile(r"(?i)\b(response|cnonce)(\s*=\s*)(\"?)([0-9a-fA-F]{8,})\3")

# SRTP master key in an SDP line: `a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:<base64>`.
# Whoever reads a log with that key can decrypt the call's audio and video.
_SRTP_INLINE = re.compile(r"(?i)(inline:)([A-Za-z0-9+/=]+)")

# A dict or JSON value under a key named `key` or ending in `_key`/`-key`
# (`{'suite': ..., 'key': '...'}`, `"srtp_key": "..."`): whatever the value.
_DICT_KEY = re.compile(r"(?i)(([\"'])(?:[\w-]*[_-])?key\2\s*:\s*)([\"'])(.*?)\3")

# A bare base64 blob of SRTP-key length (30 bytes = 40 characters) after
# `key:` or `key=`, quoted or not.
_KEY_B64 = re.compile(r"(?i)(\bkey[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9+/]{40}[A-Za-z0-9+/=]*)")

# GET_INIT_STATUS_REPLY: {"PARAM":"token","VALUE":"..."}
_PARAM_VALUE = re.compile(
    r"(?i)(\"PARAM\"\s*:\s*\"(?:token|pwd|password)\"\s*,\s*\"VALUE\"\s*:\s*\")([^\"]*)(\")"
)

# Stessa forma con l'ordine invertito, come la manda davvero il Tab 40507:
# {"VALUE": "...", "PARAM": "token"}
_VALUE_PARAM = re.compile(
    r"(?i)(\"VALUE\"\s*:\s*\")([^\"]*)(\"\s*,\s*\"PARAM\"\s*:\s*\"(?:token|pwd|password)\")"
)


def redact(text: str) -> str:
    """Restituisce `text` con i valori sensibili sostituiti da `***`.

    Non solleva mai: è chiamata dentro un handler di logging, dove un'eccezione
    farebbe perdere la riga (o peggio, la lascerebbe passare in chiaro).
    """
    if not text:
        return text
    try:
        out = _ASSIGN.sub(_mask_assign, text)
        out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{MASK}", out)
        out = _AUTH_INLINE.sub(lambda m: f"{m.group(1)}{MASK}", out)
        out = _DIGEST_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}{m.group(3)}", out)
        out = _PARAM_VALUE.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
        out = _VALUE_PARAM.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
        out = _SRTP_INLINE.sub(lambda m: f"{m.group(1)}{MASK}", out)
        out = _DICT_KEY.sub(lambda m: f"{m.group(1)}{m.group(3)}{MASK}{m.group(3)}", out)
        out = _KEY_B64.sub(lambda m: f"{m.group(1)}{MASK}", out)
        if len(out) > MAX_LEN:
            out = f"{out[:MAX_LEN]}... [{len(out) - MAX_LEN} characters cut]"
        return out
    except Exception:  # noqa: BLE001 - mai far fallire il logging
        return MASK
