"""Vimar Intercom — SIP signaling: transport, auth, operations."""

import asyncio
import contextlib
import hashlib
import os
import random
import socket
import ssl
import string
import time
import logging

from . import const as C
from . import runtime as R
from . import log_redact
from . import media_handler as media
from . import model_detect
from . import validate

_LOGGER = logging.getLogger(__name__)

# ─── Broadcast callback (set by hub) ────────────────────────────────
_broadcast = None


def init(broadcast_fn):
    global _broadcast
    _broadcast = broadcast_fn


RING_MAX_S = 90    # squillo senza CANCEL (UDP perso, PBX riavviato): chiuso dopo 90 s

_suppress_broadcast = False


@contextlib.contextmanager
def silenced():
    """Niente broadcast finché dura (cambio targa: il BYE non è una fine chiamata)."""
    global _suppress_broadcast
    _suppress_broadcast = True
    try:
        yield
    finally:  # anche se annullato: altrimenti SIP resterebbe muto
        _suppress_broadcast = False

async def broadcast(msg_type, msg):
    if _suppress_broadcast:
        _LOGGER.debug("Broadcast suppressed: %s %s", msg_type, msg)
        return
    if _broadcast:
        await _broadcast(msg_type, msg)


# ─── State ──────────────────────────────────────────────────────────
reader = None
writer = None
lock = None
_udp_sock = None     # UDP socket (local mode)
_udp_target = None   # (host, port) target for UDP sendto
registered = False
in_call = False
calling = False
cseq_counter = 0
local_tag = None
MY_IP = None

# State change callback — hub sets this to notify entities
_state_change_callback = None

call_state = {
    "call_id": None, "from_tag": None, "to_tag": None,
    "remote_contact": None, "remote_sdp": None, "original_target": None,
    "route_set": None,   # Record-Route del dialogo, già nell'ordine per il nostro Route
    "local_sdp": None,   # il nostro SDP: riusato nel 200 a un re-INVITE
}

pending_responses: dict[str, asyncio.Queue] = {}
_CANCEL = object()   # messo nella coda di do_call da do_hangup: annulla l'INVITE
incoming_requests: asyncio.Queue = None


def set_state_callback(cb):
    """Set callback that fires on registered/in_call changes."""
    global _state_change_callback
    _state_change_callback = cb


def _notify_state_change():
    """Notify hub that SIP state changed."""
    if _state_change_callback:
        try:
            _state_change_callback()
        except Exception:
            _LOGGER.exception("State change callback error")


def _set_registered(val: bool):
    global registered
    if registered != val:
        registered = val
        _notify_state_change()


def _set_in_call(val: bool):
    global in_call
    if in_call != val:
        in_call = val
        _notify_state_change()


def _set_calling(val: bool):
    global calling
    if calling != val:
        calling = val
        _notify_state_change()


def get_local_ip():
    """Detect local IP by routing toward the SIP target."""
    target = (R.LOCAL_PROXY, C.LOCAL_SIP_PORT) if R.USE_LOCAL_UDP else (R.SIP_PROXY, C.SIP_PORT)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(target)
        ip = s.getsockname()[0]
        _LOGGER.info("Detected local IP: %s (via %s)", ip, target[0])
        return ip
    except Exception:
        _LOGGER.warning("IP detection failed, using 0.0.0.0")
        return "0.0.0.0"
    finally:
        s.close()


# ─── Transport helpers ───────────────────────────────────────────────

def _transport():
    return "UDP" if R.USE_LOCAL_UDP else "TLS"


def _my_port():
    """La porta su cui ascoltiamo **davvero**.

    connect() ripiega su una porta effimera quando LOCAL_UDP_PORT e' occupata.
    Fino alla 1.0.5 questa funzione restituiva comunque la porta configurata, e
    quel valore finiva in Via, nel Contact della REGISTER e in _simple_contact():
    la registrazione riusciva lo stesso (le risposte tornano al source port) ma
    l'INVITE in arrivo veniva instradato verso una porta dove non ascolta
    nessuno. Campanello muto, nessun errore, nessun log.
    """
    if not R.USE_LOCAL_UDP:
        return 5070
    if _udp_sock is not None:
        try:
            return int(_udp_sock.getsockname()[1])
        except OSError:
            pass
    return R.LOCAL_UDP_PORT


def _via_line(branch):
    return (f"Via: SIP/2.0/{_transport()} {MY_IP}:{_my_port()};"
            f"branch={branch};rport\r\n")


def _contact_hdr(include_pn=True):
    """Return the full Contact header value (no 'Contact:' prefix)."""
    port = _my_port()
    if R.USE_LOCAL_UDP:
        contact = f"<sip:{R.SIP_USER}@{MY_IP}:{port}>"
        contact += f';+sip.instance="<urn:uuid:{R.DEVICE_UUID}>"'
        contact += ";expires=3600"
        return contact
    # TLS/cloud mode
    contact_uri = f"sip:{R.SIP_USER}@{MY_IP}:{port};transport=tls"
    if include_pn and C.PN_TOKEN:
        contact_uri += (f";app-id={C.PN_APP_ID}"
                        f";pn-type={C.PN_TYPE}"
                        f";pn-tok={C.PN_TOKEN}"
                        f";pn-msg-str=IM_MSG;pn-msg-snd=msg.caf"
                        f";pn-call-str=IC_MSG;pn-call-snd=notes_of_the_optimistic.caf"
                        f";q=0.00;domain-name={R.SIP_DOMAIN}")
    contact = f"<{contact_uri}>"
    contact += f';+sip.instance="<urn:uuid:{R.DEVICE_UUID}>"'
    contact += f";expires={'5184000' if C.PN_TOKEN else '3600'}"
    return contact


def _route_line():
    """Route verso il proxy cloud per le richieste FUORI dialogo (REGISTER, INVITE,
    MESSAGE, OPTIONS). Per quelle nel dialogo vale `_dialog_target()`."""
    if R.USE_LOCAL_UDP:
        return ""
    return f"Route: <sip:{R.SIP_PROXY};transport=tls;lr>\r\n"


def _route_set(hdrs: dict, reverse: bool = False) -> list[str]:
    """I Record-Route di un messaggio, uno per URI. `reverse`: siamo il chiamante
    (UAC) e il route set è quello del 2xx letto al contrario (RFC 3261 §12.1.2)."""
    rs = [p.strip() for v in hdrs.get("_rr_all", []) for p in v.split(",") if p.strip()]
    return rs[::-1] if reverse else rs


def _angle_uri(hdr: str) -> str:
    """L'URI di un header To/From/Contact: quello fra <...>, o tutto fino al ';'."""
    return hdr[hdr.index("<") + 1:hdr.index(">")] if "<" in hdr and ">" in hdr else hdr.split(";")[0]


def _contact_uri(hdrs: dict) -> str:
    return _angle_uri(hdrs.get("contact", ""))


def _dialog_target(contact: str | None, aor: str | None, route_set: list[str] | None) -> tuple[str, str]:
    """(Request-URI, riga Route) di una richiesta nel dialogo: ACK del 2xx, INFO, BYE.

    RFC 3261 §12.2.1.1: Request-URI = Contact del peer (remote target), Route =
    route set, cioè i Record-Route che il proxy ha messo nel 2xx (o nell'INVITE
    ricevuto). Fino alla 1.0.10 il Route era sempre quello fisso del proxy e il
    Request-URI il Contact: sul cloud Vimar il Contact è quello interno del
    proxy (sip:60002@127.0.0.1:6095) e Flexisip, senza il suo Record-Route,
    scartava INFO e BYE senza rispondere: keyframe mai richiesti, «Riaggancia»
    che aspettava il BYE della targa.

    Senza Record-Route: in locale il Contact della targa è raggiungibile e non
    serve Route; sul cloud si instrada per AOR dal proxy fisso, come l'INVITE
    (un 127.0.0.1 nel Request-URI non porta da nessuna parte).
    """
    if route_set:
        return contact or aor, f"Route: {', '.join(route_set)}\r\n"
    if R.USE_LOCAL_UDP:
        return contact or aor, ""
    return aor or contact, _route_line()


_DIALOG_METHODS = ("ACK", "BYE", "CANCEL", "INFO", "UPDATE")


def _dump(msg: str) -> str:
    """Messaggio SIP intero per il log di debug: credenziali oscurate, corpo tagliato."""
    head, _, body = msg.partition("\r\n\r\n")
    text = log_redact.redact(head.replace("\r\n", "\n"))
    if body:
        text += "\n\n" + body[:200] + ("…" if len(body) > 200 else "")
    return text


def _log_full(direction: str, msg: str) -> None:
    """Al livello DEBUG, tutto il messaggio se è una risposta o una richiesta nel
    dialogo (in entrambe le direzioni): sono quelle che sul campo non si riesce
    a diagnosticare dalla prima riga."""
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    kind, hdrs, *_ = _parse(msg)
    if isinstance(kind, int) or kind in _DIALOG_METHODS or ";tag=" in hdrs.get("to", ""):
        _LOGGER.debug("[SIP %s]\n%s", direction, _dump(msg))


def _simple_contact():
    """Contact URI for in-dialog responses (180/200 to incoming INVITE)."""
    port = _my_port()
    if R.USE_LOCAL_UDP:
        return f"<sip:{R.SIP_USER}@{MY_IP}:{port}>"
    return f"<sip:{R.SIP_USER}@{MY_IP}:{port};transport=tls>"


def _gen(prefix="z9hG4bK"):
    return f"{prefix}{random.randint(100000, 9999999):x}"


def _next_cseq():
    global cseq_counter
    cseq_counter += 1
    return cseq_counter


# ─── Digest Auth ────────────────────────────────────────────────────

def _compute_ha1(realm):
    if realm == R.SIP_DOMAIN:
        return R.SIP_HA1
    return hashlib.md5(f"{R.SIP_USER}:{realm}:{R.SIP_PASSWORD}".encode()).hexdigest()


def _digest_resp(method, uri, nonce, realm=None, qop=None, nc=None, cnonce=None):
    ha1 = _compute_ha1(realm or R.SIP_DOMAIN)
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop == "auth":
        return hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()
    return hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()


def _challenge_params(challenge: str) -> dict:
    p = {}
    for item in challenge.replace("Digest ", "").split(","):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            p[k.strip()] = v.strip().strip('"')
    return p


# L'ultima sfida del proxy (Proxy-Authenticate): con quella gli INFO di keyframe
# partono già autenticati, invece di un 407 per ognuno degli 8 del burst iniziale.
_proxy_challenge: str | None = None
AUTH_RETRIES = 2   # tetto ai tentativi autenticati di una richiesta (INVITE, INFO)


def _retry_auth(challenge: str, tries: int, last: str | None) -> bool:
    """Vale la pena rimandare la richiesta con questa sfida? Al primo 407 sì; dopo
    solo se il proxy dice stale=true o cambia nonce, e mai oltre AUTH_RETRIES.
    Dal campo (2F, 28/09): ~40 INVITE in 3,5 s, uno per ogni 407 ritrasmesso."""
    global _proxy_challenge
    if not challenge or tries >= AUTH_RETRIES:
        return False
    if tries and last is not None:
        p, q = _challenge_params(challenge), _challenge_params(last)
        if p.get("stale", "").lower() != "true" and p.get("nonce") == q.get("nonce"):
            return False
    _proxy_challenge = challenge
    return True


