"""Config flow per Vimar Intercom — onboarding via QR o credenziali manuali."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import socket
import ssl
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    AWAY_TEXT_MAX,
    CA_PATH,
    MY_NAME,
    SIP_PORT as CLOUD_SIP_PORT,
    USER_AGENT,
    CAMERA_TARGET,
    DEFAULT_SNAPSHOT_DELAY,
    DOMAIN,
    INTERNAL_PANEL_TARGET,
    PICG_TARGET,
    SGA_TARGET,
    CONF_HOMEKIT_ACCESSORY,
    CONF_HOMEKIT_ANSWER,
    CONF_HOMEKIT_RING_BUTTON,
    CONF_HOMEKIT_SMOOTH,
    DEFAULT_HOMEKIT_ACCESSORY,
    DEFAULT_HOMEKIT_ANSWER,
    DEFAULT_HOMEKIT_RING_BUTTON,
    DEFAULT_HOMEKIT_SMOOTH,
    HOMEKIT_ANSWER_OPEN,
    HOMEKIT_ANSWER_TALK,
    HOMEKIT_DATA,
    HOMEKIT_QR_URL,
)
from . import cloud_phonebook
from . import discovery
from . import profiles
from . import qr_decoder
from . import rest_client
from . import rubrica_import
from . import runtime
from .sip_client import _challenge_params, _resolve_sip_targets
from . import validate
from .runtime import (
    MEDIA_ENC_MODES, VOICE_ANSWER_MODES, media_enc_mode, view_keepalive_default, voice_answer_mode,
)

_LOGGER = logging.getLogger(__name__)

# ─── Chiavi config entry ─────────────────────────────────────────────────────
KEY_SIP_USER      = "sip_user"
KEY_SIP_PASSWORD  = "sip_password"
KEY_SIP_DOMAIN    = "sip_domain"
KEY_SIP_HA1       = "sip_ha1"
KEY_CLOUD_PROXY   = "cloud_proxy"
KEY_LOCAL_PROXY   = "local_proxy"
KEY_GID           = "gid"
KEY_PLANT_TYPE    = "plant_type"
KEY_MAC           = "mac"
KEY_LOCAL_DOMAIN  = "local_domain"
KEY_CLOUD_DOMAIN  = "cloud_domain"
KEY_USE_LOCAL_UDP  = "use_local_udp"
KEY_LOCAL_UDP_PORT = "local_udp_port"
KEY_ACTUATORS      = "actuators"
KEY_MEDIA_ENC      = "media_enc"
KEY_VOICE_ANSWER   = "voice_answer"
KEY_SGA_TARGET     = "sga_target"
# Why the entry uses another transport than the one asked for (setup fallback).
KEY_SETUP_NOTE     = "setup_note"
KEY_PICG_TARGET    = "picg_target"
KEY_CAMERA_TARGET  = "camera_target"
KEY_INTERNAL_PANEL_TARGET = "internal_panel_target"
KEY_DOOR_TARGET    = "door_target"
KEY_AWAY_FILE      = "away_message_file"
KEY_AWAY_TEXT      = "away_message_text"
KEY_AWAY_TTS       = "away_message_tts"
KEY_AWAY_DELAY     = "away_message_delay"
KEY_SNAP_DIR       = "snapshot_dir"
KEY_SNAP_DELAY     = "snapshot_delay"
KEY_VIEW_KA        = "view_keepalive"
KEY_ALLOWED_USERS  = "allowed_users"
KEY_RING_WEBHOOK_URL     = "ring_webhook_url"
KEY_RING_END_WEBHOOK_URL = "ring_end_webhook_url"

DEFAULT_CLOUD_PROXY    = "ipvdes.vimar.cloud"
DEFAULT_LOCAL_SIP_PORT = 5060
DEFAULT_LOCAL_UDP_PORT = 5060

# Icone ammesse per gli attuatori dinamici (vedi tools/parse_rubrica.py)
ALLOWED_ACTUATOR_ICONS = ("door", "light", "switch")


class InvalidActuatorTarget(ValueError):
    """Target di un attuatore non numerico (né AUTO): errore «invalid_actuator_target»."""


def _parse_actuators(raw: str) -> list[dict]:
    """Valida la lista attuatori incollata come JSON nell'options flow.

    Solleva ValueError con un messaggio parlante se il JSON non è una lista di
    dict con le chiavi ``name``, ``msg``, ``target``, ``icon`` (icon nel set
    ammesso, target numerico o AUTO). Stringa vuota → lista vuota (nessun bottone).
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"JSON non valido: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("La radice deve essere una lista JSON")
    result: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Elemento #{i} non è un oggetto JSON")
        missing = [k for k in ("name", "msg", "target", "icon") if k not in item]
        if missing:
            raise ValueError(f"Elemento #{i}: chiavi mancanti {missing}")
        icon = item["icon"]
        if icon not in ALLOWED_ACTUATOR_ICONS:
            raise ValueError(
                f"Elemento #{i}: icon '{icon}' non valida "
                f"(ammesse: {', '.join(ALLOWED_ACTUATOR_ICONS)})"
            )
        # Stessa regola di hub.sip_uri: il target finisce nella request line del
        # MESSAGE. "AUTO" = la targa dell'apri-porta (button.py).
        target = str(item["target"]).strip()
        if target.upper() != "AUTO" and not validate.sip_target(target):
            raise InvalidActuatorTarget(f"elemento #{i}, target '{target}'")
        result.append({
            "name":   str(item["name"]),
            "msg":    str(item["msg"]),
            "target": target,
            "icon":   str(icon),
        })
    return result


# ─── Validazione ─────────────────────────────────────────────────────────────

def _valid_device_name(name: str) -> bool:
    """The device name goes into the MyName header: 1 to 64 printable
    characters, no control characters (a line break would split the header)."""
    return 1 <= len(name) <= 64 and name.isprintable()


def _validate_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


