"""Vimar Intercom integration for Home Assistant."""

import asyncio
import ipaddress
import json
import logging
import os
from pathlib import Path

from aiohttp import web

import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.exceptions import ConfigEntryNotReady, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from . import away_tts
from . import log_buffer as _log_buffer
from . import validate
from .hub import VimarIntercomHub
from . import av_stream
from . import media_handler as media
from . import ring_log
from . import push_sender
from . import sip_client as sip
from . import runtime

_LOGGER = logging.getLogger(__name__)

# Buffer interno dei log e inoltro al log di HA: vedi log_buffer.py.
_debug_log = _log_buffer.debug_log
_log_buffer.install()

PLATFORMS = ["camera", "lock", "button", "event", "binary_sensor", "sensor", "switch", "select"]

# ─── Servizi ──────────────────────────────────────────────────────────────────
SERVICE_SEND_COMMAND = "send_command"
SERVICE_CALL = "call"
SERVICE_ANSWER = "answer"
SERVICE_HANGUP = "hangup"
SERVICE_OPEN_DOOR = "open_door"
SERVICE_FETCH_LOCAL = "fetch_local"
SERVICE_SIMULATE_RING = "simulate_ring"
SERVICE_FIND_SGA = "find_sga"

CARD_URL = "/vimar_intercom/vimar-intercom-card.js"


async def _register_card(hass: HomeAssistant) -> None:
    """Card del citofono (www/vimar-intercom-card.js): servita e caricata dal
    frontend da sola, una volta sola anche dopo un reload dell'entry."""
    if hass.data.get(f"{DOMAIN}_card"):
        return
    path = str(Path(__file__).parent / "www" / "vimar-intercom-card.js")
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, path, False)])
    # ?v= cambia a ogni modifica del file, così browser e app non tengono la versione vecchia.
    mtime = int(await hass.async_add_executor_job(os.path.getmtime, path))
    add_extra_js_url(hass, f"{CARD_URL}?v={mtime}")
    hass.data[f"{DOMAIN}_card"] = True


def _sip_id(value) -> str:
    """Id SIP numerico, o errore leggibile nel servizio (non un 500)."""
    if (target := validate.sip_target(value)) is None:
        raise vol.Invalid(f"id SIP non valido: {value!r} (serve un numero, es. 55001)")
    return target


SEND_COMMAND_SCHEMA = vol.Schema({
    vol.Required("body"): cv.string,
    vol.Optional("target"): cv.string,  # vuoto: l'SGA, in hub.async_send_command
    vol.Optional("header_name", default="Panda"): cv.string,
    vol.Optional("header_value", default="command"): cv.string,
})
CALL_SCHEMA = vol.Schema({vol.Optional("target"): _sip_id})
FETCH_LOCAL_SCHEMA = vol.Schema({
    vol.Required("path"): cv.string,                    # es. rest/get_info.php?action=status
    vol.Optional("save_as"): cv.string,                 # nome file in /config (opzionale)
    vol.Optional("host"): cv.string,                    # default: local_proxy
    vol.Optional("scheme", default="http"): cv.string,
})
# Nessun default per target: senza, hub.async_door usa runtime.DOOR_TARGET (la
# targa che apre la porta, dalla rubrica). Con l'SGA come default, su un 2FV2
# il comando andava al 61000, che risponde 200 e non apre.
FIND_SGA_SCHEMA = vol.Schema({
    vol.Optional("start", default="55000"): cv.string,
    vol.Optional("end", default="55010"): cv.string,
    vol.Optional("targets"): cv.string,
    vol.Optional("delay", default=1.0): vol.All(vol.Coerce(float), vol.Range(min=0.2, max=10)),
    vol.Optional("reply_wait", default=3.0): vol.All(vol.Coerce(float), vol.Range(min=1, max=15)),
    vol.Optional("probe", default="get_nicks"): vol.In(["get_nicks", "get_init_status"]),
    vol.Optional("sip_timeout", default=8.0): vol.All(vol.Coerce(float), vol.Range(min=2, max=15)),
    vol.Optional("apply", default=False): cv.boolean,
    vol.Optional("apply_sga", default=False): cv.boolean,
})
OPEN_DOOR_SCHEMA = vol.Schema({
    vol.Optional("target"): _sip_id,  # vuoto: runtime.DOOR_TARGET (targa dell'attuatore porta, altrimenti SGA)
    # Solo comandi di apertura (OPEN, OPEN_2F, ...): il servizio è aperto a ogni
    # utente, i MESSAGE liberi no.
    vol.Optional("command", default="OPEN_2F"): vol.All(cv.string, vol.Match(r"^OPEN(_[A-Z0-9_]{1,16})?\Z")),
})