def _make_auth(method, uri, challenge):
    p = _challenge_params(challenge)
    nonce = p.get("nonce", "")
    realm = p.get("realm", R.SIP_DOMAIN)
    opaque = p.get("opaque", "")
    qop = p.get("qop", "")
    nc = "00000001"
    cnonce = f"{random.randint(10**7, 10**8-1):08x}"
    if "auth" in qop:
        resp = _digest_resp(method, uri, nonce, realm, "auth", nc, cnonce)
        hdr = (f'Digest username="{R.SIP_USER}", realm="{realm}", '
               f'nonce="{nonce}", uri="{uri}", response="{resp}", '
               f'algorithm=MD5, qop=auth, nc={nc}, cnonce="{cnonce}"')
    else:
        resp = _digest_resp(method, uri, nonce, realm)
        hdr = (f'Digest username="{R.SIP_USER}", realm="{realm}", '
               f'nonce="{nonce}", uri="{uri}", response="{resp}", '
               f'algorithm=MD5')
    if opaque:
        hdr += f', opaque="{opaque}"'
    return hdr


# ─── Transport ──────────────────────────────────────────────────────

def _create_ssl_context(verify: bool = True):
    """Contesto TLS per la modalita' cloud.

    Con il CA di Vimar presente (`vimar_rootca.pem`) si verifica contro quello.
    Senza, e con verify=True, si verifica contro il trust store di sistema.
    verify=False disattiva ogni verifica: fino alla 1.0.5 era il comportamento
    di **tutte** le installazioni, perche' il CA non e' nel repo e il ramo else
    era l'unico attivo — in silenzio. Chi si fosse messo in mezzo avrebbe
    raccolto la REGISTER con il digest della password SIP.

    connect() ora prova prima verificando e ripiega qui solo se l'handshake
    fallisce per il certificato, scrivendolo nel log.
    """
    ctx = ssl.create_default_context()
    if os.path.exists(C.CA_PATH):
        ctx.load_verify_locations(C.CA_PATH)
    elif not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _resolve_sip_targets(proxy: str, default_port: int) -> list[tuple[str, int]]:
    """Restituisce [(host, port)] per il proxy SIP cloud: SRV _sips._tcp.<proxy>,
    poi fallback noti, poi il proxy stesso."""
    targets: list[tuple[str, int]] = []
    try:
        import dns.resolver  # dnspython (presente in HA)
        ans = dns.resolver.resolve(f"_sips._tcp.{proxy}", "SRV")
        recs = sorted(ans, key=lambda r: (r.priority, -r.weight))
        for r in recs:
            targets.append((str(r.target).rstrip("."), int(r.port)))
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("SRV lookup _sips._tcp.%s failed: %s", proxy, e)
    if not targets and proxy.endswith("ipvdes.vimar.cloud"):
        targets = [(f"flexiprod{i}.ipvdes2.vimarsso.cloud", 7042) for i in (1, 2, 3)]
    targets.append((proxy, default_port))
    return targets


async def connect():
    global reader, writer, lock, _udp_sock, _udp_target, MY_IP
    MY_IP = get_local_ip()

    if R.USE_LOCAL_UDP:
        # ── UDP locale: apre un socket UDP verso il citofono ───────
        _udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            _udp_sock.bind(("0.0.0.0", R.LOCAL_UDP_PORT))
        except OSError as e:
            # Porta occupata (restart di HA, un altro servizio SIP, una seconda
            # entry): si ripiega su una effimera. _my_port() la rilegge dalla
            # socket, quindi Via e Contact restano veri — ma va detto, perche'
            # se qualcun altro tiene la porta standard puo' intercettare le
            # chiamate in arrivo al posto nostro.
            _udp_sock.bind(("0.0.0.0", 0))
            _LOGGER.warning(
                "Porta UDP %d occupata (%s): in ascolto sulla %d. Se un altro "
                "servizio SIP tiene la %d, le chiamate in arrivo potrebbero "
                "non raggiungere Home Assistant.",
                R.LOCAL_UDP_PORT, e, _udp_sock.getsockname()[1], R.LOCAL_UDP_PORT)
        _udp_sock.setblocking(False)
        _udp_target = (R.LOCAL_PROXY, C.LOCAL_SIP_PORT)
        lock = asyncio.Lock()
        _LOGGER.info("SIP UDP socket bound, target %s:%d",
                     R.LOCAL_PROXY, C.LOCAL_SIP_PORT)
    else:
        # ── TLS/TCP cloud mode ─────────────────────────────────────
        loop = asyncio.get_running_loop()
        # Il proxy cloud reale è pubblicato via DNS SRV (_sips._tcp.<cproxy>):
        # es. flexiprod1/2/3.ipvdes2.vimarsso.cloud:7042. <cproxy> stesso è un
        # CDN HTTPS e NON parla SIP → senza SRV la connessione resta appesa.
        candidates = await loop.run_in_executor(None, _resolve_sip_targets, R.SIP_PROXY, C.SIP_PORT)
        last_err = None
        # L'SNI è il nome del servizio cloud (<cproxy> del QR), non l'host SRV a
        # cui ci si connette. Va preso dal config entry: su un impianto con un
        # cproxy diverso da quello di default, una costante qui manderebbe in
        # handshake TLS il nome sbagliato.
        # Due giri: prima verificando il certificato, poi — solo se a fermarci
        # e' stato il certificato e non la rete — senza verifica, dicendolo.
        # L'ordine conta: chi ha un impianto che presenta un certificato
        # verificabile ottiene una connessione sicura senza dover configurare
        # niente, e chi non ce l'ha continua a funzionare come prima, ma con una
        # riga nel log invece che in silenzio.
        cert_error = None
        for verify in (True, False):
            ctx = await loop.run_in_executor(None, _create_ssl_context, verify)
            if not verify:
                _LOGGER.warning(
                    "TLS: certificato del proxy cloud non verificabile (%s). "
                    "Riprovo SENZA verifica: la connessione resta cifrata ma non "
                    "autenticata, quindi un intermediario potrebbe leggere la "
                    "REGISTER e ricavare offline la password SIP. Per chiudere "
                    "questo buco serve il CA di Vimar in %s.",
                    cert_error, C.CA_PATH)
            for host, port in candidates:
                _LOGGER.info("Connecting to SIP proxy %s:%d (SNI %s, verify=%s)...",
                             host, port, R.SIP_PROXY, verify)
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port, ssl=ctx, server_hostname=R.SIP_PROXY),
                        timeout=12)
                    break
                except ssl.SSLCertVerificationError as e:
                    cert_error = e
                    last_err = e
                    _LOGGER.warning("SIP TLS %s:%d: certificato rifiutato: %s", host, port, e)
                    reader = writer = None
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    _LOGGER.warning("SIP TLS connect to %s:%d failed: %s", host, port, e)
                    reader = writer = None
            if writer is not None or cert_error is None:
                # Riuscito, oppure fallito per motivi che il secondo giro non
                # risolverebbe (host irraggiungibile, timeout, rete assente).
                break
        if writer is None:
            raise ConnectionError(f"Nessun proxy SIP cloud raggiungibile: {last_err}")
        lock = asyncio.Lock()
        _LOGGER.info("SIP TLS connected")


def _reconnect_started() -> asyncio.Task:
    """La riconnessione in corso, o una nuova. Una sola alla volta: reader,
    keepalive e WS "reconnect" chiamavano connect() in parallelo, e ognuno
    chiudeva la connessione appena aperta dall'altro (5 connessioni e REGISTER
    falliti per un solo calo del cloud; il reader poteva restare su quella orfana)."""
    global _reconnect_task
    if _reconnect_task is None or _reconnect_task.done():
        _reconnect_task = asyncio.create_task(_reconnect())
    return _reconnect_task


async def reconnect():
    return await asyncio.shield(_reconnect_started())


def cancel_reconnect() -> None:
    """Ferma la riconnessione in corso (unload dell'integrazione)."""
    if _reconnect_task:
        _reconnect_task.cancel()


async def _reconnect():
    """Reconnect / re-register with exponential backoff."""
    global reader, writer
    _set_registered(False)
    delays = [2, 4, 8, 16, 32]
    for attempt, delay in enumerate(delays, 1):
        _LOGGER.warning("SIP reconnect attempt %d/%d in %ds...", attempt, len(delays), delay)
        await asyncio.sleep(delay)
        try:
            if R.USE_LOCAL_UDP:
                # UDP: non serve riconnettersi, solo re-registrarsi
                ok = await do_register()
            else:
                if writer:
                    try:
                        writer.close()
                    except Exception:
                        pass
                await connect()
                _LOGGER.info("SIP reconnected, re-registering...")
                ok = await do_register()
                if ok:
                    try:
                        await do_connect_profiles()
                    except Exception:
                        pass
            if ok:
                return True
        except Exception as e:
            _LOGGER.error("Reconnect attempt %d failed: %s", attempt, e)
    _LOGGER.error("All reconnect attempts failed")
    return False


async def send(msg: str):
    first_line = msg.split("\r\n", 1)[0]
    _LOGGER.debug("[SIP >>>] %s", first_line)
    _log_full(">>>", msg)
    try:
        if R.USE_LOCAL_UDP:
            loop = asyncio.get_running_loop()
            async with lock:
                await loop.sock_sendto(_udp_sock, msg.encode(), _udp_target)
        else:
            async with lock:
                writer.write(msg.encode())
                await writer.drain()
    except Exception as e:
        _LOGGER.error("[SIP >>>] send failed: %s", e)
        raise


def _parse(msg):
    parts = msg.split("\r\n\r\n", 1)
    body = parts[1] if len(parts) > 1 else ""
    lines = parts[0].split("\r\n")
    first = lines[0]
    code = method = None
    if first.startswith("SIP/2.0"):
        try:
            code = int(first.split()[1])
        except (ValueError, IndexError):
            pass
    else:
        method = first.split()[0] if first else None
    hdrs = {}
    via_list = []
    rr_list = []   # Record-Route: più header, l'ordine conta (route set)
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            if key == "via":
                via_list.append(v.strip())
            elif key == "record-route":
                rr_list.append(v.strip())
            hdrs[key] = v.strip()
    if via_list:
        hdrs["_via_all"] = via_list
    if rr_list:
        hdrs["_rr_all"] = rr_list
    return (code or method), hdrs, body, first


def _call_id(hdrs):
    return hdrs.get("call-id", "")


def _tag(header_val):
    for part in header_val.split(";"):
        part = part.strip()
        if part.startswith("tag="):
            return part[4:]
    return ""


# ─── Rilevamento modello dal SIP ────────────────────────────────────────────
# Ogni messaggio SIP ricevuto può portare l'identità del peer: «User-Agent»
# nelle richieste (INVITE/OPTIONS/MESSAGE dal citofono) e «Server» nelle
# risposte. model_detect.match() traduce quella stringa nel nome commerciale.

_seen_uas: set[str] = set()
_model_callback = None


def set_model_callback(cb):
    """Callback(model, fw, user_agent) invocata quando il modello cambia."""
    global _model_callback
    _model_callback = cb