async def _test_sip_registration(
    sip_user: str,
    sip_password: str,
    sip_domain: str,
    local_proxy: str,
    local_udp_port: int = DEFAULT_LOCAL_UDP_PORT,
    device_imei: str = "",
    device_uuid: str = "",
    device_name: str = MY_NAME,
    timeout: float = 8.0,
    unregister: bool = False,
) -> tuple[bool, str]:
    """Register over local UDP; returns (success, message).

    The REGISTER carries the same identity the integration will use (MyName,
    Mobile-IMEI, +sip.instance): the intercom binds a pairing to that identity
    and refuses a different one with 503, so a test without it proves nothing.

    unregister: once the test has passed, remove its binding (Expires: 0, same
    Contact). With the integration running, the test registration takes over
    the running one's binding (same +sip.instance) and points it at this
    socket, closed right after: rings would go nowhere until the next renewal.
    """

    def _run() -> tuple[bool, str]:
        call_id  = secrets.token_hex(8)
        from_tag = secrets.token_hex(4)
        uri      = f"sip:{sip_domain}"
        target   = (local_proxy, DEFAULT_LOCAL_SIP_PORT)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            # The configured port, as the integration will use it; when it is
            # taken (the integration itself is running) any port will do.
            try:
                sock.bind(("0.0.0.0", local_udp_port))
            except OSError:
                sock.bind(("0.0.0.0", 0))
            sock.connect(target)
            my_ip, my_port = sock.getsockname()[:2]

            def make_register(auth_hdr: str | None = None, seq: int = 1,
                              expires: int = 60) -> bytes:
                contact = f"<sip:{sip_user}@{my_ip}:{my_port}>"
                if device_uuid:
                    contact += f';+sip.instance="<urn:uuid:{device_uuid}>"'
                lines = [
                    f"REGISTER {uri} SIP/2.0",
                    f"Via: SIP/2.0/UDP {my_ip}:{my_port};branch=z9hG4bK{secrets.token_hex(4)};rport",
                    "Max-Forwards: 70",
                    f"To: <sip:{sip_user}@{sip_domain}>",
                    f"From: <sip:{sip_user}@{sip_domain}>;tag={from_tag}",
                    f"Call-ID: {call_id}",
                    f"CSeq: {seq} REGISTER",
                    f"Contact: {contact}",
                    f"Expires: {expires}",
                    f"User-Agent: {USER_AGENT}",
                    f"MyName: {device_name}",
                ]
                if device_imei:
                    lines.append(f"Mobile-IMEI: {device_imei}")
                if auth_hdr:
                    lines.append(f"Authorization: {auth_hdr}")
                lines += ["Content-Length: 0", "", ""]
                return "\r\n".join(lines).encode()

            def drop_binding(auth_hdr: str | None, seq: int) -> None:
                """Best effort: the test result stands whatever the answer."""
                if not unregister:
                    return
                try:
                    sock.settimeout(min(timeout, 2.0))
                    sock.send(make_register(auth_hdr=auth_hdr, seq=seq, expires=0))
                    read_final()
                except OSError as exc:  # socket.timeout included
                    _LOGGER.debug("SIP test: unregister not confirmed (%s)", exc)

            def read_final() -> str:
                """Skip provisional responses (100 Trying), return the final one."""
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    message = sock.recv(65535).decode(errors="replace")
                    parts = message.split("\r\n", 1)[0].split(" ")
                    if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) >= 200:
                        return message
                raise socket.timeout

            def refused(first: str) -> str:
                if " 503" in first:
                    return (f"{first}: the intercom refuses this identity. It is "
                            "paired to another device name or identifier, or this "
                            "plant does not accept local UDP registrations.")
                return f"Unexpected response: {first}"

            sock.send(make_register(seq=1))
            response = read_final()
            first = response.split("\r\n", 1)[0]
            if " 200" in first:
                drop_binding(None, 2)
                return True, "Registration succeeded (no auth)"
            if " 401" not in first and " 407" not in first:
                return False, refused(first)

            challenge = _parse_challenge(response)
            if not challenge.get("nonce") or not challenge.get("realm"):
                return False, "Challenge without nonce/realm"
            auth = _digest_header(
                user=sip_user, password=sip_password,
                realm=challenge["realm"], nonce=challenge["nonce"], uri=uri,
                qop=challenge.get("qop", ""), opaque=challenge.get("opaque", ""),
                cnonce=secrets.token_hex(4),
            )
            sock.send(make_register(auth_hdr=auth, seq=2))
            first2 = read_final().split("\r\n", 1)[0]
            if " 200" in first2:
                drop_binding(auth, 3)
                return True, "Registration succeeded"
            return False, refused(first2) if " 503" in first2 else f"Authentication refused: {first2}"

        except socket.timeout:
            return False, (
                f"Timeout ({timeout:.0f}s): the intercom does not answer on "
                f"{local_proxy}:{DEFAULT_LOCAL_SIP_PORT}. Check that Home "
                "Assistant and the intercom are on the same network."
            )
        except OSError as exc:
            return False, f"Socket error: {exc}"
        finally:
            sock.close()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


def _parse_challenge(response: str) -> dict[str, str]:
    """The Digest challenge parameters of a 401/407 response."""
    for line in response.split("\r\n"):
        name, sep, value = line.partition(":")
        if not sep or name.strip().lower() not in ("www-authenticate", "proxy-authenticate"):
            continue
        scheme, sep, _params = value.strip().partition(" ")
        if not sep or scheme.lower() != "digest":
            continue
        return _challenge_params(value)
    return {}


def _digest_header(
    *, user: str, password: str, realm: str, nonce: str, uri: str,
    method: str = "REGISTER", qop: str = "", opaque: str = "", cnonce: str = "",
) -> str:
    """Authorization header for an MD5 Digest challenge."""
    nc = "00000001"
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    offered = [item.strip().lower() for item in qop.split(",")] if qop else []
    if "auth" in offered:
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        header = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                  f'uri="{uri}", response="{response}", algorithm=MD5, '
                  f'qop=auth, nc={nc}, cnonce="{cnonce}"')
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        header = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                  f'uri="{uri}", response="{response}", algorithm=MD5')
    if opaque:
        header += f', opaque="{opaque}"'
    return header


async def _test_cloud_registration(
    sip_user: str,
    sip_password: str,
    cloud_domain: str,
    cloud_proxy: str = DEFAULT_CLOUD_PROXY,
    device_imei: str = "",
    device_uuid: str = "",
    device_name: str = MY_NAME,
    timeout: float = 12.0,
) -> tuple[bool, str]:
    """Register on the cloud relay over TLS; returns (success, message)."""
    if not cloud_domain:
        return False, ("The credentials carry no cloud domain: set the "
                       "integration up again from the pairing QR.")

    def _run() -> tuple[bool, str]:
        call_id = secrets.token_hex(8)
        from_tag = secrets.token_hex(4)
        uri = f"sip:{cloud_domain}"
        context = ssl.create_default_context()
        if os.path.exists(CA_PATH):
            context.load_verify_locations(CA_PATH)

        last_error = "no proxy reachable"
        for host, port in _resolve_sip_targets(cloud_proxy, CLOUD_SIP_PORT):
            try:
                raw = socket.create_connection((host, port), timeout=timeout)
            except OSError as exc:
                last_error = f"{host}:{port}: {exc}"
                continue
            try:
                with context.wrap_socket(raw, server_hostname=cloud_proxy) as sock:
                    sock.settimeout(timeout)
                    my_ip, my_port = sock.getsockname()[:2]

                    def make_register(auth_hdr=None, seq=1):
                        contact = f"<sip:{sip_user}@{my_ip}:{my_port};transport=tls>"
                        if device_uuid:
                            contact += f';+sip.instance="<urn:uuid:{device_uuid}>"'
                        lines = [
                            f"REGISTER {uri} SIP/2.0",
                            f"Via: SIP/2.0/TLS {my_ip}:{my_port};alias;"
                            f"branch=z9hG4bK{secrets.token_hex(4)};rport",
                            f"Route: <sip:{cloud_proxy};transport=tls;lr>",
                            "Max-Forwards: 70",
                            f"To: <sip:{sip_user}@{cloud_domain}>",
                            f"From: <sip:{sip_user}@{cloud_domain}>;tag={from_tag}",
                            f"Call-ID: {call_id}",
                            f"CSeq: {seq} REGISTER",
                            f"Contact: {contact}",
                            "Expires: 60",
                            f"User-Agent: {USER_AGENT}",
                            f"MyName: {device_name}",
                        ]
                        if device_imei:
                            lines.append(f"Mobile-IMEI: {device_imei}")
                        if auth_hdr:
                            lines.append(f"Authorization: {auth_hdr}")
                        lines += ["Content-Length: 0", "", ""]
                        return "\r\n".join(lines).encode()

                    def read_final() -> str:
                        """Skip provisional responses, return the final one."""
                        buffer = b""
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            chunk = sock.recv(65535)
                            if not chunk:
                                raise OSError("connection closed by the relay")
                            buffer += chunk
                            while b"\r\n\r\n" in buffer:
                                head, _, buffer = buffer.partition(b"\r\n\r\n")
                                message = head.decode(errors="replace")
                                parts = message.split("\r\n", 1)[0].split(" ")
                                if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) >= 200:
                                    return message
                        raise socket.timeout

                    sock.sendall(make_register())
                    response = read_final()
                    first = response.split("\r\n", 1)[0]
                    if " 200" in first:
                        return True, f"Cloud registration succeeded via {host}"
                    if " 401" not in first and " 407" not in first:
                        return False, f"The relay refused the registration: {first}"
                    challenge = _parse_challenge(response)
                    if not challenge.get("nonce") or not challenge.get("realm"):
                        return False, "Cloud challenge without nonce/realm"
                    auth = _digest_header(
                        user=sip_user, password=sip_password,
                        realm=challenge["realm"], nonce=challenge["nonce"], uri=uri,
                        qop=challenge.get("qop", ""), opaque=challenge.get("opaque", ""),
                        cnonce=secrets.token_hex(4),
                    )
                    sock.sendall(make_register(auth_hdr=auth, seq=2))
                    final = read_final().split("\r\n", 1)[0]
                    if " 200" in final:
                        return True, f"Cloud registration succeeded via {host}"
                    return False, f"Cloud authentication refused: {final}"
            except (OSError, ssl.SSLError) as exc:
                last_error = f"{host}:{port}: {exc or type(exc).__name__}"
                continue
        return False, f"Cloud registration failed ({last_error})"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