def _entry_data(hass: HomeAssistant) -> dict:
    """Dati dell'entry attiva. Le view HTTP restano registrate anche dopo aver
    tolto e riaggiunto l'integrazione (entry_id nuovo), e la prima registrata
    vince: legate al vecchio entry_id rispondevano 503 fino al riavvio di HA."""
    return next(iter(hass.data.get(DOMAIN, {}).values()), {})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vimar Intercom from a config entry."""
    # Identità dispositivo: una per installazione, generata al primo avvio e
    # salvata nell'entry. Fino alla 1.0.1 era una costante in const.py uguale per
    # tutti: sul cloud Vimar la registrazione (e le push) sono associate
    # all'identità, quindi due impianti con lo stesso valore si scalzano a
    # vicenda. Gli entry già esistenti vengono migrati qui, in silenzio.
    if not entry.data.get("device_imei") or not entry.data.get("device_uuid"):
        identity = runtime.new_device_identity()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **identity})
        _LOGGER.info("Identità dispositivo generata per questa installazione")

    # Popola il modulo runtime con i dati del config entry.
    # Le options (impostazioni rete modificate da OptionsFlow) sovrascrivono
    # i valori di default presenti in entry.data.
    runtime.configure({**entry.data, **entry.options})
    _LOGGER.info(
        "Runtime configurato: user=%s domain=%s local_proxy=%s udp=%s",
        runtime.SIP_USER, runtime.SIP_DOMAIN,
        runtime.LOCAL_PROXY, entry.data.get("use_local_udp", True),
    )

    away_tts.setup(hass)  # sintetizza il messaggio di assenza da testo, se configurato

    hub = VimarIntercomHub()

    # Insieme di WS audio attivi: vive in hass.data per evitare globals
    # a livello di modulo (sicuro con reload e multi-entry).
    audio_ws_clients: set[web.WebSocketResponse] = set()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"hub": hub, "audio_ws_clients": audio_ws_clients}

    @callback
    def _on_model_detected(model: str, fw: str, ua: str, priority: int) -> None:
        """Propaga al device registry il modello rilevato via SIP."""
        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        if device:
            updates: dict[str, str] = {}
            if device.model != model:
                updates["model"] = model
            if fw and device.sw_version != fw:
                updates["sw_version"] = fw
            if updates:
                registry.async_update_device(device.id, **updates)
                _LOGGER.info("Device registry aggiornato: %s", updates)

        # Persisti nel config entry: al prossimo avvio il modello è noto subito
        stored = {
            "detected_model":    model,
            "detected_fw":       fw,
            "detected_ua":       ua,
            "detected_priority": priority,
        }
        if any(entry.data.get(k) != v for k, v in stored.items()):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, **stored})

    hub.register_model_callback(_on_model_detected)

    @callback
    def _on_hub_event(event_type: str, data: dict) -> None:
        """Propaga gli eventi in ingresso del citofono sul bus di HA.

        event_type è già uno dei vimar_intercom_* di const.EVENT_*.
        Payload documentato in README §Eventi.
        """
        try:
            hass.bus.async_fire(event_type, data or {})
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Fire bus event %s failed", event_type)

    hub.register_event_callback(_on_hub_event)

    try:
        # Ultimi SPS/PPS della targa (pochi byte): .storage/vimar_intercom.<entry>.sps_pps
        await hub.async_start(Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.sps_pps"))
    except OSError as e:
        # Cloud irraggiungibile (dopo un blackout HA riparte prima del router):
        # HA riprova da solo. Un'eccezione qualsiasi lasciava l'entry in errore
        # fino al reload a mano, e le porte RTP occupate dal tentativo fallito.
        await hub.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise ConfigEntryNotReady(f"Proxy SIP non raggiungibile: {e}") from e

    # Closures locali: catturano audio_ws_clients (nessun global di modulo).
    async def _ws_send_bytes(data: bytes):
        dead = set()
        for ws in list(audio_ws_clients):  # copia: il set cambia durante gli await
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.add(ws)
        audio_ws_clients.difference_update(dead)

    async def _broadcast(data: dict):
        text = json.dumps(data)
        dead = set()
        for ws in list(audio_ws_clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        audio_ws_clients.difference_update(dead)

    # Wire up audio broadcast to WebSocket clients
    media.ws_send_bytes = _ws_send_bytes
    hub.set_ws_broadcast(_broadcast)
    hub._has_ws_clients = lambda: len(audio_ws_clients) > 0

    # Initialize APNs VoIP push sender
    from .const import APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID, APNS_BUNDLE_ID, APNS_SANDBOX
    if APNS_KEY_ID and APNS_TEAM_ID:
        push_sender.init(APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID, APNS_BUNDLE_ID, APNS_SANDBOX)
        _LOGGER.info("APNs VoIP push sender initialized")
    else:
        _LOGGER.warning("APNs push not configured — set APNS_KEY_ID and APNS_TEAM_ID in const.py")

    _register_services(hass)

    hass.http.register_view(VimarAVStreamView(hass))
    hass.http.register_view(VimarAudioWSView(hass))
    await _register_card(hass)
    hass.http.register_view(VimarPushTokenView())
    hass.http.register_view(VimarDebugView())
    hass.http.register_view(VimarRingsView(hass))
    hass.http.register_view(VimarRingPhotoView(hass))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Ricarica l'entry quando cambiano le options (es. lista attuatori):
    # così i bottoni dinamici vengono ricreati con la nuova configurazione.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ricarica l'integrazione al salvataggio delle options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        # Chiudi tutti i WS audio attivi prima di fermare l'hub:
        # evita che le views (ancora registrate in HA) usino il vecchio hub
        # e che i client rimangano connessi a un hub non più valido.
        for ws in list(data.get("audio_ws_clients", set())):
            await ws.close()
        await data["hub"].async_stop()
        if not hass.data[DOMAIN]:
            for svc in (SERVICE_SEND_COMMAND, SERVICE_CALL, SERVICE_ANSWER,
                        SERVICE_HANGUP, SERVICE_OPEN_DOOR, SERVICE_FETCH_LOCAL,
                        SERVICE_SIMULATE_RING, SERVICE_FIND_SGA):
                hass.services.async_remove(DOMAIN, svc)
    return ok


# Header che indicano un hop di proxy davanti a noi.
_FORWARDED_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def _is_local_request(request) -> bool:
    """True se la richiesta arriva da rete locale/loopback.

    Blocca l'accesso agli stream video da Internet (es. remote UI / port
    forwarding). I consumatori legittimi (camera HA, HomeKit) girano sull'host
    HA stesso, quindi vedono IP loopback o privato.

    Dietro un reverse proxy (add-on NGINX, Cloudflare tunnel, Remote UI)
    `request.remote` e' l'indirizzo del proxy, non del chiamante: fino alla
    1.0.5 questo rendeva "locale" tutto Internet. Un hop dichiarato e' quindi
    motivo sufficiente per rifiutare, perche' i consumatori legittimi di questi
    endpoint parlano con Home Assistant in diretta e non ne dichiarano mai.
    """
    for h in _FORWARDED_HEADERS:
        if h in request.headers:
            _LOGGER.warning(
                "Richiesta a %s rifiutata: arriva da un proxy (%s), quindi "
                "l'indirizzo del chiamante non e' verificabile",
                getattr(request, "path", "?"), h)
            return False

    peer = getattr(request, "remote", None)
    if not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _register_services(hass: HomeAssistant) -> None:
    """Registra i servizi vimar_intercom.* (una sola volta)."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND):
        return

    async def _svc_send_command(call: ServiceCall):
        # MESSAGE arbitrari verso qualunque target: roba da amministratore
        # (async_register_admin_service: le automazioni passano, un utente non admin no).
        hub = _entry_data(hass)["hub"]
        ok, msg = await hub.async_send_command(
            body=call.data["body"],
            target=call.data.get("target"),
            header_name=call.data.get("header_name") or None,
            header_value=call.data.get("header_value") or None,
        )
        _LOGGER.info("Service send_command → ok=%s msg=%s", ok, msg)
        return {"ok": ok, "result": msg}

    async def _svc_call(call: ServiceCall):
        hub = _entry_data(hass)["hub"]
        ok, msg = await hub.async_call(target=call.data.get("target"))
        return {"ok": ok, "result": msg}

    async def _svc_answer(call: ServiceCall):
        hub = _entry_data(hass)["hub"]
        ok, msg = await hub.async_answer()
        return {"ok": ok, "result": msg}

    async def _svc_hangup(call: ServiceCall):
        hub = _entry_data(hass)["hub"]
        await hub.async_hangup()
        return {"ok": True, "result": "Chiamata terminata"}

    async def _svc_simulate_ring(call: ServiceCall):
        _entry_data(hass)["hub"].fire_ring_callbacks()

    async def _svc_find_sga(call: ServiceCall):
        """Cerca il PICG interrogando gli indirizzi indicati (issue #14).

        Non scrive nulla nella configurazione, salvo `apply: true` e un PICG
        trovato; `apply_sga: true` aggiorna anche sga_target (sugli impianti visti
        finora i due coincidono, ma sono due valori distinti). Solo admin: manda
        MESSAGE a una serie di indirizzi e può cambiare la configurazione.
        """
        try:
            targets = validate.scan_targets(
                call.data.get("start"), call.data.get("end"), call.data.get("targets"))
        except ValueError as err:
            return {"ok": False, "error": str(err)}
        hub = _entry_data(hass).get("hub")
        if hub is None:
            return {"ok": False, "error": "Integrazione non caricata"}
        result = await hub.async_find_picg(
            targets,
            probe=call.data["probe"].upper(),
            reply_wait=call.data["reply_wait"],
            delay=call.data["delay"],
            sip_timeout=call.data["sip_timeout"],
        )
        result["scanned"] = len(result.get("probes") or [])
        result["applied"] = {}
        picg = result.get("picg")
        if result.get("ok") and picg and (call.data["apply"] or call.data["apply_sga"]):
            if not validate.sip_target(picg):
                result["error"] = f"PICG dichiarato non valido: {picg!r}"
                return result
            entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
            if entry is not None:
                changes = {}
                if call.data["apply"]:
                    changes["picg_target"] = picg
                if call.data["apply_sga"]:
                    changes["sga_target"] = picg
                result["applied"] = changes
                _LOGGER.warning("find_sga: configurazione aggiornata %s", changes)
                # L'update listener ricarica l'entry: runtime riparte coi valori nuovi.
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, **changes})
        return result

    async def _svc_fetch_local(call: ServiceCall):
        """GET HTTP (Digest sipID/password) verso l'interfaccia locale del citofono.

        Replica ciò che fa l'app in "home mode": /rest/get_info.php?action=status|nickname,
        /rest/get_file.php?name=rubrica|mailbox. Restituisce stato+anteprima e può salvare il file.
        """
        # Porta la password SIP in Digest verso un host della LAN: solo admin
        # (async_register_admin_service).
        import requests as _rq
        from requests.auth import HTTPDigestAuth

        # Il citofono e' in LAN, e questa richiesta porta la password SIP in
        # Digest. Un host arbitrario significava farsi mandare quelle credenziali
        # da un server scelto dal chiamante: si accettano solo indirizzi IP
        # privati o di loopback, scritti per esteso.
        host = call.data.get("host") or runtime.LOCAL_PROXY
        if not validate.is_private_host(host):
            _LOGGER.error("fetch_local: host %r rifiutato (serve un IP privato)", host)
            return {"ok": False, "error": f"host non consentito: {host}"}
        scheme = validate.http_scheme(call.data.get("scheme"), "http")
        url = f"{scheme}://{host}/{call.data['path'].lstrip('/')}"

        # `save_as` finiva in hass.config.path() cosi' com'era: un "../"
        # scriveva ovunque sotto l'utente di Home Assistant. Ora e' solo un nome
        # di file, e il file nasce in una sottocartella dedicata.
        raw_name = call.data.get("save_as")
        save_as = validate.safe_filename(raw_name) if raw_name else None
        if raw_name and not save_as:
            _LOGGER.error("fetch_local: save_as %r rifiutato (nome file non valido)", raw_name)
            return {"ok": False, "error": f"nome file non valido: {raw_name}"}
        out_dir = hass.config.path(DOMAIN)

        def _do():
            r = _rq.get(url, auth=HTTPDigestAuth(runtime.SIP_USER, runtime.SIP_PASSWORD),
                        timeout=15, headers={"User-Agent": "TOGA/2.4.0"},
                        allow_redirects=False)
            data = r.content
            saved = None
            if save_as:
                os.makedirs(out_dir, exist_ok=True)
                dst = os.path.join(out_dir, save_as)
                with open(dst, "wb") as f:
                    f.write(data)
                saved = dst
            return r.status_code, dict(r.headers), data, saved

        try:
            code, hdrs, data, saved = await hass.async_add_executor_job(_do)
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("fetch_local %s failed: %s", url, e)
            return {"ok": False, "url": url, "error": str(e)}
        preview = data[:600].decode("utf-8", errors="replace")
        _LOGGER.info("fetch_local %s → %s (%d bytes) saved=%s", url, code, len(data), saved)
        return {"ok": 200 <= code < 300, "url": url, "status": code,
                "content_type": hdrs.get("Content-Type"), "size": len(data),
                "saved": saved, "preview": preview}

    async def _svc_open_door(call: ServiceCall):
        hub = _entry_data(hass)["hub"]
        ok, msg = await hub.async_door(
            target=call.data.get("target"), command=call.data.get("command"))
        return {"ok": ok, "result": msg}

    async_register_admin_service(
        hass, DOMAIN, SERVICE_SEND_COMMAND, _svc_send_command,
        schema=SEND_COMMAND_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_CALL, _svc_call,
        schema=CALL_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_ANSWER, _svc_answer,
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_HANGUP, _svc_hangup,
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_OPEN_DOOR, _svc_open_door,
        schema=OPEN_DOOR_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    async_register_admin_service(
        hass, DOMAIN, SERVICE_FETCH_LOCAL, _svc_fetch_local,
        schema=FETCH_LOCAL_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    async_register_admin_service(hass, DOMAIN, SERVICE_SIMULATE_RING, _svc_simulate_ring)
    async_register_admin_service(
        hass, DOMAIN, SERVICE_FIND_SGA, _svc_find_sga,
        schema=FIND_SGA_SCHEMA, supports_response=SupportsResponse.OPTIONAL)


ADMIN_WS_ACTIONS = {"command", "probe", "scan", "register", "reconnect"}


class VimarAudioWSView(HomeAssistantView):
    """WebSocket endpoint for bidirectional audio + intercom control.

    Binary messages:
      Server → Client: 0x01 + PCM16LE (intercom audio, 8kHz mono)
      Server → Client: 0x03 + 00 00 00 01 + NAL H.264 (video, Annex B; la card lo
                       decodifica con WebCodecs, l'app iOS con VideoToolbox)
      Client → Server: 0x02 + PCM16LE (mic audio, 8kHz mono)

    Text messages (JSON):
      Client → Server: {"action": "call"|"hangup"|"door"|"register"|"status"}
      Server → Client: {"type": "state"|"call_started"|"call_ended"|"ring"|"door"|"error", ...}
    """

    url = "/api/vimar_intercom/audio_ws"
    name = "api:vimar_intercom:audio_ws"
    # HARDENING: richiede autenticazione HA. Le azioni di controllo (door, call,
    # ecc.) non sono più raggiungibili senza un token valido.
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    @property
    def _hub(self) -> "VimarIntercomHub | None":
        """Risolve l'hub dalla entry attiva (sicuro con reload)."""
        return _entry_data(self._hass).get("hub")

    @property
    def _ws_clients(self) -> "set[web.WebSocketResponse]":
        """Risolve il set di WS client attivi dalla entry attiva."""
        return _entry_data(self._hass).get("audio_ws_clients", set())

    async def _broadcast(self, msg: dict) -> None:
        """Manda un messaggio JSON a tutti i WS client attivi."""
        text = json.dumps(msg)
        clients = self._ws_clients
        dead = set()
        for ws in list(clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        clients.difference_update(dead)

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hub = self._hub
        if hub is None:
            await ws.close(code=1011, message=b"Integration not loaded")
            return ws
        user = request.get("hass_user")
        is_admin = user is not None and user.is_admin
        clients = self._ws_clients
        clients.add(ws)
        _LOGGER.info("Audio WS client connected (%d total)", len(clients))
        # Video già in corso (squillo, chiamata): il GOP corrente subito, senza
        # aspettare il prossimo IDR. Anche per chi non è admin: è la vista della card.
        if media.video_proto:
            media.video_proto.replay_gop_ws(ws.send_bytes)

        # Send initial state
        await ws.send_str(json.dumps({
            "type": "state",
            "registered": hub.registered,
            "in_call": hub.in_call,
        }))

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_text(ws, msg.data, is_admin)
                elif msg.type == web.WSMsgType.BINARY:
                    # Client sending mic audio: 0x02 prefix + PCM16LE
                    if len(msg.data) > 1 and msg.data[0] == 0x02 and hub.in_call:
                        hub.claim_call()
                        media.send_audio(msg.data[1:])
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        except Exception as e:
            _LOGGER.error("Audio WS error: %s", e)
        finally:
            clients.discard(ws)
            _LOGGER.info("Audio WS client disconnected (%d remaining)", len(clients))

        return ws

    async def _handle_text(self, ws: web.WebSocketResponse, text: str, is_admin: bool):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return

        action = data.get("action")
        _LOGGER.debug("WS action received: %s (data=%s)", action, data)
        if action in ADMIN_WS_ACTIONS and not is_admin:
            # SIP MESSAGE arbitrari, scansioni e registrazione: roba da amministratore.
            await ws.send_str(json.dumps({"type": "error", "msg": "Admin only"}))
            return
        hub = self._hub
        if hub is None:
            await ws.send_str(json.dumps({"type": "error", "msg": "Integration not loaded"}))
            return

        if action == "status":
            await ws.send_str(json.dumps({
                "type": "state",
                "registered": hub.registered,
                "in_call": hub.in_call,
            }))

        elif action == "call":
            target = data.get("target")  # optional: "55002" etc.
            try:
                ok, m = await hub.async_call(target=target)
                if ok:
                    await self._broadcast({"type": "call_started", "msg": m,
                                           "target": target,
                                           "registered": hub.registered, "in_call": True})
                elif sip.in_call:
                    # Already connected — tell the app immediately
                    _LOGGER.info("Call request: already in call, notifying client")
                    await self._broadcast({"type": "call_started", "msg": "Already in call",
                                           "target": target,
                                           "registered": hub.registered, "in_call": True})
                elif sip.calling:
                    # Call in progress (connecting) — SIP broadcast will notify when connected
                    _LOGGER.info("Call request: already calling, will notify on connect")
                else:
                    await ws.send_str(json.dumps({"type": "error", "msg": m}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "hangup":
            try:
                await hub.async_hangup()
                await self._broadcast({"type": "call_ended", "msg": "Call ended",
                                       "registered": hub.registered, "in_call": False})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "switch":
            # Atomic panel switch: BYE current + INVITE new (like official app)
            target = data.get("target")
            if not target:
                await ws.send_str(json.dumps({"type": "error", "msg": "No target"}))
            else:
                try:
                    with sip.silenced():  # il BYE del cambio targa non è una fine chiamata
                        await hub.async_hangup()
                    await asyncio.sleep(0.05)  # Minimal — just enough for BYE to send
                    ok, m = await hub.async_call(target=target)
                    if ok:
                        await self._broadcast({"type": "call_started", "msg": m,
                                               "target": target,
                                               "registered": hub.registered, "in_call": True})
                    else:
                        await self._broadcast({"type": "call_ended", "msg": f"Switch failed: {m}",
                                               "registered": hub.registered, "in_call": False})
                except Exception as e:
                    await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "door":
            target = data.get("target")  # "55001" (esterno) or "55002" (interno)
            _LOGGER.info("Door action: target=%s", target)
            try:
                ok, m = await hub.async_door(target=target)
                t = "door" if ok else "error"
                await self._broadcast({"type": t, "msg": m})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "command":
            # Comando SIP MESSAGE arbitrario: {"action":"command","body":"...","target":"55001"}
            try:
                ok, m = await hub.async_send_command(
                    body=data.get("body", ""),
                    target=data.get("target"),  # default: SGA, in hub.async_send_command
                    header_name=data.get("header_name", "Panda"),
                    header_value=data.get("header_value", "command"),
                )
                await ws.send_str(json.dumps({"type": "command_result", "ok": ok, "msg": m,
                                              "body": data.get("body")}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "register":
            try:
                ok = await sip.do_register()
                if ok:
                    await self._broadcast({"type": "registered", "msg": "SIP registered",
                                           "registered": True, "in_call": hub.in_call})
                else:
                    await ws.send_str(json.dumps({"type": "error", "msg": "Registration failed"}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "probe":
            target = data.get("target", "")
            try:
                ok, m = await hub.async_probe(target)
                await ws.send_str(json.dumps({"type": "probe_result",
                                               "target": target, "ok": ok, "msg": m}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "scan":
            start = data.get("start", 55001)
            end = data.get("end", 55020)
            try:
                results = await hub.async_scan(start, end)
                await ws.send_str(json.dumps({"type": "scan_result", "results": results}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "answer":
            try:
                ok, m = await hub.async_answer()
                if ok:
                    # Broadcast ring_ended FIRST so other devices stop ringing
                    await self._broadcast({"type": "ring_ended", "msg": "Answered on another device",
                                           "registered": hub.registered, "in_call": True})
                    await self._broadcast({"type": "call_started", "msg": m,
                                           "registered": hub.registered, "in_call": True})
                else:
                    await ws.send_str(json.dumps({"type": "error", "msg": m}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "decline":
            try:
                await hub.async_decline()
                await self._broadcast({"type": "ring_ended", "msg": "Declined",
                                       "registered": hub.registered, "in_call": False})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "reconnect":
            _LOGGER.info("Force reconnect requested via WS")
            try:
                ok = await sip.reconnect()
                await ws.send_str(json.dumps({
                    "type": "state",
                    "registered": hub.registered,
                    "in_call": hub.in_call,
                    "msg": "Reconnected" if ok else "Reconnect failed",
                }))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))


class VimarPushTokenView(HomeAssistantView):
    """REST endpoint for iOS app to register/unregister VoIP push tokens."""

    url = "/api/vimar_intercom/push_token"
    name = "api:vimar_intercom:push_token"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        token = data.get("token")
        if not token:
            return web.json_response({"error": "Missing token"}, status=400)

        sender = push_sender.get_sender()
        if not sender:
            return web.json_response({"error": "Push not configured"}, status=503)

        device_name = data.get("device_name", "unknown")
        sender.register_token(token, device_name)
        return web.json_response({"status": "ok", "devices": len(sender.registered_devices)})

    async def delete(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        token = data.get("token")
        if not token:
            return web.json_response({"error": "Missing token"}, status=400)

        sender = push_sender.get_sender()
        if sender:
            sender.unregister_token(token)
        return web.json_response({"status": "ok"})


class VimarDebugView(HomeAssistantView):
    """Debug endpoint — returns recent vimar_intercom logs as plain text."""

    url = "/api/vimar_intercom/debug"
    name = "api:vimar_intercom:debug"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        # `requires_auth` da solo lascia leggere il log a qualunque utente
        # di Home Assistant, ospiti compresi. Qui dentro passa la traccia
        # SIP dell'impianto: e' materiale da amministratore.
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            raise Unauthorized()
        try:
            n = int(request.query.get("lines", "100"))
        except ValueError:
            n = 100
        text = "\n".join(_debug_log[-n:])
        return web.Response(text=text, content_type="text/plain")


class VimarRingsView(HomeAssistantView):
    """Ultimi squilli (registro accanto alle foto), dal più recente. Per la card.
    Senza cartella foto nelle opzioni: lista vuota."""

    url = "/api/vimar_intercom/rings"
    name = "api:vimar_intercom:rings"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        if not runtime.SNAPSHOT_DIR:
            return web.json_response([])
        try:  # ?limit=: 10 se manca o non è un numero, fra 1 e 50
            limit = max(1, min(50, int(request.query.get("limit") or 10)))
        except ValueError:
            limit = 10
        rings = await self._hass.async_add_executor_job(
            ring_log.recent_rings, runtime.SNAPSHOT_DIR, limit)
        return web.json_response(rings)


class VimarRingPhotoView(HomeAssistantView):
    """Foto di uno squillo. Solo squillo_AAAAMMGG_HHMMSS.jpg dentro la cartella foto:
    nessun altro file è raggiungibile. La card la carica con un percorso firmato."""

    url = "/api/vimar_intercom/rings/{name}"
    name = "api:vimar_intercom:ring_photo"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    async def get(self, request: web.Request, name: str) -> web.StreamResponse:
        path = await self._hass.async_add_executor_job(
            ring_log.ring_photo_path, runtime.SNAPSHOT_DIR, name)
        if path is None:
            return web.Response(status=404)
        return web.FileResponse(path)


class VimarAVStreamView(HomeAssistantView):
    """Serve MPEG-TS stream (H264 video + PCMU audio) at /api/vimar_intercom/av."""

    url = "/api/vimar_intercom/av"
    name = "api:vimar_intercom:av"
    requires_auth = False

    def __init__(self, hass: HomeAssistant):
        self._hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        if not _is_local_request(request):
            return web.Response(status=403, text="Forbidden (local network only)")
        hub = _entry_data(self._hass).get("hub")
        if hub is None:
            return web.Response(status=503, text="Integration not loaded")
        _LOGGER.info("AV stream requested")
        wait = await hub.stream_opened()
        queue = None
        response = web.StreamResponse()
        response.content_type = "video/mp2t"
        try:  # tutto dentro: se il client se ne va prima, lo spettatore va comunque tolto
            if not wait:
                return web.Response(status=503, text="No call")
            waited = 0
            # 25 s: col cloud Vimar la chiamata a volte parte dopo ~15 s (riconnessione TLS).
            # Ma una chiamata finita, annullata o rifiutata (486) non darà video: 503
            # subito, non 25 s di rotella sull'iPhone (contando come spettatore).
            while not hub.video_active and waited < 25 and hub.call_pending:
                await asyncio.sleep(0.1)  # ogni decimo conta: la targa chiude dopo ~10 s
                waited += 0.1
            if not hub.video_active:
                _LOGGER.warning("AV stream: call not established after 25s")
                return web.Response(status=503, text="Call not established")

            queue = await av_stream.av_subscribe()
            if queue is None:
                return web.Response(status=503, text="ffmpeg failed to start")
            # Primo fotogramma subito, non al prossimo IDR. Non attesa: l'INFO SIP
            # (con eventuale 407) può durare secondi e ritarderebbe gli header.
            self._hass.async_create_background_task(
                sip.send_keyframe_request(), "vimar_intercom keyframe")

            await response.prepare(request)
            while (chunk := await queue.get()) is not None:
                await response.write(chunk)
        except ConnectionResetError:
            pass
        finally:
            if queue is not None:
                await av_stream.av_unsubscribe(queue)
            await hub.stream_closed()
        return response