def _learn_peer(hdrs: dict, first: str = "") -> None:
    """Impara il modello del citofono dagli header identificativi."""
    for key in ("user-agent", "server"):
        ua = (hdrs.get(key) or "").strip()
        if not ua or ua == C.USER_AGENT:
            continue
        if ua not in _seen_uas:
            _seen_uas.add(ua)
            _LOGGER.info("SIP peer identificato — %s: %s  [%s]", key, ua, first[:60])
        _apply_ua(ua)


def _apply_ua(ua: str) -> None:
    model, fw, priority = model_detect.match(ua)
    if not model:
        return
    # Un match più specifico (priorità più bassa) sovrascrive quello corrente
    if model == R.DETECTED_MODEL and (fw or "") == R.DETECTED_FW:
        return
    if priority > R.DETECTED_PRIORITY:
        return

    _LOGGER.info("Modello citofono rilevato: %s (fw=%s) da User-Agent «%s»",
                 model, fw or "n/d", ua)
    R.DETECTED_MODEL    = model
    R.DETECTED_FW       = fw or ""
    R.DETECTED_UA       = ua
    R.DETECTED_PRIORITY = priority

    if _model_callback:
        try:
            _model_callback(model, fw or "", ua, priority)
        except Exception:
            _LOGGER.exception("Model callback error")


# ─── Reader tasks ───────────────────────────────────────────────────

async def _send_options_ping():
    """Invia OPTIONS al citofono come keepalive UDP."""
    if not registered:
        return
    cid = _gen("ping-")
    ftag = _gen("")
    branch = _gen()
    target = f"sip:{R.SIP_USER}@{R.SIP_DOMAIN}"
    msg = (f"OPTIONS {target} SIP/2.0\r\n"
           f"{_via_line(branch)}"
           f"Max-Forwards: 70\r\n"
           f"To: <{target}>\r\n"
           f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
           f"Call-ID: {cid}\r\n"
           f"CSeq: 1 OPTIONS\r\n"
           f"User-Agent: {C.USER_AGENT}\r\n"
           f"Content-Length: 0\r\n\r\n")
    try:
        await send(msg)
    except Exception as e:
        _LOGGER.debug("OPTIONS ping failed: %s", e)


async def _dispatch_message(raw: str):
    """Smista un messaggio SIP ricevuto (sia UDP che TCP)."""
    kind, hdrs, body, first = _parse(raw)
    cid = _call_id(hdrs)
    _LOGGER.debug("[SIP <<<] %s", first)
    _log_full("<<<", raw)
    _learn_peer(hdrs, first)

    if isinstance(kind, int):
        # Risposta finale a un INVITE che nessuno aspetta più. Anche il 200 OK
        # ritrasmesso della chiamata in corso: la coda del dialogo (INFO di
        # keyframe) lo ingoiava senza ACK e la targa chiudeva la chiamata.
        orphan = kind >= 200 and hdrs.get("cseq", "").endswith("INVITE") and (
            cid not in pending_responses or (in_call and cid == call_state["call_id"]))
        if orphan:
            _LOGGER.warning("Risposta %d a un INVITE già chiuso (cid=%s)", kind, cid[:24])
            try:  # nel reader TLS un'eccezione qui lo ucciderebbe
                await _close_orphan_invite(kind, hdrs)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Chiusura della risposta orfana fallita")
        elif cid in pending_responses:
            _LOGGER.debug("reader: queuing response %d for cid=%s", kind, cid[:24])
            await pending_responses[cid].put(raw)
        elif cid.startswith("ping-"):
            # Risposta (es. 407) all'OPTIONS keepalive: _send_options_ping non
            # registra il proprio Call-ID in pending_responses per costruzione,
            # quindi è l'esito normale del keepalive, non un errore da segnalare.
            _LOGGER.debug("Keepalive response %d for cid=%s", kind, cid[:24])
        else:
            _LOGGER.warning("Stale response %d for cid=%s", kind, cid[:24])
    elif isinstance(kind, str):
        await incoming_requests.put(raw)


def _via_branch(hdrs: dict) -> str:
    """branch del Via di una risposta, cioè della nostra richiesta (nuovo se manca)."""
    via = (hdrs.get("_via_all") or [""])[0]
    return via.split("branch=", 1)[1].split(";")[0] if "branch=" in via else _gen()


async def _close_orphan_invite(code: int, hdrs: dict, ack: bool = True) -> None:
    """Risposta finale a un nostro INVITE che nessuno aspetta più (annullato,
    scaduto dopo 45 s, task cancellato). Senza ACK la targa ritrasmette la
    risposta e, se era un 200, tiene la chiamata aperta finché non scade: nel
    frattempo risponde 486 a tutto (il «Stale response 200 … (known: [])» del
    log di campo). Un 2xx si chiude con ACK + BYE; il 2xx ritrasmesso della
    chiamata in corso (ACK perso) riceve solo un altro ACK. `ack=False`: l'ACK
    del 2xx è già partito (annullata mentre si apriva il media), solo il BYE."""
    cid = _call_id(hdrs)
    to_hdr, from_hdr = hdrs.get("to", ""), hdrs.get("from", "")
    num = hdrs.get("cseq", "1").split()[0]
    uri = _angle_uri(to_hdr)
    route = _route_line()
    if code >= 300:  # ACK di un non-2xx: stessa transazione, quindi stesso branch dell'INVITE
        branch = _via_branch(hdrs)
    else:  # ACK del 2xx e BYE: nel dialogo, quindi Contact + Record-Route del 2xx
        branch = _gen()
        uri, route = _dialog_target(_contact_uri(hdrs), uri, _route_set(hdrs, reverse=True))
    head = (f"{route}Max-Forwards: 70\r\nTo: {to_hdr}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {cid}\r\n")
    if ack:
        await send(f"ACK {uri} SIP/2.0\r\n{_via_line(branch)}{head}CSeq: {num} ACK\r\n"
                   f"Content-Length: 0\r\n\r\n")
    if code < 300 and not (in_call and cid == call_state["call_id"]):
        _LOGGER.warning("200 OK di una chiamata già annullata (%s): chiudo il dialogo con BYE", cid[:24])
        await send(f"BYE {uri} SIP/2.0\r\n{_via_line(_gen())}{head}CSeq: {int(num) + 1} BYE\r\n"
                   f"User-Agent: {C.USER_AGENT}\r\nContent-Length: 0\r\n\r\n")


async def _udp_reader_task():
    """Loop di lettura per modalità UDP locale."""
    loop = asyncio.get_running_loop()
    _LOGGER.info("SIP UDP reader started")
    last_ping = time.time()
    # Solo il citofono: su 0.0.0.0 chiunque in LAN potrebbe fingere uno squillo
    # (e con l'anteprima farsi mandare media, foto e messaggio di assenza).
    try:
        proxy_ips = {ai[4][0] for ai in await loop.getaddrinfo(
            R.LOCAL_PROXY, C.LOCAL_SIP_PORT, type=socket.SOCK_DGRAM)}
    except OSError:
        proxy_ips = {R.LOCAL_PROXY}
    ignored: set[str] = set()  # un WARNING per mittente, poi DEBUG: un IP sbagliato nelle opzioni si deve leggere

    while True:
        try:
            data, addr = await asyncio.wait_for(
                loop.sock_recvfrom(_udp_sock, 65535), timeout=20)
            if addr[0] not in proxy_ips and addr[0] not in _dialog_hosts():
                _LOGGER.log(logging.DEBUG if addr[0] in ignored else logging.WARNING,
                            "SIP UDP da %s ignorato (non è il citofono %s)", addr[0], R.LOCAL_PROXY)
                ignored.add(addr[0])
                continue
            raw = data.decode(errors="replace")
            await _dispatch_message(raw)
            last_ping = time.time()
        except asyncio.TimeoutError:
            # Keepalive: OPTIONS ogni 20 s se registrato
            if registered and (time.time() - last_ping) >= 20:
                await _send_options_ping()
                last_ping = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.error("SIP UDP reader error: %s", e)
            await asyncio.sleep(2)


def _host_of(uri_or_via: str) -> str:
    """L'host di un URI SIP («sip:55001@192.168.0.5:5060;x») o di un Via
    («SIP/2.0/UDP 192.168.0.5:5060;branch=…»)."""
    s = uri_or_via.split(";")[0].strip()
    s = s.rsplit("@", 1)[-1].split()[-1]
    return s.rsplit(":", 1)[0] if s.count(":") == 1 else s


def _dialog_hosts() -> set[str]:
    """Gli host del dialogo in corso (Contact e Via dello squillo, Contact della chiamata):
    senza Record-Route le richieste nel dialogo (BYE, re-INVITE, INFO) arrivano dalla
    targa stessa, non dal proxy, e il filtro del reader le scartava: HA restava
    «in chiamata» e la targa ritrasmetteva il BYE. Solo host già visti in un
    messaggio accettato, mai indirizzi qualunque della LAN."""
    hosts = set()
    if ringing() or in_call:
        for via in (pending_incoming.get("via_block") or "").split("\r\n"):
            if via.startswith("Via:"):
                hosts.add(_host_of(via[4:]))
        if pending_incoming.get("contact"):
            hosts.add(_host_of(pending_incoming["contact"]))
    if (in_call or calling) and call_state.get("remote_contact"):
        hosts.add(_host_of(call_state["remote_contact"]))
    return hosts


_reconnect_task: asyncio.Task | None = None


async def _reconnect_from_reader():
    """reconnect() in un task a parte, e il reader torna a leggere appena c'è la
    connessione nuova.

    Fino alla 1.0.9 il reader faceva `await reconnect()`: ma la REGISTER di
    reconnect() aspetta risposte che legge solo il reader stesso. Ogni tentativo
    scadeva (15 s), tutti e cinque fallivano (~2,5 min) e la registrazione tornava
    solo col keepalive, fino a due minuti dopo: citofono muto dopo ogni chiusura
    della connessione da parte del cloud.
    """
    old = reader
    task = _reconnect_started()
    while not task.done() and (reader is old or reader is None):
        await asyncio.sleep(0.05)


async def reader_task():
    if R.USE_LOCAL_UDP:
        await _udp_reader_task()
        return

    # ── TLS/TCP reader (originale) ────────────────────────────────
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(8192), timeout=30)
            if not chunk:
                _LOGGER.warning("SIP connection closed by server, reconnecting...")
                await _reconnect_from_reader()
                buf = b""
                continue
            buf += chunk
            if len(buf) > 1_000_000:  # guard: 1 MB max — evita OOM su messaggi malformati
                _LOGGER.error("SIP TCP buffer overflow (>1 MB); reset connessione")
                await _reconnect_from_reader()
                buf = b""
                continue
        except asyncio.TimeoutError:
            # Send CRLF keepalive (RFC 5626) to prevent proxy from
            # considering TLS connection stale
            try:
                async with lock:
                    writer.write(b"\r\n\r\n")
                    await writer.drain()
            except Exception:
                _LOGGER.warning("CRLF keepalive failed, reconnecting...")
                await _reconnect_from_reader()
                buf = b""
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.error("SIP reader error: %s, reconnecting...", e)
            await _reconnect_from_reader()
            buf = b""
            await asyncio.sleep(2)
            continue

        while b"\r\n\r\n" in buf:
            hdr_end = buf.index(b"\r\n\r\n") + 4
            hdr_text = buf[:hdr_end].decode(errors="replace")
            cl = 0
            for line in hdr_text.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    try:
                        # max(0): un Content-Length negativo non faceva avanzare il
                        # buffer e il ciclo girava all'infinito, senza mai cedere l'event loop.
                        cl = max(0, int(line.split(":", 1)[1].strip()))
                    except ValueError:
                        pass
            total = hdr_end + cl
            if len(buf) < total:
                break
            raw = buf[:total].decode(errors="replace")
            buf = buf[total:]
            await _dispatch_message(raw)