async def _probe_transport(
    credentials: dict, *, local_proxy: str, local_udp_port: int,
    identity: dict, prefer_local: bool, local_domain: str = "",
) -> tuple[bool, bool, str]:
    """Find out which transport really works: (use_local_udp, ok, message).

    Start from what the plant profile suggests, and when that fails try the
    other path before giving up: the plant type in the QR is an indication,
    not a guarantee, and a new firmware can contradict it. Whatever passes is
    what gets stored.
    """

    async def _local() -> tuple[bool, str]:
        return await _test_sip_registration(
            sip_user=credentials["sip_user"],
            sip_password=credentials["sip_password"],
            # The domain runtime.configure uses in local UDP mode.
            sip_domain=local_domain or credentials["sip_domain"],
            local_proxy=local_proxy,
            local_udp_port=local_udp_port,
            device_imei=identity.get("device_imei", ""),
            device_uuid=identity.get("device_uuid", ""),
            device_name=identity.get("device_name") or MY_NAME,
        )

    async def _cloud() -> tuple[bool, str]:
        return await _test_cloud_registration(
            sip_user=credentials["sip_user"],
            sip_password=credentials["sip_password"],
            cloud_domain=credentials.get("cloud_domain", ""),
            cloud_proxy=credentials.get("cloud_proxy") or DEFAULT_CLOUD_PROXY,
            device_imei=identity.get("device_imei", ""),
            device_uuid=identity.get("device_uuid", ""),
            device_name=identity.get("device_name") or MY_NAME,
        )

    tries = [(True, "local UDP", _local), (False, "cloud TLS", _cloud)]
    if not prefer_local:
        tries.reverse()
    (first_local, first_name, first), (_, second_name, second) = tries

    ok, msg = await first()
    _LOGGER.info("SIP test (%s): ok=%s msg=%s", first_name, ok, msg)
    if ok:
        return first_local, True, msg
    ok2, msg2 = await second()
    _LOGGER.info("SIP test (%s, fallback): ok=%s msg=%s", second_name, ok2, msg2)
    if ok2:
        return (not first_local), True, (
            f"{first_name} not available ({msg}); registered via {second_name}")
    return first_local, False, f"{first_name}: {msg}. {second_name}: {msg2}"


# ─── Config Flow ─────────────────────────────────────────────────────────────


def _inside(path: str, folder: str) -> bool:
    """True se path è folder o sta sotto (link risolti; /config/www2 non conta)."""
    return Path(path).resolve().is_relative_to(Path(folder).resolve())

def _default(value: str | None) -> dict:
    """`{"default": value}` solo se c'è un valore: un default "" precompila il campo
    con una stringa vuota invece di lasciarlo vuoto."""
    return {"default": value} if value else {}


class VimarIntercomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce l'onboarding dell'integrazione Vimar Intercom."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict = {}
        self._identity: dict = {}
        self._qr_error: str | None = None
        # Dati del record mDNS quando il flusso parte dal discovery (issue #6).
        # Vuoto se l'utente ha avviato il flusso a mano.
        self._discovered: dict[str, str] = {}
        # (entry, new address) when a configured Tab announced another address.
        self._moved: tuple | None = None

    # ─── Discovery mDNS (_eipvdes._tcp) ─────────────────────────────────────
    # Nessun import di ZeroconfServiceInfo: sta in helpers.service_info.zeroconf
    # solo da HA 2024.12, e il minimo è 2024.7 (review della vecchia PR #7).
    # Servono solo .host e .properties.

    async def async_step_zeroconf(self, discovery_info) -> FlowResult:
        """Il Tab si è annunciato in LAN. Il record porta indirizzo, MAC e il dominio
        SIP che il Tab si aspetta; le credenziali no: il flusso prosegue come sempre
        (QR o manuale) con questi campi già compilati."""
        info = discovery.extract_discovery(
            str(discovery_info.host), getattr(discovery_info, "properties", None))
        _LOGGER.debug("Zeroconf: host=%s proxy=%s domain=%s model=%s fw=%s",
                      discovery_info.host, info["local_proxy"], info["sip_domain"],
                      info["model"], info["firmware"])
        mac = info["mac_normalized"]
        if not mac:
            # Senza MAC non si riconosce lo stesso Tab al discovery successivo:
            # si aprirebbe un flusso nuovo a ogni riavvio.
            return self.async_abort(reason="no_mac")

        # Già configurato? Le installazioni hanno unique_id «utente@dominio»: si
        # riconoscono dal MAC salvato (dal QR, con separatori diversi) o dall'IP.
        for entry in self._async_current_entries(include_ignore=False):
            conf = {**entry.data, **entry.options}
            same_mac = discovery.normalize_mac(conf.get(KEY_MAC)) == mac
            if same_mac or conf.get(KEY_LOCAL_PROXY) == info["local_proxy"]:
                moved = self._moved_proxy(conf, info["local_proxy"], str(discovery_info.host))
                if same_mac and moved:
                    # One flow per Tab: a second announcement joins this one.
                    await self.async_set_unique_id(mac)
                    self._abort_if_unique_id_configured()   # the user's "Ignore" holds
                    self._moved = (entry, moved)
                    self.context["title_placeholders"] = {"name": "Vimar Intercom", "host": moved}
                    return await self.async_step_zeroconf_moved()
                return self.async_abort(reason="already_configured")
        # Un altro Tab con un'installazione già presente: una sola entry.
        if self._has_entry():
            return self.async_abort(reason="single_instance_allowed")

        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()   # un «ignora» dell'utente vale
        self._discovered = info
        self.context["title_placeholders"] = {
            "name": f"Vimar {info['model']}" if info["model"] else "Vimar Intercom",
            "host": info["local_proxy"],
        }
        return await self.async_step_zeroconf_confirm()

    def _has_entry(self) -> bool:
        """Una sola installazione: lo stato SIP e media è a livello di modulo, quindi
        una seconda entry condividerebbe registrazione e chiamata con la prima.

        Il limite sta qui e non in `single_config_entry` del manifest: con quel flag
        Home Assistant ferma ogni flusso nuovo prima di chiamarci, compreso il
        discovery mDNS, e il Tab che cambia IP in UDP locale non veniva più seguito
        (`_update_proxy`, issue #6)."""
        return bool(self._async_current_entries(include_ignore=False))

    @staticmethod
    def _moved_proxy(conf: dict, proxy: str, host: str) -> str | None:
        """The new address of a configured Tab, if this announcement may propose one.

        Nothing in an mDNS record is authenticated: the MAC it is matched by is
        public (the Tab announces it), and the host is the address record the
        announcer wrote, not the packet's source. The checks below only keep the
        record consistent with itself (the `proxy` it carries is its own address)
        and the address a private IPv4 one, like a Tab's; what protects the entry is
        that the address is applied only after the user confirms it (#46). An
        authenticated probe of the new address would have kept this automatic, but
        HTTP Digest and SIP both hand the unconfirmed host a response it can attack
        offline for the SIP password. Only in local mode, where the address is used.
        """
        if not conf.get(KEY_USE_LOCAL_UDP, True) or not proxy or proxy == conf.get(KEY_LOCAL_PROXY):
            return None
        try:
            ip = ipaddress.ip_address(proxy)
        except ValueError:
            ip = None
        if proxy != host or ip is None or ip.version != 4 or not ip.is_private or ip.is_link_local:
            _LOGGER.warning("Ignored an mDNS announcement for the configured intercom: address %s, "
                            "host %s. Only a private IPv4 address, matching the record's host, "
                            "is offered.", proxy, host)
            return None
        return proxy

    async def async_step_zeroconf_moved(self, user_input: dict | None = None) -> FlowResult:
        """The configured Tab announced itself at another address: apply it on confirmation."""
        entry, proxy = self._moved
        if user_input is not None:
            self._update_proxy(entry, proxy)
            return self.async_abort(reason="address_updated")
        conf = {**entry.data, **entry.options}
        return self.async_show_form(
            step_id="zeroconf_moved",
            data_schema=vol.Schema({}),
            description_placeholders={"old": conf.get(KEY_LOCAL_PROXY) or "n/d", "new": proxy},
        )

    @callback
    def _update_proxy(self, entry, proxy: str) -> None:
        """Il DHCP ha dato un altro IP al Tab: si aggiorna l'entry e si ricarica.
        Solo in modalità locale, dove l'IP serve davvero."""
        conf = {**entry.data, **entry.options}
        if not proxy or conf.get(KEY_LOCAL_PROXY) == proxy or not conf.get(KEY_USE_LOCAL_UDP, True):
            return
        _LOGGER.info("Il citofono ha cambiato indirizzo: %s → %s", conf.get(KEY_LOCAL_PROXY), proxy)
        kwargs = {"data": {**entry.data, KEY_LOCAL_PROXY: proxy}}
        if KEY_LOCAL_PROXY in entry.options:
            kwargs["options"] = {**entry.options, KEY_LOCAL_PROXY: proxy}
        self.hass.config_entries.async_update_entry(entry, **kwargs)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)

    async def async_step_zeroconf_confirm(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Conferma del Tab trovato, poi si prosegue con QR o credenziali."""
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "host":     self._discovered.get("local_proxy", ""),
                "mac":      self._discovered.get("mac", ""),
                "model":    self._discovered.get("model") or "n/d",
                "firmware": self._discovered.get("firmware") or "n/d",
            },
        )

    def _apply_discovered(self) -> None:
        """Dopo QR o credenziali: il dominio locale e il MAC annunciati dal Tab.

        Il `domain` del record è quello che il Tab si aspetta in modalità locale
        (il proprio IP sul 40515, il dominio cloud sul 40507). Il QR del 40515 porta
        `domain=127.0.0.1` e la registrazione locale col dominio cloud riceve 503.
        """
        d = self._discovered
        if not d:
            return
        if d.get("sip_domain"):
            self._credentials[KEY_LOCAL_DOMAIN] = d["sip_domain"]
        if d.get("mac") and not self._credentials.get(KEY_MAC):
            self._credentials[KEY_MAC] = d["mac"]

    def _local_test_domain(self) -> str:
        """Il dominio che runtime.configure userà in UDP locale (LOCAL_DOMAIN se c'è)."""
        return self._credentials.get(KEY_LOCAL_DOMAIN) or self._credentials["sip_domain"]

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step iniziale: scelta modalità (QR o manuale)."""
        if self._has_entry():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            if user_input.get("mode") == "qr":
                return await self.async_step_qr()
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("mode", default="qr"): vol.In({
                    "qr":     "Scansiona il QR di abbinamento (consigliato)",
                    "manual": "Inserimento manuale credenziali SIP",
                }),
            }),
        )

    async def async_step_qr(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step 2a: incolla il testo del QR di abbinamento."""
        errors: dict[str, str] = {}

        if user_input is not None:
            qr_text = user_input.get("qr_text", "").strip()
            try:
                fields = await self.hass.async_add_executor_job(
                    qr_decoder.decode, qr_text
                )
                self._credentials = qr_decoder.extract_sip_credentials(fields)
                self._apply_discovered()
                return await self.async_step_network()
            except qr_decoder.QRDecodeError as exc:
                _LOGGER.warning("QR decode error: %s", exc)
                errors["qr_text"] = "qr_invalid"
                self._qr_error = str(exc)

        return self.async_show_form(
            step_id="qr",
            data_schema=vol.Schema({
                vol.Required("qr_text"): str,
            }),
            errors=errors,
            description_placeholders={
                "error_detail": self._qr_error or "",
            },
        )

    async def async_step_manual(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step 2b: inserimento manuale delle credenziali SIP."""
        errors: dict[str, str] = {}

        if user_input is not None:
            sip_user     = user_input.get("sip_user", "").strip()
            sip_password = user_input.get("sip_password", "").strip()
            sip_domain   = user_input.get("sip_domain", "").strip()
            cloud_proxy  = user_input.get("cloud_proxy", DEFAULT_CLOUD_PROXY).strip()

            if not sip_user:
                errors["sip_user"] = "required"
            if not sip_password:
                errors["sip_password"] = "required"
            if not sip_domain:
                errors["sip_domain"] = "required"

            if not errors:
                sip_ha1 = hashlib.md5(
                    f"{sip_user}:{sip_domain}:{sip_password}".encode()
                ).hexdigest()
                self._credentials = {
                    KEY_SIP_USER:     sip_user,
                    KEY_SIP_PASSWORD: sip_password,
                    KEY_SIP_DOMAIN:   sip_domain,
                    KEY_SIP_HA1:      sip_ha1,
                    KEY_CLOUD_PROXY:  cloud_proxy,
                    KEY_GID: "", KEY_PLANT_TYPE: "", KEY_MAC: "",
                }
                self._apply_discovered()
                return await self.async_step_network()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required("sip_user"):     str,
                vol.Required("sip_password"): str,
                # Il dominio annunciato dal Tab, se è un nome (il dominio cloud del
                # 40507); un IP (40515) è solo il dominio locale, non quello dell'account.
                vol.Required("sip_domain", **_default(
                    "" if discovery.is_ip(self._discovered.get("sip_domain"))
                    else self._discovered.get("sip_domain"))): str,
                vol.Optional("cloud_proxy", default=DEFAULT_CLOUD_PROXY): str,
            }),
            errors=errors,
        )

    async def async_step_network(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step 3: intercom IP and a real registration on the working transport."""
        errors: dict[str, str] = {}
        profile = profiles.profile_for(self._credentials.get(KEY_PLANT_TYPE))

        if user_input is not None:
            # Drop a duplicate BEFORE probing: the probe really registers, and
            # on a credential already in use it would disturb the entry using it.
            await self.async_set_unique_id(
                f"{self._credentials['sip_user']}@{self._credentials['sip_domain']}")
            self._abort_if_unique_id_configured()

            fallback: str | None = None
            local_proxy    = user_input.get("local_proxy", "").strip()
            use_local_udp  = user_input.get("use_local_udp", profile.prefers_local_udp)
            local_udp_port = int(user_input.get("local_udp_port", DEFAULT_LOCAL_UDP_PORT))
            device_name    = str(user_input.get("device_name") or MY_NAME).strip()

            if not device_name:
                errors["device_name"] = "required"
            elif not _valid_device_name(device_name):
                errors["device_name"] = "invalid_device_name"
            if not local_proxy:
                errors["local_proxy"] = "required"
            elif not _validate_ip(local_proxy):
                errors["local_proxy"] = "invalid_ip"
            elif not errors:
                # The probe registers with the identity the integration will
                # use from then on, so it is created here and stored. On a new
                # credential that registration is what fixes the pairing,
                # name included.
                if not self._identity:
                    self._identity = runtime.new_device_identity()
                self._identity["device_name"] = device_name
                use_local_wanted = use_local_udp
                use_local_udp, ok, msg = await _probe_transport(
                    self._credentials,
                    local_proxy=local_proxy,
                    local_udp_port=local_udp_port,
                    identity=self._identity,
                    prefer_local=use_local_udp,
                    local_domain=self._local_test_domain(),
                )
                self._qr_error = msg
                if not ok:
                    errors["local_proxy"] = "sip_registration_failed"
                elif use_local_udp != use_local_wanted:
                    # The path asked for did not work and the other one did:
                    # the entry works, but the user must know it is not the
                    # transport they chose.
                    fallback = msg

            if not errors:
                data = {
                    **self._credentials,
                    **self._identity,
                    KEY_LOCAL_PROXY:    local_proxy,
                    KEY_USE_LOCAL_UDP:  use_local_udp,
                    KEY_LOCAL_UDP_PORT: local_udp_port,
                }
                extra = {}
                if fallback:
                    _LOGGER.warning("Setup: %s", fallback)
                    data[KEY_SETUP_NOTE] = fallback
                    names = {True: "local UDP", False: "cloud TLS"}
                    extra = {"description": "transport_fallback",
                             "description_placeholders": {
                                 "tried": names[use_local_wanted],
                                 "used": names[use_local_udp],
                                 "detail": fallback}}
                return self.async_create_entry(
                    title=f"Vimar Intercom ({local_proxy})",
                    data=data,
                    **extra,
                )

        return self.async_show_form(
            step_id="network",
            data_schema=vol.Schema({
                vol.Required("local_proxy", **_default(self._discovered.get("local_proxy"))): str,
                vol.Optional("use_local_udp", default=profile.prefers_local_udp): bool,
                vol.Optional(
                    "local_udp_port", default=DEFAULT_LOCAL_UDP_PORT
                ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
                vol.Optional("device_name", default=MY_NAME): str,
            }),
            errors=errors,
            description_placeholders={
                "sip_user":   self._credentials.get("sip_user", ""),
                "sip_domain": self._credentials.get("sip_domain", ""),
                "mac":        self._credentials.get("mac", ""),
                "plant":      profiles.describe(
                    self._credentials.get(KEY_PLANT_TYPE),
                    self._credentials.get("product_code"),
                ),
                "error_detail": self._qr_error or "",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        return OptionsFlowHandler(config_entry)


def _homekit_pairing_text(hass, entry_id) -> str:
    """The setup code and QR of the HomeKit doorbell while it waits to be
    paired, for the HomeKit options page.

    Only here: the options are for administrators, and pairing gives live
    video, Talk and the gate, which ``allowed_users`` keeps from other users.
    The QR is an image behind an administrators-only view; the signed path
    authenticates the browser as whoever opened this page."""
    try:
        info = hass.data.get(HOMEKIT_DATA, {}).get("pairing", {}).get(entry_id)
    except AttributeError:
        return ""
    if not isinstance(info, dict) or not info.get("pin"):
        return ""
    italian = str(getattr(hass.config, "language", "") or "").startswith("it")
    if italian:
        text = ("Nell'app Casa: **Aggiungi accessorio**, poi inquadra il QR oppure scegli "
                "*Altre opzioni* e inserisci il codice:")
    else:
        text = ("In the Home app: **Add Accessory**, then scan the QR code or choose "
                "*More options* and enter the code:")
    text += f"\n\n## {info['pin']}"
    if info.get("svg") and info.get("token"):
        try:
            from homeassistant.components.http.auth import async_sign_path  # noqa: PLC0415

            path = async_sign_path(hass, f"{HOMEKIT_QR_URL}?t={info['token']}",
                                   timedelta(minutes=10))
            text += f"\n\n![QR]({path})"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HomeKit: pairing QR link not signed")
    return text


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Modifica le impostazioni di rete senza re-inserire le credenziali."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._actuators_error: str | None = None
        self._rubrica_error: str | None = None
        self._imported: dict | None = None
        self._imported_gid: str = "101"
        # PICG dichiarato dal citofono stesso (get_info.php?action=nickname).
        # Resta None quando la rubrica arriva da un file caricato a mano.
        self._picg_from_rest: str | None = None

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Menu: impostazioni a mano, rubrica dal citofono, o file rubrica.db."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "homekit", "fetch_rubrica", "fetch_rubrica_cloud", "import_rubrica"],
        )

    async def async_step_settings(
        self, user_input: dict | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        # Le options già salvate hanno precedenza sui dati iniziali dell'entry.
        current = {**self._entry.data, **self._entry.options}

        # Valore di default del campo attuatori: la lista già salvata, serializzata
        # in JSON leggibile così l'utente la ritrova e può modificarla.
        actuators_default = json.dumps(
            current.get(KEY_ACTUATORS, []), ensure_ascii=False, indent=2
        )

        if user_input is not None:
            local_proxy    = user_input.get("local_proxy", "").strip()
            use_local_udp  = user_input.get("use_local_udp", True)
            local_udp_port = int(user_input.get("local_udp_port", DEFAULT_LOCAL_UDP_PORT))
            media_enc      = media_enc_mode(user_input.get(KEY_MEDIA_ENC))
            voice_answer   = voice_answer_mode(user_input.get(KEY_VOICE_ANSWER))
            actuators_raw  = user_input.get(KEY_ACTUATORS, "")
            actuators_default = actuators_raw  # rimostra ciò che l'utente ha scritto

            # SGA/PICG: id SIP numerici (es. "55001"). Campo vuoto → fallback al
            # default storico in const.py (gestito da runtime.configure()), quindi
            # qui basta validare il formato quando l'utente scrive qualcosa.
            targets = {}
            for key in (KEY_SGA_TARGET, KEY_PICG_TARGET, KEY_CAMERA_TARGET,
                        KEY_INTERNAL_PANEL_TARGET, KEY_DOOR_TARGET):
                targets[key] = str(user_input.get(key, "")).strip()
                if targets[key] and not validate.sip_target(targets[key]):
                    errors[key] = "invalid_target"

            away_file  = str(user_input.get(KEY_AWAY_FILE, "")).strip()
            away_text  = str(user_input.get(KEY_AWAY_TEXT, "")).strip()
            away_tts   = str(user_input.get(KEY_AWAY_TTS) or "").strip()
            away_delay = user_input.get(KEY_AWAY_DELAY, 0)
            if len(away_text) > AWAY_TEXT_MAX:
                errors[KEY_AWAY_TEXT] = "text_too_long"
            # Come snapshot_dir: solo cartelle che HA può leggere (allowlist_external_dirs,
            # media). Il percorso va dritto a `ffmpeg -i`.
            if away_file and not self.hass.config.is_allowed_path(away_file):
                errors[KEY_AWAY_FILE] = "file_not_allowed"
            elif away_file and not await self.hass.async_add_executor_job(os.path.isfile, away_file):
                errors[KEY_AWAY_FILE] = "file_not_found"

            snap_dir   = str(user_input.get(KEY_SNAP_DIR, "")).strip()
            snap_delay = user_input.get(KEY_SNAP_DELAY, DEFAULT_SNAPSHOT_DELAY)
            view_ka    = user_input.get(KEY_VIEW_KA, view_keepalive_default(use_local_udp))
            if (use_local_udp != current.get(KEY_USE_LOCAL_UDP, True)
                    and view_ka == view_keepalive_default(current.get(KEY_USE_LOCAL_UDP, True))):
                view_ka = view_keepalive_default(use_local_udp)  # era il predefinito: segue la modalità
            allowed_users = [str(u) for u in user_input.get(KEY_ALLOWED_USERS) or []]
            ring_webhook_url     = str(user_input.get(KEY_RING_WEBHOOK_URL) or "").strip()
            ring_end_webhook_url = str(user_input.get(KEY_RING_END_WEBHOOK_URL) or "").strip()
            for key, url in ((KEY_RING_WEBHOOK_URL, ring_webhook_url),
                             (KEY_RING_END_WEBHOOK_URL, ring_end_webhook_url)):
                if not validate.http_url(url):
                    errors[key] = "invalid_url"
            if snap_dir and not self.hass.config.is_allowed_path(snap_dir):
                errors[KEY_SNAP_DIR] = "path_not_allowed"
            elif snap_dir and await self.hass.async_add_executor_job(
                    _inside, snap_dir, self.hass.config.path("www")):
                # /config/www è servita su /local SENZA login: la foto della strada
                # finirebbe leggibile da internet.
                errors[KEY_SNAP_DIR] = "path_public"

            actuators: list[dict] = []
            try:
                actuators = _parse_actuators(actuators_raw)
            except ValueError as exc:
                errors[KEY_ACTUATORS] = ("invalid_actuator_target" if isinstance(exc, InvalidActuatorTarget)
                                         else "invalid_actuators")
                self._actuators_error = str(exc)

            # Il test SIP live va fatto solo se cambiano davvero i parametri SIP:
            # rifarlo a ogni salvataggio (es. modifica solo attuatori) fallirebbe per
            # conflitto con l'integrazione già registrata e bloccherebbe il salvataggio.
            sip_changed = (
                local_proxy    != current.get(KEY_LOCAL_PROXY, "")
                or use_local_udp  != current.get(KEY_USE_LOCAL_UDP, True)
                or local_udp_port != current.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT)
            )
            if not _validate_ip(local_proxy):
                errors["local_proxy"] = "invalid_ip"
            elif use_local_udp and sip_changed:
                ok, msg = await _test_sip_registration(
                    sip_user      = current["sip_user"],
                    sip_password  = current["sip_password"],
                    # The domain runtime.configure uses in local UDP mode
                    # (as _local_test_domain in the setup flow).
                    sip_domain    = current.get(KEY_LOCAL_DOMAIN) or current["sip_domain"],
                    local_proxy   = local_proxy,
                    local_udp_port = local_udp_port,
                    device_imei   = str(current.get("device_imei") or ""),
                    device_uuid   = str(current.get("device_uuid") or ""),
                    device_name   = str(current.get("device_name") or MY_NAME),
                    # The entry is set up and registered with this identity:
                    # do not leave its binding on the test's closed socket.
                    unregister    = True,
                )
                if not ok:
                    errors["local_proxy"] = "sip_registration_failed"
                else:
                    # The test's unregister may have taken the live binding
                    # with it: register again now, whether or not the form is
                    # saved (saving reloads and registers anyway).
                    entry_data = getattr(self.hass, "data", {}).get(DOMAIN, {}).get(
                        getattr(self._entry, "entry_id", None), {})
                    hub = entry_data.get("hub") if isinstance(entry_data, dict) else None
                    if hub is not None:
                        self.hass.async_create_background_task(
                            hub.async_register_now(), "vimar_intercom re-register")

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        # Options this page does not show (HomeKit) keep their
                        # value; without this, saving the network settings
                        # turned the HomeKit doorbell off.
                        **self._entry.options,
                        KEY_LOCAL_PROXY:    local_proxy,
                        KEY_USE_LOCAL_UDP:  use_local_udp,
                        KEY_LOCAL_UDP_PORT: local_udp_port,
                        KEY_MEDIA_ENC:      media_enc,
                        KEY_VOICE_ANSWER:   voice_answer,
                        KEY_ACTUATORS:      actuators,
                        **targets,
                        KEY_AWAY_FILE:      away_file,
                        KEY_AWAY_TEXT:      away_text,
                        KEY_AWAY_TTS:       away_tts,
                        KEY_AWAY_DELAY:     away_delay,
                        KEY_SNAP_DIR:       snap_dir,
                        KEY_SNAP_DELAY:     snap_delay,
                        KEY_VIEW_KA:        view_ka,
                        KEY_ALLOWED_USERS:  allowed_users,
                        KEY_RING_WEBHOOK_URL:     ring_webhook_url,
                        KEY_RING_END_WEBHOOK_URL: ring_end_webhook_url,
                    },
                )

        # Il form si ricompone su ciò che l'utente ha appena inviato, non solo
        # sui valori salvati: ricostruirlo da `current` scarterebbe in silenzio
        # tutte le altre modifiche fatte insieme a quella che non ha passato la
        # validazione. `current` resta intatto perché serve ai confronti sopra
        # (sip_changed) per capire cosa è davvero cambiato.
        form = {**current, **(user_input or {})}
        # HA non ha un selettore di utenti: elenco a scelta multipla dagli utenti veri
        # (non quelli di sistema). Un utente cancellato sparisce dalla lista al salvataggio.
        users = [{"value": u.id, "label": u.name or u.id}
                 for u in await self.hass.auth.async_get_users() if not u.system_generated]

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema({
                vol.Required(
                    "local_proxy",
                    default=form.get(KEY_LOCAL_PROXY, "")
                ): str,
                vol.Optional(
                    "use_local_udp",
                    default=form.get(KEY_USE_LOCAL_UDP, True)
                ): bool,
                vol.Optional(
                    "local_udp_port",
                    default=form.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT)
                ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
                # auto (segue il media_enc dichiarato dall'impianto) / on / off (issue #4).
                vol.Optional(
                    KEY_MEDIA_ENC,
                    default=media_enc_mode(form.get(KEY_MEDIA_ENC)),
                ): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=list(MEDIA_ENC_MODES),
                    translation_key="media_enc",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
                # Chi può rispondere a voce su /audio_ws: dichiarato / mai / chiunque.
                vol.Optional(
                    KEY_VOICE_ANSWER,
                    default=voice_answer_mode(form.get(KEY_VOICE_ANSWER)),
                ): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=list(VOICE_ANSWER_MODES),
                    translation_key="voice_answer",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
                vol.Optional(
                    KEY_ACTUATORS,
                    default=actuators_default,
                ): str,
                vol.Optional(
                    KEY_SGA_TARGET,
                    default=form.get(KEY_SGA_TARGET) or SGA_TARGET,
                ): str,
                vol.Optional(
                    KEY_PICG_TARGET,
                    default=form.get(KEY_PICG_TARGET) or PICG_TARGET,
                ): str,
                # Empty is a valid value: the panel learned from the last ring
                # with video (hub._camera_fallback), else 55100. Pre-filling 55100
                # saved it on the first save and the fallback never ran again.
                vol.Optional(
                    KEY_CAMERA_TARGET,
                    default=form.get(KEY_CAMERA_TARGET) or "",
                ): str,
                vol.Optional(
                    KEY_INTERNAL_PANEL_TARGET,
                    default=form.get(KEY_INTERNAL_PANEL_TARGET) or INTERNAL_PANEL_TARGET,
                ): str,
                # Nessun default fisso: vuoto è un valore valido (ripiego in
                # runtime.configure), non «55001».
                vol.Optional(
                    KEY_DOOR_TARGET,
                    default=form.get(KEY_DOOR_TARGET) or "",
                ): str,
                vol.Optional(
                    KEY_AWAY_FILE,
                    default=form.get(KEY_AWAY_FILE, ""),
                ): str,
                vol.Optional(
                    KEY_AWAY_TEXT,
                    default=form.get(KEY_AWAY_TEXT, ""),
                ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
                # Niente default: un EntitySelector non accetta "" (vuoto = motore
                # predefinito di HA); il valore salvato torna come suggerimento.
                vol.Optional(
                    KEY_AWAY_TTS,
                    description={"suggested_value": form.get(KEY_AWAY_TTS) or None},
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="tts")),
                vol.Optional(
                    KEY_AWAY_DELAY,
                    default=form.get(KEY_AWAY_DELAY, 0),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=60)),
                vol.Optional(
                    KEY_SNAP_DIR,
                    default=form.get(KEY_SNAP_DIR, ""),
                ): str,
                vol.Optional(
                    KEY_SNAP_DELAY,
                    default=form.get(KEY_SNAP_DELAY, DEFAULT_SNAPSHOT_DELAY),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=30)),
                vol.Optional(
                    KEY_VIEW_KA,
                    default=form.get(KEY_VIEW_KA, view_keepalive_default(
                        form.get(KEY_USE_LOCAL_UDP, True))),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                vol.Optional(
                    KEY_ALLOWED_USERS,
                    default=[u for u in form.get(KEY_ALLOWED_USERS) or [] if any(u == x["value"] for x in users)],
                ): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=users, multiple=True, mode="list")),  # caselle, non un menu
                # password: l'URL può portare un token segreto (es. Scrypted), non va
                # mostrato in chiaro nel form.
                vol.Optional(
                    KEY_RING_WEBHOOK_URL,
                    default=form.get(KEY_RING_WEBHOOK_URL, ""),
                ): selector.TextSelector(selector.TextSelectorConfig(type="password")),
                vol.Optional(
                    KEY_RING_END_WEBHOOK_URL,
                    default=form.get(KEY_RING_END_WEBHOOK_URL, ""),
                ): selector.TextSelector(selector.TextSelectorConfig(type="password")),
            }),
            errors=errors,
            description_placeholders={
                "actuators_error": getattr(self, "_actuators_error", "") or "",
            },
        )

    async def async_step_homekit(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """The HomeKit video doorbell: on/off, video mode, when a ring is answered."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self._entry.options,
                    CONF_HOMEKIT_ACCESSORY: bool(user_input.get(CONF_HOMEKIT_ACCESSORY)),
                    CONF_HOMEKIT_SMOOTH: bool(user_input.get(CONF_HOMEKIT_SMOOTH)),
                    CONF_HOMEKIT_ANSWER: user_input.get(
                        CONF_HOMEKIT_ANSWER, DEFAULT_HOMEKIT_ANSWER),
                    CONF_HOMEKIT_RING_BUTTON: bool(user_input.get(CONF_HOMEKIT_RING_BUTTON)),
                },
            )
        current = self._entry.options
        return self.async_show_form(
            step_id="homekit",
            description_placeholders={"pairing": _homekit_pairing_text(
                getattr(self, "hass", None), getattr(self._entry, "entry_id", None))},
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_HOMEKIT_ACCESSORY,
                    default=current.get(CONF_HOMEKIT_ACCESSORY, DEFAULT_HOMEKIT_ACCESSORY),
                ): bool,
                vol.Optional(
                    CONF_HOMEKIT_SMOOTH,
                    default=current.get(CONF_HOMEKIT_SMOOTH, DEFAULT_HOMEKIT_SMOOTH),
                ): bool,
                vol.Optional(
                    CONF_HOMEKIT_ANSWER,
                    default=current.get(CONF_HOMEKIT_ANSWER, DEFAULT_HOMEKIT_ANSWER),
                ): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[HOMEKIT_ANSWER_TALK, HOMEKIT_ANSWER_OPEN],
                    translation_key="homekit_answer",
                    mode=selector.SelectSelectorMode.LIST,
                )),
                vol.Optional(
                    CONF_HOMEKIT_RING_BUTTON,
                    default=current.get(CONF_HOMEKIT_RING_BUTTON, DEFAULT_HOMEKIT_RING_BUTTON),
                ): bool,
            }),
        )

    async def async_step_fetch_rubrica(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Scarica la rubrica **dal citofono**, senza doverla estrarre a mano.

        Usa l'API HTTP che il citofono espone in rete locale (`rest_client`)
        con le stesse credenziali SIP già nel config entry: niente token,
        niente account Vimar, niente cloud. Nella stessa occasione chiede i
        nickname, così il PICG lo **dichiara il citofono** invece di doverlo
        indovinare o leggere dalla rubrica.

        Funziona solo se il citofono è raggiungibile in LAN sulla porta 80; per
        gli impianti che si raggiungono solo dal cloud resta la voce «importa
        da file».
        """
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}
        host = (current.get(KEY_LOCAL_PROXY) or "").strip()
        default_gid = str(current.get(KEY_GID) or "101")

        if not host:
            # Senza indirizzo del citofono non c'è niente da contattare.
            errors["base"] = "no_local_proxy"
        elif user_input is not None:
            gid = (user_input.get("rubrica_gid") or default_gid).strip() or default_gid
            sip_user = current.get(KEY_SIP_USER, "")
            sip_password = current.get(KEY_SIP_PASSWORD, "")

            def _fetch() -> tuple[dict, str | None]:
                """Scarica, legge, ripulisce. Tutto bloccante, quindi executor."""
                fd, path = tempfile.mkstemp(suffix=".db", prefix="vimar_rubrica_")
                os.close(fd)
                try:
                    rest_client.download_db(
                        host, sip_user, sip_password, rest_client.DB_RUBRICA, dest=path
                    )
                    parsed = rubrica_import.parse_rubrica_file(path, gid)
                finally:
                    try:
                        os.unlink(path)
                    except OSError:  # pragma: no cover — file già rimosso
                        pass

                # I nickname sono un di più: se non arrivano, l'import della
                # rubrica resta valido e il PICG rimane quello configurato.
                picg: str | None = None
                try:
                    picg = rest_client.find_picg(
                        rest_client.get_nicknames(host, sip_user, sip_password)
                    )
                except rest_client.RestError:
                    _LOGGER.debug("nickname non disponibili da %s", host)
                return parsed, picg

            try:
                result, picg = await self.hass.async_add_executor_job(_fetch)
            except rest_client.RestAuthError as exc:
                errors["base"] = "rest_auth_failed"
                self._rubrica_error = str(exc)
            except rest_client.RestUnavailable as exc:
                errors["base"] = "rest_unavailable"
                self._rubrica_error = str(exc)
            except (rest_client.RestError, rubrica_import.RubricaImportError,
                    ValueError, OSError) as exc:
                errors["base"] = "rubrica_import_failed"
                self._rubrica_error = str(exc)
            else:
                if not result["actuators"]:
                    errors["base"] = "rubrica_no_actuators"
                    self._rubrica_error = (
                        f"Rubrica scaricata, ma nessun attuatore per il GID {gid}."
                    )
                else:
                    self._imported = result
                    self._imported_gid = gid
                    self._picg_from_rest = picg
                    return await self.async_step_import_confirm()

        return self.async_show_form(
            step_id="fetch_rubrica",
            data_schema=vol.Schema({
                vol.Optional(
                    "rubrica_gid",
                    default=(user_input or {}).get("rubrica_gid") or default_gid,
                ): str,
            }),
            errors=errors,
            description_placeholders={
                "host": host or "—",
                "rubrica_error": self._rubrica_error or "",
            },
        )

    async def _cloud_token(self) -> tuple[str | None, str | None, str | None]:
        """(token, rubrica_ver, GID) dall'ultima risposta a GET_INIT_STATUS dell'hub.

        Se il token manca si richiede lo stato una volta e si aspetta qualche secondo:
        la risposta arriva come MESSAGE separato. Il token non si salva da nessuna parte.
        """
        hub = (self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}).get("hub")
        if hub is None:
            return None, None, None

        def _read():
            st = hub.stats
            return ((st.get("init_status") or {}).get("token"), st.get("rubrica_ver"),
                    st.get("apt_gid"))

        token, ver, gid = _read()
        if not token and hub.registered:
            await hub.async_request_status()
            for _ in range(10):
                await asyncio.sleep(0.5)
                token, ver, gid = _read()
                if token:
                    break
        return token, ver, (str(gid) if gid not in (None, "") else None)

    async def async_step_fetch_rubrica_cloud(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Scarica la rubrica **dal cloud Vimar** col `token` (issue #5).

        Solo sugli impianti che mandano la risposta lunga di GET_INIT_STATUS (visto su un
        40515 / 2FV2): lì c'è il token, e il file è lo stesso rubrica.db dell'app VIEW.
        Per gli impianti solo cloud senza porta 80 in LAN è l'unica via senza estrarre
        il file a mano.
        """
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}
        cproxy = (current.get(KEY_CLOUD_PROXY) or "").strip()
        cdomain = (current.get(KEY_CLOUD_DOMAIN) or "").strip()
        if not cdomain and cproxy and str(current.get(KEY_SIP_DOMAIN, "")).endswith("." + cproxy):
            cdomain = current[KEY_SIP_DOMAIN]   # entry manuale: il dominio SIP è quello cloud
        token, ver, plant_gid = await self._cloud_token()
        default_gid = plant_gid or str(current.get(KEY_GID) or "101")

        if not token:
            errors["base"] = "no_cloud_token"
        elif cloud_phonebook.check_inputs(cdomain, cproxy, token, ver):
            errors["base"] = "cloud_failed"
            self._rubrica_error = (
                f"manca {cloud_phonebook.check_inputs(cdomain, cproxy, token, ver)} "
                "(dominio e proxy cloud vengono dal QR)")
        elif user_input is not None:
            gid = (user_input.get("rubrica_gid") or default_gid).strip() or default_gid

            def _fetch() -> dict:
                data = cloud_phonebook.download(cdomain, cproxy, token, ver)
                fd, path = tempfile.mkstemp(suffix=".db", prefix="vimar_rubrica_")
                os.close(fd)
                try:
                    with open(path, "wb") as fh:
                        fh.write(data)
                    return rubrica_import.parse_rubrica_file(path, gid)
                finally:
                    try:
                        os.unlink(path)
                    except OSError:  # pragma: no cover
                        pass

            try:
                result = await self.hass.async_add_executor_job(_fetch)
            except cloud_phonebook.CloudAuthError as exc:
                errors["base"] = "cloud_auth_failed"
                self._rubrica_error = str(exc)
            except (cloud_phonebook.CloudPhonebookError, rubrica_import.RubricaImportError,
                    ValueError, OSError) as exc:
                errors["base"] = "cloud_failed"
                self._rubrica_error = str(exc)
            else:
                if not result["actuators"]:
                    errors["base"] = "rubrica_no_actuators"
                    self._rubrica_error = (
                        f"Rubrica scaricata, ma nessun attuatore per il GID {gid}.")
                else:
                    self._imported = result
                    self._imported_gid = gid
                    self._picg_from_rest = None
                    return await self.async_step_import_confirm()

        return self.async_show_form(
            step_id="fetch_rubrica_cloud",
            data_schema=vol.Schema({
                vol.Optional("rubrica_gid",
                             default=(user_input or {}).get("rubrica_gid") or default_gid): str,
            }),
            errors=errors,
            description_placeholders={
                "cproxy": cproxy or "—",
                "rubrica_error": self._rubrica_error or "",
            },
        )

    async def async_step_import_rubrica(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Carica un file rubrica.db ed estrae attuatori + SGA/parametri SYSTEM,
        sostituendo la necessità di girare tools/parse_rubrica.py a mano e
        incollarne l'output nel campo Attuatori (JSON)."""
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}
        default_gid = str(current.get(KEY_GID) or "101")

        if user_input is not None:
            gid = (user_input.get("rubrica_gid") or default_gid).strip() or default_gid
            upload_id = user_input.get("rubrica_file")

            def _load() -> dict:
                # process_uploaded_file è un context manager sincrono: la lettura
                # del file (SQLite) è I/O bloccante, quindi tutto il blocco gira
                # nell'executor, mai nel loop asyncio.
                with process_uploaded_file(self.hass, upload_id) as file_path:
                    return rubrica_import.parse_rubrica_file(str(file_path), gid)

            try:
                result = await self.hass.async_add_executor_job(_load)
            except (rubrica_import.RubricaImportError, ValueError, OSError) as exc:
                errors["rubrica_file"] = "rubrica_import_failed"
                self._rubrica_error = str(exc)
            else:
                if not result["actuators"]:
                    errors["rubrica_file"] = "rubrica_no_actuators"
                    self._rubrica_error = (
                        f"Nessun attuatore trovato per il GID appartamento {gid}."
                    )
                else:
                    self._imported = result
                    self._imported_gid = gid
                    return await self.async_step_import_confirm()

        return self.async_show_form(
            step_id="import_rubrica",
            data_schema=vol.Schema({
                vol.Required("rubrica_file"): selector.FileSelector(
                    selector.FileSelectorConfig(accept=".db")
                ),
                # Come sopra: se l'import fallisce, il GID digitato resta nel
                # campo invece di tornare al valore salvato.
                vol.Optional(
                    "rubrica_gid",
                    default=(user_input or {}).get("rubrica_gid") or default_gid,
                ): str,
            }),
            errors=errors,
            description_placeholders={
                "rubrica_error": self._rubrica_error or "",
            },
        )

    def _confirm_picg(self, current: dict, sga: str | None) -> str:
        """Il picg_target che la conferma salverà — unica fonte per riepilogo e salvataggio.

        In ordine: il PICG dichiarato dal citofono (rubrica scaricata, nickname
        disponibili); altrimenti quello già configurato, che un import da file
        non deve toccare perché il file non dice nulla sul PICG; solo se non ne
        è configurato nessuno, l'SGA della rubrica (sugli impianti visti finora
        coincidono), e infine il default.

        Prima della correzione il riepilogo prometteva «resta quello già
        configurato» mentre il salvataggio lo sovrascriveva con l'SGA: un 60001
        messo a mano per un 2FV2 diventava 55001.
        """
        return (
            self._picg_from_rest
            or current.get(KEY_PICG_TARGET)
            or sga
            or PICG_TARGET
        )

    async def async_step_import_confirm(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Riepilogo di ciò che è stato letto da rubrica.db; confermando si
        sostituisce la lista attuatori corrente con quella importata."""
        current = {**self._entry.data, **self._entry.options}
        result = self._imported or {"actuators": [], "sga": None}
        actuators: list[dict] = result["actuators"]
        sga = result.get("sga")
        camera = result.get("camera")
        door = result.get("door")

        if user_input is not None:
            # Due fonti distinte per due valori distinti:
            #   sga_target  ← SYSTEM.MAGIC_APT_INTERCOM della rubrica
            #   picg_target ← il ruolo PICG dichiarato dal citofono nei nickname
            new_sga  = sga or current.get(KEY_SGA_TARGET) or SGA_TARGET
            new_picg = self._confirm_picg(current, sga)
            #   camera_target ← PHONEBOOK.AUTO del proprio GA, o la prima PE
            new_camera = camera or current.get(KEY_CAMERA_TARGET) or ""
            #   door_target ← GID_PE dell'attuatore porta (non l'SGA)
            new_door = door or current.get(KEY_DOOR_TARGET) or ""
            return self.async_create_entry(
                title="",
                data={
                    # Le altre opzioni (messaggio di assenza, foto...)
                    # non vengono dalla rubrica: restano quelle configurate.
                    **self._entry.options,
                    KEY_LOCAL_PROXY:    current.get(KEY_LOCAL_PROXY, ""),
                    KEY_USE_LOCAL_UDP:  current.get(KEY_USE_LOCAL_UDP, True),
                    KEY_LOCAL_UDP_PORT: current.get(KEY_LOCAL_UDP_PORT, DEFAULT_LOCAL_UDP_PORT),
                    KEY_MEDIA_ENC:      media_enc_mode(current.get(KEY_MEDIA_ENC)),
                    KEY_ACTUATORS:      actuators,
                    KEY_SGA_TARGET:     new_sga,
                    KEY_PICG_TARGET:    new_picg,
                    KEY_CAMERA_TARGET:  new_camera,
                    # La rubrica non dice quale sia il pannello interno:
                    # l'import non lo tocca.
                    KEY_INTERNAL_PANEL_TARGET: current.get(KEY_INTERNAL_PANEL_TARGET, ""),
                    KEY_DOOR_TARGET:    new_door,
                },
            )

        current_sga = current.get(KEY_SGA_TARGET) or SGA_TARGET
        if sga and sga != current_sga:
            sga_info = (
                f"{sga} — diverso da quello attualmente configurato ({current_sga}); "
                "confermando verrà impostato come nuovo sga_target/picg_target."
            )
        elif sga:
            sga_info = f"{sga} — coincide con quello già in uso."
        else:
            sga_info = "non trovato (tabella SYSTEM assente o senza MAGIC_APT_INTERCOM); resta invariato quello già configurato."

        configured_picg = current.get(KEY_PICG_TARGET)
        new_picg = self._confirm_picg(current, sga)
        if self._picg_from_rest and self._picg_from_rest != (configured_picg or PICG_TARGET):
            picg_info = (
                f"{self._picg_from_rest} — dichiarato dal citofono stesso, "
                f"diverso da quello configurato ({configured_picg or PICG_TARGET}); "
                "confermando verrà impostato come nuovo picg_target."
            )
        elif self._picg_from_rest:
            picg_info = f"{self._picg_from_rest} — dichiarato dal citofono, coincide con quello già in uso."
        elif configured_picg:
            picg_info = (
                f"non richiesto (rubrica da file): resta quello già configurato ({configured_picg})."
            )
        else:
            picg_info = (
                f"non richiesto (rubrica da file) e non ancora configurato: verrà impostato "
                f"uguale all'SGA ({new_picg})."
            )

        current_camera = current.get(KEY_CAMERA_TARGET) or CAMERA_TARGET
        if camera and camera != current_camera:
            camera_info = (
                f"{camera} — diversa da quella attualmente configurata ({current_camera}); "
                "confermando verrà impostata come nuova camera_target."
            )
        elif camera:
            camera_info = f"{camera} — coincide con quella già in uso."
        else:
            camera_info = (
                f"nessuna targa (PE) trovata nella rubrica; resta quella già configurata ({current_camera})."
            )

        current_door = current.get(KEY_DOOR_TARGET) or ""
        if door and door != current_door:
            door_info = (
                f"{door} — la targa dell'attuatore porta (GID_PE)"
                + (f", diversa da quella configurata ({current_door})" if current_door else "")
                + "; confermando il comando di apertura andrà lì."
            )
        elif door:
            door_info = f"{door} — coincide con quella già in uso."
        elif current_door:
            door_info = (
                f"nessun attuatore porta nella rubrica; resta quella già configurata ({current_door})."
            )
        else:
            door_info = (
                f"nessun attuatore porta nella rubrica; il comando di apertura resta all'SGA "
                f"({sga or current_sga})."
            )

        return self.async_show_form(
            step_id="import_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "count": str(len(actuators)),
                "names": ", ".join(a["name"] for a in actuators) or "—",
                "gid": self._imported_gid,
                "sga_info": sga_info,
                "picg_info": picg_info,
                "camera_info": camera_info,
                "door_info": door_info,
            },
        )
