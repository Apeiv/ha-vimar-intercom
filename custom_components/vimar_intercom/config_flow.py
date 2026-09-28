"""Config flow per Vimar Intercom — onboarding via QR o credenziali manuali."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import random
import socket
import tempfile
from pathlib import Path

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CAMERA_TARGET,
    DEFAULT_SNAPSHOT_DELAY,
    DOMAIN,
    INTERNAL_PANEL_TARGET,
    PICG_TARGET,
    SGA_TARGET,
)
from . import qr_decoder
from . import rest_client
from . import rubrica_import
from . import validate
from .runtime import MEDIA_ENC_MODES, media_enc_mode

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
KEY_USE_LOCAL_UDP  = "use_local_udp"
KEY_LOCAL_UDP_PORT = "local_udp_port"
KEY_ACTUATORS      = "actuators"
KEY_MEDIA_ENC      = "media_enc"
KEY_SGA_TARGET     = "sga_target"
KEY_PICG_TARGET    = "picg_target"
KEY_CAMERA_TARGET  = "camera_target"
KEY_INTERNAL_PANEL_TARGET = "internal_panel_target"
KEY_DOOR_TARGET    = "door_target"
KEY_AWAY_FILE      = "away_message_file"
KEY_AWAY_DELAY     = "away_message_delay"
KEY_SNAP_DIR       = "snapshot_dir"
KEY_SNAP_DELAY     = "snapshot_delay"

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
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """Tenta una registrazione SIP UDP e restituisce (successo, messaggio)."""

    def _run() -> tuple[bool, str]:
        def rand_hex(n: int = 8) -> str:
            return f"{random.randint(0, 16**n - 1):0{n}x}"

        call_id  = rand_hex(16)
        from_tag = rand_hex(8)
        uri      = f"sip:{sip_domain}"

        ha1_cache: dict[str, str] = {}

        def compute_ha1(realm: str) -> str:
            if realm not in ha1_cache:
                ha1_cache[realm] = hashlib.md5(
                    f"{sip_user}:{realm}:{sip_password}".encode()
                ).hexdigest()
            return ha1_cache[realm]

        def make_register(auth_hdr: str | None = None, seq: int = 1) -> bytes:
            my_ip = "0.0.0.0"
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect((local_proxy, DEFAULT_LOCAL_SIP_PORT))
                my_ip = s.getsockname()[0]
                s.close()
            except Exception:
                pass
            branch = f"z9hG4bK{rand_hex()}"
            lines = [
                f"REGISTER {uri} SIP/2.0",
                f"Via: SIP/2.0/UDP {my_ip}:{local_udp_port};branch={branch};rport",
                "Max-Forwards: 70",
                f"To: <sip:{sip_user}@{sip_domain}>",
                f"From: <sip:{sip_user}@{sip_domain}>;tag={from_tag}",
                f"Call-ID: {call_id}",
                f"CSeq: {seq} REGISTER",
                f"Contact: <sip:{sip_user}@{my_ip}:{local_udp_port}>",
                "Expires: 60",
                "User-Agent: HomeAssistant/VimarIntercom",
            ]
            if auth_hdr:
                lines.append(f"Authorization: {auth_hdr}")
            lines += ["Content-Length: 0", "", ""]
            return "\r\n".join(lines).encode()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.bind(("0.0.0.0", 0))
            sock.sendto(make_register(seq=1), (local_proxy, DEFAULT_LOCAL_SIP_PORT))
            data, _ = sock.recvfrom(65535)
            resp     = data.decode(errors="replace")
            first    = resp.split("\r\n", 1)[0]

            if "200" in first:
                return True, "Registrazione riuscita (senza auth)"

            if "401" not in first:
                return False, f"Risposta inattesa: {first}"

            # Estrai challenge
            nonce = realm = opaque = qop = ""
            for line in resp.split("\r\n"):
                lo = line.lower()
                if lo.startswith("www-authenticate:"):
                    for part in line.split(","):
                        part = part.strip()
                        for field in ("nonce", "realm", "opaque", "qop"):
                            if part.lower().startswith(field + "="):
                                val = part.split("=", 1)[1].strip().strip('"')
                                if field == "nonce":  nonce  = val
                                if field == "realm":  realm  = val
                                if field == "opaque": opaque = val
                                if field == "qop":    qop    = val

            if not nonce or not realm:
                return False, "Challenge 401 senza nonce/realm"

            ha1    = compute_ha1(realm)
            ha2    = hashlib.md5(f"REGISTER:{uri}".encode()).hexdigest()
            nc     = "00000001"
            cnonce = rand_hex(8)

            if "auth" in qop:
                rh = hashlib.md5(
                    f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()
                ).hexdigest()
                auth = (
                    f'Digest username="{sip_user}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{rh}", '
                    f'algorithm=MD5, qop=auth, nc={nc}, cnonce="{cnonce}"'
                )
            else:
                rh   = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
                auth = (
                    f'Digest username="{sip_user}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{rh}", algorithm=MD5'
                )
            if opaque:
                auth += f', opaque="{opaque}"'

            sock.sendto(make_register(auth_hdr=auth, seq=2), (local_proxy, DEFAULT_LOCAL_SIP_PORT))
            data2, _ = sock.recvfrom(65535)
            resp2    = data2.decode(errors="replace")
            first2   = resp2.split("\r\n", 1)[0]

            if "200" in first2:
                return True, "Registrazione riuscita"
            return False, f"Risposta auth: {first2}"

        except socket.timeout:
            return False, (
                f"Timeout ({timeout:.0f}s) — il citofono non e' raggiungibile "
                f"su {local_proxy}:{DEFAULT_LOCAL_SIP_PORT}. "
                "Verifica che HA e il citofono siano sulla stessa rete."
            )
        except OSError as exc:
            return False, f"Errore socket: {exc}"
        finally:
            sock.close()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


# ─── Config Flow ─────────────────────────────────────────────────────────────


def _inside(path: str, folder: str) -> bool:
    """True se path è folder o sta sotto (link risolti; /config/www2 non conta)."""
    return Path(path).resolve().is_relative_to(Path(folder).resolve())

class VimarIntercomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce l'onboarding dell'integrazione Vimar Intercom."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict = {}
        self._qr_error: str | None = None

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step iniziale: scelta modalità (QR o manuale)."""
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
                return await self.async_step_network()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required("sip_user"):     str,
                vol.Required("sip_password"): str,
                vol.Required("sip_domain"):   str,
                vol.Optional("cloud_proxy", default=DEFAULT_CLOUD_PROXY): str,
            }),
            errors=errors,
        )

    async def async_step_network(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Step 3: IP citofono, porta UDP, test registrazione."""
        errors: dict[str, str] = {}

        if user_input is not None:
            local_proxy    = user_input.get("local_proxy", "").strip()
            use_local_udp  = user_input.get("use_local_udp", True)
            local_udp_port = int(user_input.get("local_udp_port", DEFAULT_LOCAL_UDP_PORT))

            if not local_proxy:
                errors["local_proxy"] = "required"
            elif not _validate_ip(local_proxy):
                errors["local_proxy"] = "invalid_ip"
            elif use_local_udp:
                ok, msg = await _test_sip_registration(
                    sip_user      = self._credentials["sip_user"],
                    sip_password  = self._credentials["sip_password"],
                    sip_domain    = self._credentials["sip_domain"],
                    local_proxy   = local_proxy,
                    local_udp_port = local_udp_port,
                )
                _LOGGER.info("SIP test: ok=%s msg=%s", ok, msg)
                if not ok:
                    errors["local_proxy"] = "sip_registration_failed"
                    self._qr_error = msg

            if not errors:
                data = {
                    **self._credentials,
                    KEY_LOCAL_PROXY:    local_proxy,
                    KEY_USE_LOCAL_UDP:  use_local_udp,
                    KEY_LOCAL_UDP_PORT: local_udp_port,
                }
                unique_id = (
                    f"{self._credentials['sip_user']}@"
                    f"{self._credentials['sip_domain']}"
                )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Vimar Intercom ({local_proxy})",
                    data=data,
                )

        return self.async_show_form(
            step_id="network",
            data_schema=vol.Schema({
                vol.Required("local_proxy"): str,
                vol.Optional("use_local_udp", default=True): bool,
                vol.Optional(
                    "local_udp_port", default=DEFAULT_LOCAL_UDP_PORT
                ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
            }),
            errors=errors,
            description_placeholders={
                "sip_user":   self._credentials.get("sip_user", ""),
                "sip_domain": self._credentials.get("sip_domain", ""),
                "mac":        self._credentials.get("mac", ""),
                "error_detail": self._qr_error or "",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        return OptionsFlowHandler(config_entry)


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
            menu_options=["settings", "fetch_rubrica", "import_rubrica"],
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
            away_delay = user_input.get(KEY_AWAY_DELAY, 0)
            # Come snapshot_dir: solo cartelle che HA può leggere (allowlist_external_dirs,
            # media). Il percorso va dritto a `ffmpeg -i`.
            if away_file and not self.hass.config.is_allowed_path(away_file):
                errors[KEY_AWAY_FILE] = "file_not_allowed"
            elif away_file and not await self.hass.async_add_executor_job(os.path.isfile, away_file):
                errors[KEY_AWAY_FILE] = "file_not_found"

            snap_dir   = str(user_input.get(KEY_SNAP_DIR, "")).strip()
            snap_delay = user_input.get(KEY_SNAP_DELAY, DEFAULT_SNAPSHOT_DELAY)
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
                    sip_domain    = current["sip_domain"],
                    local_proxy   = local_proxy,
                    local_udp_port = local_udp_port,
                )
                if not ok:
                    errors["local_proxy"] = "sip_registration_failed"

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        KEY_LOCAL_PROXY:    local_proxy,
                        KEY_USE_LOCAL_UDP:  use_local_udp,
                        KEY_LOCAL_UDP_PORT: local_udp_port,
                        KEY_MEDIA_ENC:      media_enc,
                        KEY_ACTUATORS:      actuators,
                        **targets,
                        KEY_AWAY_FILE:      away_file,
                        KEY_AWAY_DELAY:     away_delay,
                        KEY_SNAP_DIR:       snap_dir,
                        KEY_SNAP_DELAY:     snap_delay,
                    },
                )

        # Il form si ricompone su ciò che l'utente ha appena inviato, non solo
        # sui valori salvati: ricostruirlo da `current` scarterebbe in silenzio
        # tutte le altre modifiche fatte insieme a quella che non ha passato la
        # validazione. `current` resta intatto perché serve ai confronti sopra
        # (sip_changed) per capire cosa è davvero cambiato.
        form = {**current, **(user_input or {})}

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
                vol.Optional(
                    KEY_CAMERA_TARGET,
                    default=form.get(KEY_CAMERA_TARGET) or CAMERA_TARGET,
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
            }),
            errors=errors,
            description_placeholders={
                "actuators_error": getattr(self, "_actuators_error", "") or "",
            },
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