def _clen(body: str) -> int:
    """`Content-Length` di un corpo: in **byte**, non in caratteri.

    Fino alla 1.0.6 era `len(body)`: con un corpo non ASCII (`NICK;Cucina è`) si
    dichiaravano 13 byte e se ne spedivano 14. In UDP il corpo arrivava troncato; in
    TCP/TLS il byte in più veniva letto come inizio del messaggio successivo.
    """
    return len(body.encode("utf-8"))


# Ritrasmissione delle richieste non-INVITE su UDP (RFC 3261 §17.1.2.2, Timer E).
# L'app ufficiale non ne ha bisogno perché in locale usa TCP; noi usiamo UDP, e
# fino alla 1.0.6 ogni richiesta partiva una volta sola: un datagramma perso dava
# «REGISTER: nessuna risposta finale utile (0 risposte)» e due minuti di
# «Non registrato» fino al giro successivo del keepalive.
_T1 = 0.5   # primo intervallo di ritrasmissione (s)
_T2 = 4.0   # intervallo massimo (s)


async def _send_request(msg: str, cid: str, timeout: float = 15) -> list[str]:
    """Invia una richiesta non-INVITE e ne raccoglie le risposte fino alla finale.

    Due differenze rispetto a `send()` + `_wait_final()`:

    * la coda delle risposte esiste **prima** dell'invio: il reader scarta come
      «stale» le risposte di un Call-ID senza coda, e il citofono risponde in
      poche decine di millisecondi;
    * su UDP la richiesta viene **ritrasmessa identica** (stesso branch, quindi la
      stessa transazione per il server) a 0,5 - 1 - 2 - 4 - 4 … secondi, finché non
      arriva una risposta qualsiasi. Su TCP/TLS il trasporto è affidabile e non si
      ritrasmette.
    """
    q = pending_responses.setdefault(cid, asyncio.Queue())
    # La coda di un dialogo (BYE) raccoglie anche i 200 degli INFO di keyframe:
    # conta solo la risposta con il CSeq di questa richiesta.
    want = _parse(msg)[1].get("cseq", "").split()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    retransmit = bool(R.USE_LOCAL_UDP)
    interval = _T1
    next_tx = loop.time() + interval
    results: list[str] = []
    try:
        await send(msg)  # anche un invio fallito toglie la coda (finally)
        # Coda tolta da altri: BYE incrociati, handle_incoming_bye ha chiuso il dialogo e
        # la risposta al nostro BYE non arriverà qui. Si esce subito, non dopo `timeout`.
        while pending_responses.get(cid) is q:
            now = loop.time()
            if now >= deadline:
                break
            wake = min(deadline, next_tx) if retransmit else deadline
            try:
                # ponytail: al massimo 0,25 s per volta, per accorgersi della coda tolta
                raw = await asyncio.wait_for(q.get(), timeout=max(0.0, min(wake - now, 0.25)))
            except asyncio.TimeoutError:
                if retransmit and loop.time() >= next_tx and loop.time() < deadline:
                    interval = min(interval * 2, _T2)
                    next_tx = loop.time() + interval
                    _LOGGER.debug("ritrasmissione cid=%s (prossima tra %.1fs)", cid[:24], interval)
                    await send(msg)
                continue
            kind, hdrs, *_ = _parse(raw)
            if want and hdrs.get("cseq", "").split() != want:
                continue
            results.append(raw)
            # Una risposta, anche provvisoria, dice che la richiesta è arrivata.
            retransmit = False
            if isinstance(kind, int) and kind >= 200:
                break
    finally:
        pending_responses.pop(cid, None)
    return results


async def _wait_final(cid, timeout=15):
    q = pending_responses.setdefault(cid, asyncio.Queue())
    results = []
    deadline = time.time() + timeout
    while True:
        rem = deadline - time.time()
        if rem <= 0:
            break
        try:
            raw = await asyncio.wait_for(q.get(), timeout=min(rem, 3))
            results.append(raw)
            kind, *_ = _parse(raw)
            if isinstance(kind, int) and kind >= 200:
                break
        except asyncio.TimeoutError:
            continue
    pending_responses.pop(cid, None)
    return results


# ─── SDP ────────────────────────────────────────────────────────────

_local_crypto_key = None
_local_video_crypto_key = None


# H.264 (RFC 6184): packetization-mode e profile-level-id vanno rispettati.
# La targa 2F (40507, baresip) offre e accetta SOLO packetization-mode=0
# (profile-level-id 42800c); le 2FV2 accettano il mode 1. Rispondere mode 1 a
# un'offerta mode 0 è una risposta senza codec in comune: la targa non manda
# un pacchetto video (prova del 28/09: audio rx=3602, video rx=0 in 54 s), e
# a un'offerta con il solo mode 1 risponde m=video 0 (probe di agosto).
_H264_OFFER = (
    ("96", "profile-level-id=42801F;packetization-mode=1"),
    ("97", "profile-level-id=42800c;packetization-mode=0"),
)


def _h264_answer(offer: dict | None) -> tuple[tuple[str, str], ...]:
    """Le linee H.264 della risposta: il payload type e i parametri che la
    targa ha offerto (il primo H.264 dell'offerta). Senza offerta, o senza
    H.264 nell'offerta, l'offerta completa (mode 1 e mode 0)."""
    video = (offer or {}).get("video") or {}
    for line in video.get("rtpmap", []):
        head, _, codec = line.partition(" ")
        if not codec.upper().startswith("H264/"):
            continue
        pt = head.split(":", 1)[1]
        fmtp = ""
        for f in video.get("fmtp", []):
            fhead, _, params = f.partition(" ")
            if fhead.split(":", 1)[1] == pt:
                fmtp = params.strip()
        keep = [kv for kv in fmtp.split(";")
                if kv.split("=", 1)[0].strip().lower() in ("packetization-mode", "profile-level-id")]
        return ((pt, ";".join(k.strip() for k in keep) or "packetization-mode=0"),)
    return _H264_OFFER


def build_sdp(offer: dict | None = None):
    """Costruisce l'offerta/risposta SDP.

    Su questo impianto (verificato sul campo 20/08/2026 verso la targa 55100)
    il media viaggia in RTP IN CHIARO: se si offre SRTP (RTP/SAVP + a=crypto)
    la targa baresip non risponde e tutto il media fallisce. Perciò il default
    è RTP/AVP senza a=crypto. SRTP resta disponibile via R.MEDIA_ENC=True per
    impianti che negoziano media_enc.
    """
    global _local_crypto_key, _local_video_crypto_key
    sid = str(int(time.time()))
    import base64 as _b64

    enc = bool(getattr(R, "MEDIA_ENC", False))
    proto = "RTP/SAVP" if enc else "RTP/AVP"

    if enc:
        _local_crypto_key = _b64.b64encode(os.urandom(30)).decode()
        _local_video_crypto_key = _b64.b64encode(os.urandom(30)).decode()
        audio_crypto = f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_local_crypto_key}\r\n"
        video_crypto = f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_local_video_crypto_key}\r\n"
    else:
        # RTP in chiaro: nessuna chiave locale → setup_media non crea srtp_tx/rx.
        _local_crypto_key = None
        _local_video_crypto_key = None
        audio_crypto = ""
        video_crypto = ""

    h264 = _h264_answer(offer)
    h264_lines = "".join(
        f"a=rtpmap:{pt} H264/90000\r\n"
        f"a=fmtp:{pt} {fmtp}\r\n"
        f"a=rtcp-fb:{pt} ccm fir\r\n"
        f"a=rtcp-fb:{pt} nack\r\n"
        f"a=rtcp-fb:{pt} nack pli\r\n"
        for pt, fmtp in h264
    )

    return (
        f"v=0\r\n"
        f"o=- {sid} {sid} IN IP4 {MY_IP}\r\n"
        f"s=Talk\r\n"
        f"c=IN IP4 {MY_IP}\r\n"
        f"b=AS:512\r\n"
        f"t=0 0\r\n"
        f"a=rtcp-xr:rcvr-rtt=all:10000 stat-summary=loss,dup,jitt,TTL voip-metrics\r\n"
        f"m=audio {C.RTP_AUDIO_PORT} {proto} 0 8 101\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
        f"a=rtpmap:8 PCMA/8000\r\n"
        f"a=rtpmap:101 telephone-event/8000\r\n"
        f"a=fmtp:101 0-15\r\n"
        f"a=ptime:20\r\n"
        f"a=sendrecv\r\n"
        f"{audio_crypto}"
        f"m=video {C.RTP_VIDEO_PORT} {proto} {' '.join(pt for pt, _ in h264)}\r\n"
        f"b=AS:256\r\n"
        f"{h264_lines}"
        f"a=sendrecv\r\n"
        f"{video_crypto}"
    )


def parse_sdp(sdp_text):
    result = {"audio": {}, "video": {}, "conn": ""}
    m = None
    for line in sdp_text.split("\n"):
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
            if m:
                result[m]["ip"] = ip
            else:
                result["conn"] = ip
        elif line.startswith("m=audio"):
            m = "audio"
            parts = line.split()
            result["audio"]["port"] = int(parts[1])
            result["audio"]["fmts"] = parts[3:]
        elif line.startswith("m=video"):
            m = "video"
            parts = line.split()
            result["video"]["port"] = int(parts[1])
            result["video"]["fmts"] = parts[3:]
        elif line[2:] in ("sendrecv", "sendonly", "recvonly", "inactive") and line.startswith("a="):
            result.setdefault(m or "session", {})["dir"] = line[2:]
        elif line.startswith("a=rtpmap:") and m:
            result[m].setdefault("rtpmap", []).append(line)
        elif line.startswith("a=fmtp:") and m:
            result[m].setdefault("fmtp", []).append(line)
        elif line.startswith("a=crypto:") and m:
            parts = line.split()
            for p in parts:
                if p.startswith("inline:"):
                    result[m]["crypto_key"] = p[7:]
                    break
    session_dir = result.pop("session", {}).get("dir")
    for section in ("audio", "video"):
        if section in result and "ip" not in result[section]:
            result[section]["ip"] = result["conn"]
        if session_dir and "dir" not in result[section]:
            result[section]["dir"] = session_dir
    return result


# ─── Operations ─────────────────────────────────────────────────────

async def do_register():
    # In UDP mode writer is always None — skip the TLS connect check
    if R.USE_LOCAL_UDP:
        if _udp_sock is None:
            await connect()
    elif not writer or writer.is_closing():
        await connect()

    global local_tag
    local_tag = _gen("")
    cid = _gen("reg-")
    uri = f"sip:{R.SIP_DOMAIN}"

    def _msg(auth=None, seq=1):
        branch = _gen()
        m = (f"REGISTER {uri} SIP/2.0\r\n"
             f"{_via_line(branch)}"
             f"{_route_line()}"
             f"Max-Forwards: 70\r\n"
             f"To: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>\r\n"
             f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={local_tag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} REGISTER\r\n"
             f"Contact: {_contact_hdr()}\r\n"
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Mobile-IMEI: {R.DEVICE_IMEI}\r\n"
             f"MyName: {C.MY_NAME}\r\n"
             f"Supported: replaces,outbound,gruu\r\n"
             f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n")
        if auth:
            m += f"Authorization: {auth}\r\n"
        return m + "Content-Length: 0\r\n\r\n"

    s1 = _next_cseq()
    resps = await _send_request(_msg(seq=s1), cid)

    # Ogni uscita negativa deve azzerare `registered`: e' anche il keepalive a
    # chiamare questa funzione, e fino alla 1.0.5 un fallimento lasciava il flag
    # a True. Home Assistant dichiarava il citofono raggiungibile mentre non lo
    # era piu', e il binary_sensor restava verde a impianto spento.
    for r in resps:
        code, hdrs, *_ = _parse(r)
        if code == 401:
            ch = hdrs.get("www-authenticate", "")
            if not ch:
                _LOGGER.warning("REGISTER: 401 senza challenge, registrazione fallita")
                _set_registered(False)
                return False
            auth = _make_auth("REGISTER", uri, ch)
            for r2 in await _send_request(_msg(auth=auth, seq=_next_cseq()), cid):
                if _parse(r2)[0] == 200:
                    _set_registered(True)
                    _LOGGER.info("SIP registered successfully")
                    return True
            _LOGGER.warning("REGISTER: nessun 200 dopo l'autenticazione")
            _set_registered(False)
            return False
        elif code == 200:
            _set_registered(True)
            _LOGGER.info("SIP registered successfully")
            return True
    # Diagnostica: "0 risposte" = nulla è tornato nemmeno dopo le ritrasmissioni;
    # altrimenti diciamo quali codici sono arrivati, che prima non si leggevano.
    codes = [_parse(r)[0] for r in resps]
    _LOGGER.warning(
        "REGISTER: nessuna risposta finale utile (%d risposte%s) verso %s via %s",
        len(resps), f": {codes}" if codes else "",
        R.LOCAL_PROXY if R.USE_LOCAL_UDP else R.SIP_PROXY,
        "UDP" if R.USE_LOCAL_UDP else "TLS",
    )
    _set_registered(False)
    return False


async def do_system_message(target_uri, body_text, extra_headers=None):
    if not registered:
        _LOGGER.warning("do_system_message: not registered, target=%s body=%s", target_uri, body_text)
        return False, "Non registrato"
    _LOGGER.info("do_system_message: target=%s body=%s headers=%s", target_uri, body_text, extra_headers)
    ftag = _gen("")
    # Come l'app ufficiale (MakeCallModel/SystemMsg): Call-ID = 10 caratteri alfanumerici
    cid = ''.join(random.choices(string.ascii_letters + string.digits, k=10))

    def _msg(auth=None, seq=1):
        branch = _gen()
        m = (f"MESSAGE {target_uri} SIP/2.0\r\n"
             f"{_via_line(branch)}"
             f"{_route_line()}"
             f"Max-Forwards: 70\r\n"
             f"To: <{target_uri}>\r\n"
             f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} MESSAGE\r\n"
             f"Contact: {_simple_contact()}\r\n"
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Mobile-IMEI: {R.DEVICE_IMEI}\r\n"
             f"MyName: {C.MY_NAME}\r\n")
        if extra_headers:
            for k, v in extra_headers.items():
                m += f"{k}: {v}\r\n"
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        m += (f"Content-Type: text/plain\r\n"
              f"Content-Length: {_clen(body_text)}\r\n\r\n{body_text}")
        return m

    for r in await _send_request(_msg(seq=_next_cseq()), cid, timeout=15):
        code, hdrs, *_ = _parse(r)
        _LOGGER.info("do_system_message: response %s for %s", code, target_uri)
        if code and code < 200:
            continue
        if code in (401, 407):
            ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
            if not ch:
                return False, f"Auth vuoto ({code})"
            _retry_auth(ch, 0, None)  # memorizza la sfida: gli INFO di keyframe partono autenticati
            auth = _make_auth("MESSAGE", target_uri, ch)
            for r2 in await _send_request(_msg(auth=auth, seq=_next_cseq()), cid, timeout=15):
                c2 = _parse(r2)[0]
                _LOGGER.info("do_system_message: auth response %s for %s", c2, target_uri)
                if c2 and 200 <= c2 < 300:
                    return True, f"OK ({c2})"
                if c2 and c2 >= 300:
                    return False, f"Errore: {c2}"
            return False, "Timeout"
        if code and 200 <= code < 300:
            return True, f"OK ({code})"
        if code and code >= 300:
            return False, f"Errore: {code}"
    return False, "Timeout"


async def do_call(target=None):
    """INVITE a SIP target (default: intercom targa 55001)."""
    if not registered:
        _LOGGER.error("do_call: NOT registered")
        return False, "Non registrato"
    if in_call or calling:
        _LOGGER.error("do_call: already in call/calling")
        return False, "Già in chiamata"
    if ringing():
        # Una chiamata ora sostituirebbe il media (e le chiavi) dell'anteprima dello squillo.
        return False, "Squillo in corso"

    _set_calling(True)
    target_uri = target or R.INTERCOM
    _LOGGER.info("do_call: target=%s", target_uri)
    ftag = _gen("")
    cid = _gen("call-")
    sdp = build_sdp()
    call_state["call_id"] = cid
    call_state["from_tag"] = ftag
    call_state["original_target"] = target_uri
    call_state["local_sdp"] = sdp

    vimar_callid = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    inv_branch = ""

    def _inv(auth=None, seq=1):
        nonlocal inv_branch
        branch = inv_branch = _gen()
        m = (f"INVITE {target_uri} SIP/2.0\r\n"
             f"{_via_line(branch)}"
             f"{_route_line()}"
             f"Max-Forwards: 70\r\n"
             f"To: <{target_uri}>\r\n"
             f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} INVITE\r\n"
             f"Contact: {_simple_contact()}"
             f';+sip.instance="<urn:uuid:{R.DEVICE_UUID}>"\r\n'
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Supported: replaces,outbound,gruu,timer\r\n"
             f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n"
             f"Session-Expires: 600;refresher=uas\r\n"
             f"Min-SE: 90\r\n")
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        m += (f"Mobile-IMEI: {R.DEVICE_IMEI}\r\n"
              f"MyName: {C.MY_NAME}\r\n"
              f"X-Call-ID: {vimar_callid}\r\n"
              f"Content-Type: application/sdp\r\n"
              f"Content-Length: {_clen(sdp)}\r\n\r\n{sdp}")
        return m

    def _ack(to_tag, seq, branch=None):
        # ACK di un non-2xx: fa parte della transazione dell'INVITE, quindi porta il
        # suo branch (RFC 3261 §17.1.1.3) e la stessa strada (Route fisso). Con un
        # branch nuovo la targa/proxy non lo riconosce e continua a ritrasmettere
        # il 4xx/6xx. ACK del 2xx: transazione nuova, nel dialogo (§13.2.2.4).
        if branch:
            uri, route = target_uri, _route_line()
        else:
            branch = _gen()
            uri, route = _dialog_target(call_state["remote_contact"], target_uri,
                                        call_state["route_set"])
        to_hdr = f"<{target_uri}>"
        if to_tag:
            to_hdr += f";tag={to_tag}"
        return (f"ACK {uri} SIP/2.0\r\n"
                f"{_via_line(branch)}"
                f"{route}"
                f"Max-Forwards: 70\r\n"
                f"To: {to_hdr}\r\n"
                f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
                f"Call-ID: {cid}\r\n"
                f"CSeq: {seq} ACK\r\n"
                f"Content-Length: 0\r\n\r\n")

    async def _cancel():
        # Stessa transazione dell'INVITE in corso: stesso branch, stesso CSeq.
        # Il 487 (o il 200 incrociato) lo chiude poi _close_orphan_invite.
        try:
            await send(f"CANCEL {target_uri} SIP/2.0\r\n"
                       f"{_via_line(inv_branch)}"
                       f"{_route_line()}"
                       f"Max-Forwards: 70\r\n"
                       f"To: <{target_uri}>\r\n"
                       f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
                       f"Call-ID: {cid}\r\n"
                       f"CSeq: {cur_seq} CANCEL\r\n"
                       f"Content-Length: 0\r\n\r\n")
        except Exception as e:  # noqa: BLE001
            # TLS caduto: la chiamata è annullata lo stesso, l'esito torna a chi chiama
            # (prima usciva un'eccezione da do_call).
            _LOGGER.warning("CANCEL non inviato (%s): annullo in locale", e)

    cur_seq, inv_msg, retx, retx_iv, retx_at = 0, "", False, _T1, 0.0

    async def _invite(auth=None):
        nonlocal cur_seq, inv_msg, retx, retx_iv, retx_at
        cur_seq = _next_cseq()
        inv_msg = _inv(auth=auth, seq=cur_seq)
        await send(inv_msg)
        # UDP: l'INVITE si ritrasmette finché arriva una risposta qualsiasi (Timer A,
        # RFC 3261 §17.1.1.2). Perso il primo, "Vedi esterno" aspettava 45 s.
        retx, retx_iv = bool(R.USE_LOCAL_UDP), _T1
        retx_at = time.time() + retx_iv

    await _invite()
    await broadcast("log", "INVITE inviato...")

    q = pending_responses.setdefault(cid, asyncio.Queue())
    _LOGGER.debug("do_call: cid=%s, q id=%s, pending_keys=%s", cid[:24], id(q), list(pending_responses.keys())[:3])
    deadline = time.time() + 45
    auth_tries, last_ch = 0, None

    try:
        while time.time() < deadline:
            _LOGGER.debug("do_call: waiting q.get (qsize=%d, cid_in_pending=%s, q_is_same=%s)",
                           q.qsize(), cid in pending_responses, pending_responses.get(cid) is q)
            try:
                wait = min(3, max(0.01, retx_at - time.time())) if retx else 3
                raw = await asyncio.wait_for(q.get(), timeout=wait)
            except asyncio.TimeoutError:
                if retx and time.time() >= retx_at:
                    await send(inv_msg)
                    retx_iv *= 2
                    retx_at = time.time() + retx_iv
                continue

            if raw is _CANCEL:  # do_hangup durante lo squillo della targa
                await _cancel()
                return False, "Annullata"

            code, hdrs, body, first = _parse(raw)
            if "INVITE" not in hdrs.get("cseq", "INVITE"):
                continue  # non è una risposta all'INVITE (es. il 200 di un CANCEL)
            ttag = _tag(hdrs.get("to", ""))
            _LOGGER.debug("do_call: response %s (body=%dB)", code, len(body) if body else 0)
            retx = False

            if code in (100, 180, 183):
                if code == 183 and body:
                    with contextlib.suppress(ValueError):  # media fuori LAN: ignorato
                        call_state["remote_sdp"] = _remote_media(body)
                continue

            if code in (401, 407):
                rseq = int(hdrs.get("cseq", "0").split()[0] or 0)
                await send(_ack(ttag, rseq, _via_branch(hdrs)))
                if rseq != cur_seq:
                    continue  # ritrasmissione di una sfida già soddisfatta: solo l'ACK
                ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
                if not ch:
                    pending_responses.pop(cid, None)
                    _set_calling(False)
                    return False, f"Auth vuoto ({code})"
                if not _retry_auth(ch, auth_tries, last_ch):
                    _LOGGER.error("INVITE: %d dopo %d tentativi autenticati, mi fermo", code, auth_tries)
                    pending_responses.pop(cid, None)
                    _set_calling(False)
                    return False, f"Autenticazione rifiutata ({code})"
                auth_tries, last_ch = auth_tries + 1, ch
                await _invite(_make_auth("INVITE", target_uri, ch))
                continue

            if 200 <= code < 300:
                try:
                    remote = _remote_media(body) if body else None
                except ValueError as e:  # UDP locale: SDP verso indirizzi fuori LAN
                    _LOGGER.error("200 OK rifiutato: %s", e)
                    await _close_orphan_invite(code, hdrs)  # ACK + BYE
                    return False, "SDP rifiutato"
                call_state["to_tag"] = ttag
                call_state["remote_contact"] = _contact_uri(hdrs)
                call_state["route_set"] = _route_set(hdrs, reverse=True)

                async def _annullata(acked: bool):
                    # do_hangup ha azzerato `calling` mentre si lavorava il 200 OK (già
                    # in coda, o durante ACK/setup_media): ha chiuso media e stato
                    # (call_ended), e la chiamata ripartiva lo stesso, senza nessuno
                    # che la chiudesse. Il dialogo aperto dal 200 si chiude con BYE.
                    await _close_orphan_invite(code, hdrs, ack=not acked)
                    await media.stop_media()
                    if call_state["call_id"] == cid:
                        _clear_call_state()
                    return False, "Annullata"

                if not calling:
                    return await _annullata(acked=False)
                await send(_ack(ttag, cur_seq))
                if not calling:
                    return await _annullata(acked=True)

                if remote:
                    call_state["remote_sdp"] = remote
                    _LOGGER.info("SDP: audio=%s video=%s", remote.get('audio', {}), remote.get('video', {}))
                    await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)
                    if not calling:
                        return await _annullata(acked=True)

                _set_in_call(True)
                _set_calling(False)
                await broadcast("call_started", "Connesso!")
                # Request keyframe immediately — no delay
                await send_keyframe_request()
                pending_responses.pop(cid, None)
                return True, "Connesso!"

            if code >= 300:
                _LOGGER.error("INVITE rejected: %d", code)
                await _close_orphan_invite(code, hdrs)  # ACK nella transazione dell'INVITE
                pending_responses.pop(cid, None)
                _set_calling(False)
                reason = first.split(" ", 2)[2] if first.count(" ") >= 2 else str(code)
                return False, f"{code} {reason}"

        pending_responses.pop(cid, None)
        _set_calling(False)
        _LOGGER.error("INVITE timeout (45s) for %s", target_uri)
        await _cancel()  # la targa non resti a squillare (e a rispondere dopo)
        return False, "Timeout (45s)"
    finally:
        # Ogni uscita dalla transazione — return, timeout o eccezione sollevata
        # da parse_sdp()/setup_media() dopo il 200 OK — deve liberare la coda e
        # azzerare `calling`: senza questo il flag resta True per sempre e la
        # guardia a inizio funzione blocca ogni chiamata successiva.
        pending_responses.pop(cid, None)
        _set_calling(False)


async def send_keyframe_request():
    """Send SIP INFO picture_fast_update to get a video keyframe (SPS/PPS).

    L'INFO è in-dialog: request-URI = Contact del peer (55100/targa), non
    l'hardcode R.INTERCOM (55001). Gestisce la challenge 407/401 rispedendo
    con Proxy-Authorization (come MESSAGE/INVITE) invece di lasciare il proxy
    a rifiutare a ripetizione ("Stale response 407")."""
    if not in_call or not call_state["call_id"]:
        return
    to_uri = call_state.get("original_target") or R.INTERCOM
    info_target, route = _dialog_target(call_state.get("remote_contact"), to_uri,
                                        call_state.get("route_set"))
    cid = call_state["call_id"]
    body = ('<?xml version="1.0" encoding="utf-8" ?>'
            '<media_control><vc_primitive><to_encoder>'
            '<picture_fast_update></picture_fast_update>'
            '</to_encoder></vc_primitive></media_control>')
    seq = 0

    def _info(auth=None):
        nonlocal seq
        seq = _next_cseq()
        m = (
            f"INFO {info_target} SIP/2.0\r\n"
            f"{_via_line(_gen())}"
            f"{route}"
            f"Max-Forwards: 70\r\n"
            f"To: <{to_uri}>;tag={call_state['to_tag']}\r\n"
            f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={call_state['from_tag']}\r\n"
            f"Call-ID: {cid}\r\n"
            f"CSeq: {seq} INFO\r\n")
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        m += (f"Content-Type: application/media_control+xml\r\n"
              f"Content-Length: {_clen(body)}\r\n\r\n{body}")
        return m

    # Registra la coda PRIMA di inviare: la risposta arriva sul Call-ID del
    # dialog attivo, che _wait_final consuma; senza questo il reader la scarta
    # come "Stale response".
    pending_responses.setdefault(cid, asyncio.Queue())
    # Già autenticato con l'ultima sfida del proxy: il burst iniziale (8 INFO a
    # 150 ms) faceva 8 volte 407 + INFO ripetuto. Se la sfida è vecchia arriva un
    # 407 stale=true e si rimanda con quella nuova, aspettandone la risposta.
    auth_tries, last_ch = 0, None
    await send(_info(_make_auth("INFO", info_target, _proxy_challenge) if _proxy_challenge else None))
    _LOGGER.debug("Sent INFO picture_fast_update → %s", info_target)

    # Attendi risposta breve; su 407/401 rimanda con auth (al più AUTH_RETRIES).
    #
    # Le risposte che non sono per il nostro INFO (es. il 200 di un BYE, se un
    # hangup concorrente condivide la coda del dialog) vanno restituite alla coda,
    # ma solo **dopo** il ciclo. Fino alla 1.0.6 venivano rimesse in coda subito e
    # rilette al giro successivo: da Python 3.12 `asyncio.wait_for` su una coda già
    # piena non sospende, quindi il ciclo girava senza mai cedere l'event loop fino
    # alla scadenza — Home Assistant fermo 3 secondi, ogni 5 secondi, per tutta la
    # chiamata (misurato: 0,01 s su 3.11, 3,01 s su 3.12 e 3.13).
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3
    foreign: list[str] = []
    try:
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    pending_responses[cid].get(), timeout=min(1.0, deadline - loop.time())
                )
            except (asyncio.TimeoutError, KeyError):
                break
            code, hdrs, *_ = _parse(raw)
            cseq = hdrs.get("cseq", "").split()
            if "INFO" not in hdrs.get("cseq", "INFO"):
                foreign.append(raw)
                continue
            if cseq != [str(seq), "INFO"]:
                continue  # risposta a un INFO precedente (es. il 200 dopo un 407): non è la nostra
            if not isinstance(code, int) or code < 200:
                continue
            if code in (401, 407):
                ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
                if _retry_auth(ch, auth_tries, last_ch):
                    auth_tries, last_ch = auth_tries + 1, ch
                    await send(_info(auth=_make_auth("INFO", info_target, ch)))
                    _LOGGER.debug("Re-sent INFO with Proxy-Authorization after %d", code)
                    continue  # e si aspetta la risposta di QUESTO INFO (seq nuovo)
            break
    finally:
        queue = pending_responses.get(cid)
        for raw in foreign:
            if queue is None:
                break
            try:
                queue.put_nowait(raw)
            except asyncio.QueueFull:
                break
    # Nota: NON facciamo pop() qui — un do_call/_wait_final concorrente potrebbe
    # possedere la stessa coda. La coda del dialog viene ripulita a hangup.


async def do_hangup():
    if calling and (q := pending_responses.get(call_state["call_id"])):
        # "Annulla" mentre la targa squilla: do_call manda il CANCEL ed esce. Fino
        # alla 1.0.9 do_call restava in attesa e, al 200 OK, la chiamata partiva
        # lo stesso dopo il riaggancio.
        q.put_nowait(_CANCEL)
    _set_calling(False)
    if not in_call or not call_state["call_id"]:
        _set_in_call(False)
        if not ringing():  # non spegnere l'anteprima di uno squillo
            await media.stop_media()
        await broadcast("call_ended", "Chiamata terminata")
        return

    cid = call_state["call_id"]
    ftag = call_state["from_tag"] or local_tag or _gen("")
    ttag = call_state["to_tag"] or ""

    to_uri = call_state.get("original_target") or R.INTERCOM
    target_uri, route = _dialog_target(call_state.get("remote_contact"), to_uri,
                                       call_state.get("route_set"))
    to_hdr = f"<{to_uri}>"
    if ttag:
        to_hdr += f";tag={ttag}"

    bye = (f"BYE {target_uri} SIP/2.0\r\n"
           f"{_via_line(_gen())}"
           f"{route}"
           f"Max-Forwards: 70\r\n"
           f"To: {to_hdr}\r\n"
           f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
           f"Call-ID: {cid}\r\n"
           f"CSeq: {_next_cseq()} BYE\r\n"
           f"User-Agent: {C.USER_AGENT}\r\n"
           f"Content-Length: 0\r\n\r\n")
    # INFO: nei log del campo si deve leggere chi ha chiuso, noi o il citofono.
    _LOGGER.info("BYE inviato da Home Assistant (call %s)", cid[:24])
    # La chiamata è finita quando il BYE parte (RFC 3261 §15.1.1), non quando
    # risponde: media spento e stato idle subito. Fino alla 1.0.10 si aspettava la
    # risposta, e sul cloud (BYE mai risposto) la card restava "in chiamata" per
    # 5 s, finché non chiudeva la targa. call_state resta finché non è risposto,
    # così il BYE incrociato della targa trova ancora il dialogo (200, non 481).
    _set_in_call(False)
    await media.stop_media()
    await broadcast("call_ended", "Chiamata terminata")
    try:
        # Come le altre non-INVITE: su UDP ritrasmesso finché la targa risponde. Un
        # BYE perso la lasciava occupata (486 al "Vedi esterno" successivo).
        await _send_request(bye, cid, timeout=5)
    except Exception as e:  # noqa: BLE001
        # Connessione caduta (TLS cloud in riconnessione): la chiamata si chiude
        # comunque qui. Prima restava in_call per sempre, col media acceso, il timer
        # di durata massima già annullato e il citofono sordo (486 a ogni squillo).
        _LOGGER.warning("BYE non inviato (%s): chiudo la chiamata in locale", e)
    if call_state["call_id"] == cid:
        # Altrimenti BYE incrociati: handle_incoming_bye ha già ripulito, o è già
        # arrivato altro (l'anteprima di uno squillo, un'altra chiamata).
        _clear_call_state()


def _clear_call_state() -> None:
    call_state.update(call_id=None, from_tag=None, to_tag=None, remote_contact=None,
                      remote_sdp=None, original_target=None, route_set=None, local_sdp=None)


async def do_options(target=None):
    if not registered:
        return False, "Non registrato"
    target = target or R.INTERCOM
    ftag = _gen("")
    cid = _gen("opt-")

    def _msg(auth=None, seq=1):
        branch = _gen()
        m = (f"OPTIONS {target} SIP/2.0\r\n"
             f"{_via_line(branch)}"
             f"{_route_line()}"
             f"Max-Forwards: 70\r\n"
             f"To: <{target}>\r\n"
             f"From: <sip:{R.SIP_USER}@{R.SIP_DOMAIN}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} OPTIONS\r\n"
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Accept: application/sdp\r\n")
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        return m + "Content-Length: 0\r\n\r\n"

    await send(_msg(seq=_next_cseq()))
    for r in await _wait_final(cid):
        code, hdrs, *_ = _parse(r)
        if code and code < 200:
            continue
        if code in (401, 407):
            ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
            if not ch:
                return False, "Auth vuoto"
            await send(_msg(auth=_make_auth("OPTIONS", target, ch), seq=_next_cseq()))
            for r2 in await _wait_final(cid):
                c2 = _parse(r2)[0]
                if c2 and 200 <= c2 < 300:
                    return True, f"OK: {c2}"
                return False, f"Errore: {c2}"
            return False, "Timeout"
        if code and 200 <= code < 300:
            return True, f"OK: {code}"
        if code and code >= 300:
            return False, f"Errore: {code}"
    return False, "Timeout"


async def do_connect_profiles():
    """Register push profile on Vimar cloud. Uses Digest auth (not Basic)."""
    username = f"{R.SIP_USER}@{R.SIP_DOMAIN}"
    body = [{"sipid": R.SIP_USER, "domain": R.SIP_DOMAIN, "pntok": C.PN_TOKEN}]
    if not C.PN_TOKEN:
        return False, "No FCM token"

    import requests as req_lib
    loop = asyncio.get_running_loop()

    def _call(endpoint):
        return req_lib.post(
            f"https://ipvdes.vimar.cloud/eipvdesUtils/{endpoint}",
            json=body,
            auth=req_lib.auth.HTTPDigestAuth(username, C.PN_TOKEN),
            headers={"Accept": "application/json"}, timeout=15)

    try:
        resp = await loop.run_in_executor(None, _call, "connectProfiles")
        _LOGGER.info("connectProfiles: %d", resp.status_code)
        if resp.status_code == 200:
            return True, "Profilo connesso"
        if resp.status_code == 403:
            await loop.run_in_executor(None, _call, "disconnectProfiles")
            resp3 = await loop.run_in_executor(None, _call, "connectProfiles")
            _LOGGER.info("connectProfiles retry: %d", resp3.status_code)
            if resp3.status_code == 200:
                return True, "Profilo connesso"
            return False, f"connectProfiles: {resp3.status_code}"
        return False, f"connectProfiles: {resp.status_code}"
    except Exception as e:
        _LOGGER.error("connectProfiles error: %s", e)
        return False, str(e)


# ─── Incoming SIP ───────────────────────────────────────────────────

pending_incoming = {
    "active": False, "cid": None, "branch": None, "from_hdr": None, "to_hdr": None,
    "cseq": None, "via_block": None, "my_tag": None,
    "caller_uri": None, "caller_tag": None, "body": None,
    "sdp": None, "early": False, "resp": None,
    "contact": None, "route_set": None,   # remote target e route set del dialogo (UAS)
}


async def handle_incoming_invite(raw):
    _, hdrs, body, first = _parse(raw)
    from_hdr = hdrs.get("from", "?")
    cid = _call_id(hdrs)
    via_block = _via_block(hdrs)
    to_hdr = hdrs.get("to", "")
    cseq = hdrs.get("cseq", "1 INVITE")

    if in_call and cid == call_state["call_id"] and _tag(to_hdr):
        # re-INVITE nel dialogo (refresh di sessione, cambio media): la stessa
        # chiamata, non uno squillo. Fino alla 1.0.9 riceveva il 200 del primo
        # INVITE (CSeq sbagliato) o, su una chiamata in uscita, 180 + 603: la
        # targa resta senza risposta al suo re-INVITE e chiude la chiamata.
        try:
            remote = _remote_media(body) if body else None
        except ValueError as e:  # SDP inaccettabile (media fuori LAN): la chiamata resta com'è
            _LOGGER.error("re-INVITE rifiutato: %s", e)
            await send(f"SIP/2.0 488 Not Acceptable Here\r\n{via_block}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
                       f"Call-ID: {cid}\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n")
            return
        sdp = call_state.get("local_sdp") or build_sdp(remote)
        await send(f"SIP/2.0 200 OK\r\n{via_block}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
                   f"Call-ID: {cid}\r\nCSeq: {cseq}\r\nContact: {_simple_contact()}\r\n"
                   f"Content-Type: application/sdp\r\n"
                   f"Content-Length: {_clen(sdp)}\r\n\r\n{sdp}")
        if remote and remote != call_state["remote_sdp"]:
            call_state["remote_sdp"] = remote
            await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)
        return

    if cid == pending_incoming["cid"]:
        if _via_branch(hdrs) != pending_incoming["branch"]:
            # Stesso Call-ID, altro branch: il relay cloud biforca lo squillo (due INVITE
            # a pochi ms, non una ritrasmissione: T1 = 500 ms) e poi annulla il ramo
            # doppio ~70 ms dopo il nostro 200 OK (m4r1k, 40517 cloud). Il secondo ramo
            # è una richiesta «merged» (RFC 3261 §8.2.2.2): 482 sul suo Via, e lo
            # squillo resta quello del primo.
            _LOGGER.info("INVITE biforcato (cid=%s): 482 al secondo ramo", cid[:24])
            await send(f"SIP/2.0 482 Loop Detected\r\n{via_block}To: {to_hdr};tag={_gen('')}\r\n"
                       f"From: {from_hdr}\r\nCall-ID: {cid}\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n")
            return
        # Ritrasmissione UDP dello stesso INVITE: stessa risposta, non un nuovo squillo.
        # Dopo la risposta riceve di nuovo il 200 OK (perso via UDP); dopo un rifiuto,
        # un CANCEL o lo scadere dello squillo di nuovo quella risposta finale: senza,
        # la targa ritrasmetteva fino a scadere e il PBX teneva lo squillo aperto.
        resp = pending_incoming["resp"] or ""
        if ringing() or (in_call and call_state.get("call_id") == cid) or resp.startswith(
                ("SIP/2.0 4", "SIP/2.0 5", "SIP/2.0 6")):
            await send(resp)
        return

    if ringing() or in_call or calling:
        # Occupato (un'altra targa mentre questa squilla, o durante una chiamata):
        # 486 e basta. Prima sovrascriveva lo squillo in corso (che restava senza
        # risposta finale) e, in chiamata, finiva in un 603: un 6xx fa annullare al
        # PBX la chiamata anche verso il Tab e gli altri telefoni (RFC 3261 §16.7).
        await send(f"SIP/2.0 486 Busy Here\r\n{via_block}To: {to_hdr};tag={_gen('')}\r\n"
                   f"From: {from_hdr}\r\nCall-ID: {cid}\r\nCSeq: {cseq}\r\n"
                   f"Content-Length: 0\r\n\r\n")
        return

    caller_tag = _tag(from_hdr)
    caller_uri = _angle_uri(from_hdr)

    _LOGGER.info("Incoming INVITE from %s", caller_uri)

    my_tag = _gen("")

    pending_incoming.update(
        active=True, cid=cid, branch=_via_branch(hdrs), from_hdr=from_hdr, to_hdr=to_hdr,
        cseq=cseq, via_block=via_block, my_tag=my_tag,
        caller_uri=caller_uri, caller_tag=caller_tag, body=body,
        sdp=None, early=False,
        # RFC 3261 §12.1.1 (UAS): remote target = Contact dell'INVITE, route set =
        # i suoi Record-Route nell'ordine in cui stanno.
        contact=_contact_uri(hdrs) or caller_uri, route_set=_route_set(hdrs),
    )

    head = (f"{via_block}To: {to_hdr};tag={my_tag}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
            f"Contact: {_simple_contact()}\r\n")
    if body and not (in_call or calling):
        # Early media: 183 con il nostro SDP → la targa manda il video già
        # durante lo squillo (l'"anteprima" dell'app VIEW), senza rispondere.
        # Il 200 OK riusa questo SDP e le sue chiavi SRTP. Mai durante una
        # nostra chiamata: setup_media sostituirebbe il suo media.
        sdp = build_sdp(parse_sdp(body))
        resp = (f"SIP/2.0 183 Session Progress\r\n{head}"
                f"Content-Type: application/sdp\r\n"
                f"Content-Length: {_clen(sdp)}\r\n\r\n{sdp}")
        pending_incoming.update(sdp=sdp, early=True, resp=resp)
        await send(resp)
        try:
            await media.setup_media(_remote_media(body), _local_crypto_key,
                                    _local_video_crypto_key, early=True)
        except Exception:  # SDP anomalo: niente anteprima, ma il campanello deve suonare
            _LOGGER.exception("Anteprima dello squillo non avviata")
            await media.stop_media()
            pending_incoming["early"] = False  # la risposta farà un setup_media normale
        if not ringing(cid) and not in_call:
            await media.stop_media()  # CANCEL arrivato mentre il video partiva
            return
    else:
        resp = f"SIP/2.0 180 Ringing\r\n{head}Content-Length: 0\r\n\r\n"
        pending_incoming["resp"] = resp
        await send(resp)

    # CANCEL perso (UDP, riavvio del PBX): l'anteprima non deve restare accesa per sempre.
    pending_incoming["timer"] = asyncio.get_running_loop().call_later(
        RING_MAX_S, lambda: asyncio.ensure_future(_ring_timeout(cid)))
    await broadcast("ring", f"Chiamata da: {caller_uri}")


def _remote_media(body: str) -> dict:
    """SDP remoto da usare per il media. In UDP locale solo indirizzi della LAN:
    lì un INVITE si falsifica, e il media (anche il nostro microfono) andrebbe
    dove dice l'SDP. Col cloud il media passa dai relay Vimar, pubblici."""
    remote = parse_sdp(body)
    ips = {remote.get("conn"), remote["audio"].get("ip"), remote["video"].get("ip")} - {"", None}
    if R.USE_LOCAL_UDP and not all(validate.is_private_host(ip) for ip in ips):
        raise ValueError(f"media verso indirizzi fuori LAN rifiutato: {sorted(ips)}")
    return remote


def _final(p: dict, status: str) -> str:
    """Risposta finale senza corpo all'INVITE dello squillo `p` (603, 480, 487, 488).
    Va anche in pending_incoming["resp"], per l'INVITE ritrasmesso (UDP)."""
    pending_incoming["resp"] = resp = (
        f"SIP/2.0 {status}\r\n"
        f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
        f"Call-ID: {p['cid']}\r\nCSeq: {p['cseq']}\r\n"
        f"Content-Length: 0\r\n\r\n")
    return resp


def _close_ring() -> None:
    """Squillo non più in corso: timer dei 90 s annullato, niente anteprima attesa."""
    timer = pending_incoming.pop("timer", None)
    if timer:
        timer.cancel()
    pending_incoming.update(active=False, early=False, sdp=None)


def ringing(cid=None) -> bool:
    """Squillo in corso (se cid è dato: proprio quello)."""
    p = pending_incoming
    return p["active"] and (cid is None or p["cid"] == cid)


def early_media() -> bool:
    """Anteprima video dello squillo in corso."""
    return pending_incoming["active"] and pending_incoming["early"]


async def _ring_timeout(cid) -> None:
    if ringing(cid) and not in_call:
        # Risposta finale anche qui: se il CANCEL si è solo perso, la targa smette.
        await do_decline_incoming("480 Temporarily Unavailable")
        await broadcast("ring_ended", "Squillo scaduto")


async def do_answer_incoming():
    if not ringing():
        return False, "Nessuna chiamata in arrivo"

    # Copia e chiusura dello squillo PRIMA del primo await: due risposte
    # ravvicinate (timer del messaggio + "Rispondi") non mandano due 200 OK.
    p = dict(pending_incoming)
    _close_ring()
    remote = None
    if p["body"]:
        try:
            remote = _remote_media(p["body"])
        except ValueError as e:
            # SDP inaccettabile (UDP locale, media fuori LAN): 488 e niente chiamata.
            # Prima il 200 OK partiva e la chiamata restava in piedi senza media.
            _LOGGER.error("Risposta rifiutata: %s", e)
            with contextlib.suppress(Exception):
                await send(_final(p, "488 Not Acceptable Here"))
            await _end_ring()
            await broadcast("ring_ended", "SDP rifiutato")
            return False, "SDP rifiutato"
    # con early media: stesso SDP/chiavi del 183
    sdp = p["sdp"] or build_sdp(parse_sdp(p["body"]) if p.get("body") else None)
    resp = (
        f"SIP/2.0 200 OK\r\n"
        f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
        f"Call-ID: {p['cid']}\r\nCSeq: {p['cseq']}\r\n"
        f"Contact: {_simple_contact()}\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {_clen(sdp)}\r\n\r\n{sdp}")
    pending_incoming["resp"] = resp  # per le ritrasmissioni dell'INVITE

    # Il 200 parte una volta e la chiamata non dipende dall'ACK: sul relay cloud
    # Vimar l'ACK del nostro 200 non arriva MAI, anche a chiamata perfetta (m4r1k,
    # 40517, 28/09). Un «niente ACK → BYE» (Timer H) chiuderebbe ogni chiamata
    # cloud dopo ~32 s; su UDP l'INVITE ritrasmesso riceve di nuovo `resp`.
    try:
        await send(resp)
    except Exception as e:  # connessione caduta: niente chiamata, niente anteprima orfana
        if p["early"]:
            await media.stop_media()
        return False, f"Risposta non inviata: {e}"

    _set_in_call(True)
    call_state["call_id"] = p["cid"]
    call_state["from_tag"] = p["my_tag"]
    call_state["to_tag"] = p["caller_tag"]
    call_state["remote_contact"] = p["contact"] or p["caller_uri"]
    call_state["route_set"] = p["route_set"]
    # Per il BYE (do_hangup) di una chiamata IN ARRIVO: il To: deve puntare al
    # CHIAMANTE, non alla targa di default (R.INTERCOM).
    call_state["original_target"] = p["caller_uri"]
    call_state["local_sdp"] = sdp
    call_state["remote_sdp"] = None  # senza offerta nell'INVITE arriva con l'ACK

    if remote:
        call_state["remote_sdp"] = remote
        _LOGGER.info("Answer SDP: audio=%s video=%s", remote.get('audio'), remote.get('video'))
        # Con early media il flusso è già aperto, salvo che qualcosa l'abbia chiuso.
        if not (p["early"] and media.video_proto and media.video_proto.remote_addr):
            await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)
        else:
            media.enable_tx()  # l'anteprima riceveva soltanto

    await broadcast("call_started", "Chiamata attiva!")
    # Request keyframe for video
    await send_keyframe_request()
    return True, "Risposto!"


async def do_decline_incoming(reason: str = "603 Decline"):
    if not ringing():
        return

    try:
        await send(_final(pending_incoming, reason))
    except Exception as e:  # noqa: BLE001
        # TLS caduto: lo squillo si chiude lo stesso. Rimasto "attivo" bloccava le
        # chiamate e faceva rispondere 486 a ogni squillo nuovo, per sempre.
        _LOGGER.warning("Rifiuto dello squillo non inviato (%s): lo chiudo in locale", e)
    await _end_ring()


async def _end_ring():
    """Squillo finito senza risposta nostra: chiude l'eventuale early media."""
    _close_ring()
    # Anche se un secondo INVITE ha sovrascritto lo squillo, l'anteprima del primo
    # va chiusa: fuori da una nostra chiamata non deve restare media acceso.
    if not (in_call or calling):
        await media.stop_media()


def _via_block(hdrs):
    via_all = hdrs.get("_via_all", [])
    if via_all:
        return "".join(f"Via: {v}\r\n" for v in via_all)
    return f"Via: {hdrs.get('via', '')}\r\n"


async def handle_incoming_bye(raw):
    _, hdrs, *_ = _parse(raw)
    cid = _call_id(hdrs)
    from_hdr = hdrs.get("from", "")
    to_hdr = hdrs.get("to", "")
    cseq = hdrs.get("cseq", "1 BYE")

    if cid != call_state["call_id"]:
        # BYE di un dialogo che non è la nostra chiamata: non chiudere quella attiva.
        await send(
            f"SIP/2.0 481 Call/Transaction Does Not Exist\r\n"
            f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
        return

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Content-Length: 0\r\n\r\n")

    _LOGGER.info("BYE dal citofono (call %s) Reason=%s", cid[:24], hdrs.get("reason", "-"))
    was_in_call = in_call
    _set_in_call(False)
    pending_responses.pop(cid, None)  # coda del dialogo (INFO di keyframe): non resta appesa
    _clear_call_state()
    if was_in_call:  # altrimenti BYE incrociati: do_hangup ha già chiuso media e chiamata
        await media.stop_media()
        await broadcast("call_ended", "Chiamata terminata")


async def handle_incoming_options(raw):
    _, hdrs, *_ = _parse(raw)
    from_hdr = hdrs.get("from", "")
    to_hdr = hdrs.get("to", "")
    cid = _call_id(hdrs)
    cseq = hdrs.get("cseq", "1 OPTIONS")
    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n"
        f"Content-Length: 0\r\n\r\n")


async def handle_incoming_cancel(raw):
    _, hdrs, *_ = _parse(raw)
    cid = _call_id(hdrs)
    from_hdr = hdrs.get("from", "")
    to_hdr = hdrs.get("to", "")
    cseq = hdrs.get("cseq", "1 CANCEL")

    _LOGGER.info("Incoming CANCEL for %s", cid[:24])

    # La transazione da annullare è (Call-ID, branch): il CANCEL del ramo doppio che
    # il relay cloud manda dopo il nostro 200 OK non è lo squillo (né la chiamata)
    # che finisce. Su Call-ID e basta chiudeva la chiamata vera appena risposta.
    known = cid == pending_incoming["cid"] and _via_branch(hdrs) == pending_incoming["branch"]
    await send(
        f"SIP/2.0 {'200 OK' if known else '481 Call/Transaction Does Not Exist'}\r\n"
        f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Content-Length: 0\r\n\r\n")

    if known and ringing(cid):
        await send(_final(pending_incoming, "487 Request Terminated"))
        await _end_ring()
        # Solo il CANCEL dello squillo in corso: un altro annullerebbe il suo timer.
        await broadcast("ring_ended", "Chiamata cancellata")


async def request_processor():
    def _fire(coro, name: str):
        """Lancia un task e loga le eccezioni non gestite (evita eccezioni silenziate)."""
        t = asyncio.create_task(coro)
        t.add_done_callback(
            lambda t, n=name: _LOGGER.error("%s handler error: %s", n, t.exception())
            if not t.cancelled() and t.exception() else None
        )

    while True:
        raw = await incoming_requests.get()
        kind, hdrs, body, first = _parse(raw)
        if kind == "INVITE":
            _fire(handle_incoming_invite(raw), "INVITE")
        elif kind == "CANCEL":
            _fire(handle_incoming_cancel(raw), "CANCEL")
        elif kind == "BYE":
            _fire(handle_incoming_bye(raw), "BYE")
        elif kind == "OPTIONS":
            _fire(handle_incoming_options(raw), "OPTIONS")
        elif kind == "MESSAGE":
            _LOGGER.debug("SIP MESSAGE body=%r from=%s", body, hdrs.get("from",""))
            # Cap generoso (era 200: troncava GET_INIT_STATUS_REPLY ~266B → perdeva dnd/voicemail).
            await broadcast("message", (body or "")[:4096])
            from_hdr = hdrs.get("from", "")
            to_hdr = hdrs.get("to", "")
            msg_cid = hdrs.get("call-id", "")
            msg_cseq = hdrs.get("cseq", "1 MESSAGE")
            await send(
                f"SIP/2.0 200 OK\r\n"
                f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
                f"Call-ID: {msg_cid}\r\nCSeq: {msg_cseq}\r\n"
                f"Content-Length: 0\r\n\r\n")
        elif kind in ("INFO", "UPDATE"):  # UPDATE: refresh di sessione, annunciato in Allow
            from_hdr = hdrs.get("from", "")
            to_hdr = hdrs.get("to", "")
            info_cid = hdrs.get("call-id", "")
            info_cseq = hdrs.get("cseq", "1 INFO")
            await send(
                f"SIP/2.0 200 OK\r\n"
                f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
                f"Call-ID: {info_cid}\r\nCSeq: {info_cseq}\r\n"
                f"Content-Length: 0\r\n\r\n")
        elif kind == "NOTIFY":
            # Il Tab può notificare cambi di stato (segreteria, DND, ...) via
            # NOTIFY. Catturiamo body + evento SIP e rispondiamo 200 OK.
            ev = hdrs.get("event", "")
            _LOGGER.info("SIP NOTIFY event=%s body=%s hdrs=%s",
                         ev, (body or "")[:300], first[:80])
            await broadcast("message", f"NOTIFY {ev}: {(body or '')[:200]}")
            from_hdr = hdrs.get("from", "")
            to_hdr = hdrs.get("to", "")
            n_cid = hdrs.get("call-id", "")
            n_cseq = hdrs.get("cseq", "1 NOTIFY")
            await send(
                f"SIP/2.0 200 OK\r\n"
                f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
                f"Call-ID: {n_cid}\r\nCSeq: {n_cseq}\r\n"
                f"Content-Length: 0\r\n\r\n")
        elif kind == "ACK":
            # Offerta ritardata (INVITE senza SDP): l'offerta era nel nostro 200 OK
            # e la risposta della targa arriva qui. Senza, chiamata muta e senza video.
            if (body.strip() and in_call and _call_id(hdrs) == call_state["call_id"]
                    and not call_state["remote_sdp"]):
                _fire(_media_from_ack(body, _call_id(hdrs)), "ACK")
        else:
            _LOGGER.debug("Unhandled SIP request: %s", kind)


async def _media_from_ack(body: str, cid: str) -> None:
    # Il task parte dopo: nel frattempo un BYE può aver chiuso la chiamata (o un'altra
    # averla sostituita), e il media non va riaperto verso chi non è più in linea.
    if not (in_call and call_state["call_id"] == cid and not call_state["remote_sdp"]):
        return
    call_state["remote_sdp"] = remote = _remote_media(body)
    await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)
    await send_keyframe_request()
