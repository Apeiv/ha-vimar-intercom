"""Vimar Intercom Hub — manages SIP + media lifecycle."""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from . import sip_client as sip
from . import frame_grabber
from . import media_handler as media
from . import push_sender
from . import ring_log
from . import const as C
from . import runtime as R
from . import validate

_LOGGER = logging.getLogger(__name__)

STREAM_HANGUP_DELAY = 30
# Niente auto-call per 60 s dopo la fine di una chiamata (qualsiasi, anche
# rifiutata o annullata), di un auto-call fallito o di uno squillo, ma solo per
# le riaperture «di riflesso»: go2rtc e lo stream worker riaprono /av da soli
# appena lo stream finisce (entro QUICK_REOPEN_S dall'uscita dell'ultimo
# spettatore), e ogni riapertura richiamava la targa (486, poi un altro
# tentativo). Nel frattempo /av risponde subito 503. Chi apre la camera dopo
# (dashboard, HomeKit, anche entro il minuto) chiama: non è una riconnessione.
AUTO_CALL_COOLDOWN = 60
QUICK_REOPEN_S = 5

# Nomi "umani" degli indirizzi SIP dell'impianto
SIP_ID_NAMES = {
    "55001": "Targa Esterna",
    "55002": "Targa Interna",
    "60001": "Monitor Interno",
}


def sip_uri(target) -> str:
    """URI SIP per un id dell'impianto. Chiamate, porta, pulsanti e attuatori
    passano tutti da qui: un id non numerico (CR/LF, `x@altro.dominio`) non
    arriva mai nella request line."""
    t = validate.sip_target(target)
    if t is None:
        raise ValueError(f"target SIP non valido: {target!r}")
    return f"sip:{t}@{R.SIP_DOMAIN}"


def _uri_to_id(uri: str | None) -> str | None:
    """'sip:55001@dominio' → '55001'."""
    if not uri:
        return None
    u = uri.replace("sip:", "").replace("sips:", "")
    return u.split("@")[0].split(";")[0] or None


def sip_id_name(sip_id: str | None) -> str | None:
    if not sip_id:
        return None
    return SIP_ID_NAMES.get(sip_id, sip_id)
MAX_CALL_DURATION = 300  # 5 minutes — auto-hangup safety net

# Interni interrogati con un OPTIONS all'avvio per farsi identificare dal
# citofono quando il modello non è ancora noto (OPTIONS è innocuo: è lo stesso
# messaggio già usato come keepalive).
MODEL_PROBE_TARGETS = ("55001", "55002", "60001")


class VimarIntercomHub:
    """Orchestrates SIP registration, calls, door control, and media."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._ring_callbacks: list[Callable] = []
        self._video_end_callbacks: list[Callable] = []  # video finito: la camera ferma lo stream di HA
        self._state_callbacks: list[Callable] = []
        self._model_callbacks: list[Callable] = []
        self._ws_broadcast_fn: Callable | None = None
        self._has_ws_clients: Callable | None = None
        self._stream_viewers = 0
        self._hangup_task: asyncio.Task | None = None
        self._away_task: asyncio.Task | None = None
        self._auto_ended_at = -1e9  # monotonic: fine dell'ultima chiamata
        self._viewers_left_at = -1e9  # monotonic: l'ultimo spettatore di /av se n'è andato
        self._was_busy = False      # in_call or calling, all'ultimo cambio di stato
        self._photo_task: asyncio.Task | None = None
        self._ring_time: str | None = None  # chiave dell'ultimo squillo nel registro
        self._call_timeout_task: asyncio.Task | None = None
        self._keyframe_task: asyncio.Task | None = None
        self._keyframe_now: asyncio.Task | None = None
        media.request_keyframe = self._request_keyframe  # pacchetto video perso
        self._auto_called = False

        # ─── Statistiche / stato esteso (esposte da sensor.py) ───────────
        self.stats: dict = {
            "last_ring_time": None,        # datetime UTC ultimo squillo
            "last_caller_id": None,        # es. "55001"
            "last_caller_uri": None,       # es. "sip:55001@dominio"
            "ring_count": 0,               # squilli dall'avvio
            "missed_count": 0,             # squilli non risposti da HA
            "last_call_start": None,
            "last_call_end": None,
            "last_call_duration": None,    # secondi
            "last_call_direction": None,   # "in" | "out"
            "call_count": 0,               # chiamate attive dall'avvio
            "last_door_time": None,
            "last_door_target": None,
            "last_door_result": None,
            "door_count": 0,
            "last_register_time": None,
            "register_failures": 0,
            "last_command_time": None,
            "last_command_body": None,
            "last_command_target": None,
            "last_command_result": None,
            "last_error": None,
            "last_error_time": None,
            "voicemail": None,             # stato segreteria annunciato dal Tab (True/False)
            "dnd": None,                   # stato Non disturbare annunciato dal Tab
            "vm_level": None,              # spazio segreteria, es. "0/100"
            "rubrica_ver": None,           # versione (md5) della rubrica del Tab
            "vm_ver": None,                # versione (md5) del db videomessaggi
            "init_status": {},             # ultimo GET_INIT_STATUS_REPLY grezzo {PARAM: VALUE}
            "last_message_in": None,       # ultimo SIP MESSAGE ricevuto dal citofono
            "last_message_in_time": None,
            # ─── Eventi in ingresso (PROTOCOL.md §4) ─────────────────────────
            "last_missed_call": None,      # dict {sip_id, ts, name}
            "missed_call_count": 0,        # MISSED_CALL ricevuti dall'avvio
            "new_videomessage": False,     # ON su VM;VIDEO_MESSAGE_CHANGE;NEW
            "last_videomessage": None,     # ultimo change grezzo
            "last_fuoriporta": None,       # dict {sip_id, msg}
            "last_call_info": None,        # dict {sip_id, reason, media_type, video_src}
            "started_at": datetime.now(timezone.utc),
        }
        self._call_started_mono: float | None = None
        self._ring_answered = False
        # Callback per emettere eventi bus HA (registrati da __init__.py).
        # Evita di iniettare hass nell'hub, coerente con ring/state callbacks.
        self._event_callbacks: list[Callable] = []
        self._init_status_sent = False
        # SET_APT_PARAMS in attesa della risposta, per MSGID (PROTOCOL §3: l'unico
        # comando che correla la risposta per ID).
        self._apt_param_waiters: dict[str, asyncio.Future] = {}

    # ─── helpers stato esteso ────────────────────────────────────────────
    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _touch(self):
        """Notifica le entità HA che le statistiche sono cambiate."""
        for cb in list(self._state_callbacks):  # un callback può togliersi (select.py)
            try:
                cb()
            except Exception:
                _LOGGER.exception("State callback error")

    @property
    def calling(self) -> bool:
        return sip.calling

    @property
    def _busy_now(self) -> bool:
        """Una chiamata nostra in corso o in partenza."""
        return sip.in_call or sip.calling

    @property
    def local_ip(self) -> str | None:
        return sip.MY_IP

    @property
    def transport(self) -> str:
        return "udp-local" if R.USE_LOCAL_UDP else "tls-cloud"

    @property
    def proxy(self) -> str:
        return R.LOCAL_PROXY if R.USE_LOCAL_UDP else R.SIP_PROXY

    @property
    def sip_user(self) -> str:
        return R.SIP_USER

    @property
    def sip_domain(self) -> str:
        return R.SIP_DOMAIN

    @property
    def voicemail(self) -> bool | None:
        """Stato segreteria annunciato dal Tab (None finché sconosciuto)."""
        return self.stats.get("voicemail")

    @property
    def dnd(self) -> bool | None:
        """Stato Non disturbare annunciato dal Tab (None finché sconosciuto)."""
        return self.stats.get("dnd")

    @property
    def status(self) -> str:
        """Stato sintetico: offline / ringing / in_call / calling / idle."""
        if not sip.registered:
            return "offline"
        # In chiamata prima dello squillo: un INVITE che arriva a chiamata in corso
        # (l'eco della nostra) non deve trasformare "Microfono" in "Rispondi".
        if sip.in_call:
            return "in_call"
        if sip.ringing():
            return "ringing"
        if sip.calling:
            return "calling"
        return "idle"

    def set_ws_broadcast(self, fn: Callable):
        self._ws_broadcast_fn = fn

    @property
    def registered(self) -> bool:
        return sip.registered

    @property
    def in_call(self) -> bool:
        return sip.in_call

    @property
    def video_active(self) -> bool:
        """Il video arriva: chiamata attiva o anteprima dello squillo (early media)."""
        return sip.in_call or sip.early_media()

    @property
    def call_pending(self) -> bool:
        """Il video può ancora arrivare: chiamata attiva o in partenza (anche un
        auto-call), squillo, o l'app iOS collegata che chiamerà da sé. /av aspetta
        solo in questi casi, altrimenti 503 subito."""
        return bool(self._busy_now or sip.ringing() or self._auto_called
                    or (self._has_ws_clients and self._has_ws_clients()))

    @property
    def is_ringing(self) -> bool:
        return sip.ringing()

    def fire_ring_callbacks(self) -> None:
        """Evento doorbell → automazioni. Da solo è lo squillo di prova
        (servizio simulate_ring): niente SIP, push, WebSocket né statistiche."""
        for cb in self._ring_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Ring callback error")

    def register_video_end_callback(self, callback: Callable) -> Callable[[], None]:
        """Chiamata o squillo finiti: non arriva più video. Restituisce l'annullamento."""
        self._video_end_callbacks.append(callback)
        return lambda: self._video_end_callbacks.remove(callback)

    def _video_ended(self) -> None:
        for cb in self._video_end_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Video callback error")

    def register_ring_callback(self, callback: Callable) -> None:
        self._ring_callbacks.append(callback)

    def unregister_ring_callback(self, callback: Callable) -> None:
        if callback in self._ring_callbacks:
            self._ring_callbacks.remove(callback)

    def register_state_callback(self, callback: Callable) -> None:
        """Register a callback for SIP state changes (registered, in_call)."""
        self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: Callable) -> None:
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    def register_event_callback(self, callback: Callable) -> None:
        """Registra un callback(event_type: str, data: dict) per gli eventi
        in ingresso da esporre sul bus HA (missed_call, videomessage, ...)."""
        self._event_callbacks.append(callback)

    def unregister_event_callback(self, callback: Callable) -> None:
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    def _fire_event(self, event_type: str, data: dict) -> None:
        """Propaga un evento in ingresso ai callback registrati (bus HA)."""
        for cb in self._event_callbacks:
            try:
                cb(event_type, data)
            except Exception:
                _LOGGER.exception("Event callback error (%s)", event_type)

    @property
    def detected_model(self) -> str:
        """Modello rilevato via SIP (stringa vuota se ancora sconosciuto)."""
        return R.DETECTED_MODEL

    def register_model_callback(self, callback: Callable) -> None:
        """Callback(model, fw, user_agent, priority) sul rilevamento modello."""
        self._model_callbacks.append(callback)

    def _on_model_detected(self, model: str, fw: str, ua: str, priority: int):
        """Chiamata da sip_client quando un peer SIP rivela il modello."""
        for cb in self._model_callbacks:
            try:
                cb(model, fw, ua, priority)
            except Exception:
                _LOGGER.exception("Model callback error")

    async def _probe_model(self):
        """Interroga gli interni con un OPTIONS finché qualcuno si identifica."""
        await asyncio.sleep(3)
        for target in MODEL_PROBE_TARGETS:
            if R.DETECTED_MODEL:
                break
            uri = sip_uri(target)
            try:
                await sip.do_options(target=uri)
            except Exception as e:
                _LOGGER.debug("Model probe %s fallito: %s", target, e)
            await asyncio.sleep(0.5)

        if R.DETECTED_MODEL:
            _LOGGER.info("Modello citofono: %s", R.DETECTED_MODEL)
        else:
            _LOGGER.info(
                "Modello non rilevato — nessun peer SIP si è identificato. "
                "User-Agent visti finora: %s", sorted(sip._seen_uas) or "nessuno")

    def _on_sip_state_change(self):
        """Called by sip_client when registered/in_call changes."""
        # Fine di QUALSIASI chiamata (anche rifiutata, anche fatta da "Vedi esterno"),
        # nell'istante in cui in_call/calling scendono: prima del call_ended, che
        # arriva dopo stop_media, quando go2rtc ha già riaperto /av. Fino alla 1.0.9
        # la pausa valeva solo dopo un auto-call chiuso dalla targa: chiusa una
        # chiamata dalla card, go2rtc riapriva /av e l'hub richiamava da solo
        # (→ 486 dalla targa ancora occupata → un altro auto-call al retry).
        busy = self._busy_now
        if self._was_busy and not busy:
            self._auto_ended_at = time.monotonic()
            self._video_ended()
        self._was_busy = busy
        self._touch()
        # Notify WS clients of state change
        if self._ws_broadcast_fn:
            task = asyncio.create_task(self._ws_broadcast_fn({
                "type": "state",
                "registered": sip.registered,
                "in_call": sip.in_call,
            }))
            task.add_done_callback(
                lambda t: _LOGGER.error("WS state broadcast error: %s", t.exception())
                if not t.cancelled() and t.exception() else None
            )

    async def stream_opened(self) -> bool:
        """Uno spettatore apre /av. False = niente chiamata in vista, inutile aspettare."""
        self._stream_viewers += 1
        _LOGGER.info("Stream opened (%d viewers)", self._stream_viewers)

        if self._hangup_task:
            self._hangup_task.cancel()
            self._hangup_task = None

        # Chiamata in corso, in partenza o in arrivo (vedi call_pending): si aspetta il
        # video, senza chiamare né rispondere da soli (uno stream aperto ruberebbe lo
        # squillo al Tab). Altrimenti, nella pausa AUTO_CALL_COOLDOWN, una riapertura
        # subito dopo l'uscita dell'ultimo spettatore non chiama e riceve 503.
        if self.call_pending:
            return True
        now = time.monotonic()
        if now - self._viewers_left_at < QUICK_REOPEN_S and now - self._auto_ended_at < AUTO_CALL_COOLDOWN:
            _LOGGER.info("Stream riaperto subito dopo la fine del precedente: "
                         "riconnessione, niente auto-call")
            return False

        if sip.registered:
            self._auto_called = True
            # Fire auto-call as background task — don't block the HTTP response
            asyncio.create_task(self._do_auto_call())
            return True
        return False

    async def _do_auto_call(self):
        """Background auto-call when video stream opens without active call."""
        try:
            # Default di do_call: R.INTERCOM, cioè la targa video (camera_target).
            ok, msg = await sip.do_call()
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        if not ok:
            # Anche un fallito che non è mai arrivato a `calling` (es. squillo in
            # corso): i retry di go2rtc non devono richiamare subito.
            _LOGGER.error("Auto-call failed: %s", msg)
            self._auto_called = False
            self._auto_ended_at = time.monotonic()

    async def stream_closed(self):
        self._stream_viewers = max(0, self._stream_viewers - 1)
        _LOGGER.info("Stream viewer disconnected (%d remaining)", self._stream_viewers)
        if self._stream_viewers == 0:
            self._viewers_left_at = time.monotonic()

        # Anche mentre collega (calling): chi apre la camera e la richiude prima
        # della risposta (il cloud ci mette anche 15 s) lasciava la chiamata aperta
        # senza spettatori fino al tetto di 5 minuti. do_hangup allora annulla.
        if self._stream_viewers == 0 and self._auto_called and self._busy_now:
            self._hangup_task = asyncio.create_task(self._delayed_hangup())

    async def _delayed_hangup(self):
        try:
            await asyncio.sleep(STREAM_HANGUP_DELAY)
            if self._stream_viewers == 0 and self._auto_called and self._busy_now:
                _LOGGER.info("No viewers, hanging up auto-call")
                self._auto_called = False
                await sip.do_hangup()
        except asyncio.CancelledError:
            pass

    def _start_call_timeout(self):
        """Start max call duration timer."""
        self._cancel_call_timeout()
        self._call_timeout_task = asyncio.create_task(self._call_timeout())

    def _cancel_call_timeout(self):
        if self._call_timeout_task:
            self._call_timeout_task.cancel()
            self._call_timeout_task = None

    async def _call_timeout(self):
        try:
            await asyncio.sleep(MAX_CALL_DURATION)
            if sip.in_call:
                _LOGGER.info("Max call duration (%ds) reached, hanging up", MAX_CALL_DURATION)
                self._auto_called = False
                await sip.do_hangup()
        except asyncio.CancelledError:
            pass

    def _request_keyframe(self):
        """Pacchetto video perso (media_handler): keyframe subito, non al giro dei 5 s."""
        self._keyframe_now = asyncio.create_task(sip.send_keyframe_request())

    def _start_keyframe_loop(self):
        """Send periodic keyframe requests during calls for video recovery."""
        self._cancel_keyframe_loop()
        self._keyframe_task = asyncio.create_task(self._keyframe_loop())

    def _cancel_keyframe_loop(self):
        if self._keyframe_task:
            self._keyframe_task.cancel()
            self._keyframe_task = None

    async def _keyframe_loop(self):
        """Keyframe burst at start, then slow periodic refresh.

        Il burst serve a ottenere SPS/PPS+IDR appena parte il video. Dopo,
        se il video sta effettivamente arrivando (pkt_count cresce) rallentiamo
        molto: un INFO ogni 2s spammava il proxy (e i 407) senza utilità.
        """
        def _video_flowing():
            vp = media.video_proto
            return bool(vp and vp.pkt_count > 0)

        try:
            # Immediate first request — no delay
            if sip.in_call:
                await sip.send_keyframe_request()
            # Rapid burst: 8 requests at 150ms intervals to grab the first IDR
            for _ in range(8):
                await asyncio.sleep(0.15)
                if not sip.in_call:
                    return
                if _video_flowing():
                    break
                await sip.send_keyframe_request()
            # Then slow refresh: every 5s, only while the call lasts.
            while sip.in_call:
                await asyncio.sleep(5)
                if sip.in_call:
                    await sip.send_keyframe_request()
        except asyncio.CancelledError:
            pass

    async def async_start(self):
        if self._running:
            return

        sip.init(self._handle_broadcast)
        sip.set_state_callback(self._on_sip_state_change)
        sip.set_model_callback(self._on_model_detected)
        media.init(self._handle_broadcast)

        sip.MY_IP = sip.get_local_ip()
        sip.incoming_requests = asyncio.Queue()
        _LOGGER.info("Local IP: %s", sip.MY_IP)

        await media.setup_transports()
        _LOGGER.info("RTP transports ready")

        await sip.connect()

        self._tasks.append(asyncio.create_task(sip.reader_task()))
        self._tasks.append(asyncio.create_task(sip.request_processor()))
        self._tasks.append(asyncio.create_task(self._auto_startup()))
        self._tasks.append(asyncio.create_task(self._keepalive_loop()))
        self._running = True

    async def async_stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self._hangup_task:
            self._hangup_task.cancel()
        sip.cancel_reconnect()  # non deve riconnettere un hub scaricato
        self._cancel_away()
        if self._photo_task:
            self._photo_task.cancel()
        self._cancel_call_timeout()
        self._cancel_keyframe_loop()
        await media.stop_media()
        media.close_transports()
        if sip.writer:
            try:
                sip.writer.close()
            except Exception:
                pass
        if sip._udp_sock:
            try:
                sip._udp_sock.close()
            except Exception:
                pass
        _LOGGER.info("Hub stopped")

    async def async_call(self, target: str | None = None) -> tuple[bool, str]:
        self._auto_called = False
        return await (sip.do_call(target=sip_uri(target)) if target else sip.do_call())

    def claim_call(self) -> None:
        """Qualcuno parla (microfono sul WS audio): la chiamata è sua. Il messaggio
        di assenza non la chiude a fine file, un auto-call non viene riagganciato
        quando lo stream video si chiude."""
        self._cancel_away()
        self._auto_called = False

    async def async_answer(self) -> tuple[bool, str]:
        if self._away_task and not self._away_task.done() and sip.in_call:
            # Sta suonando il messaggio di assenza: "Rispondi" prende la chiamata
            # (annullato, il messaggio non riaggancia).
            self._away_task.cancel()
            ok, msg = True, "Chiamata presa dal messaggio di assenza"
        elif sip.in_call:
            # Un INVITE a chiamata in corso (l'eco della nostra): rispondergli
            # sovrascriverebbe il dialogo attivo e la chiamata vera cadrebbe.
            ok, msg = False, "Già in chiamata"
        else:
            ok, msg = await sip.do_answer_incoming()
        if ok:
            self._ring_answered = True
            self.stats["last_call_direction"] = "in"
            self._log_outcome("answered")
        self._touch()
        return ok, msg

    async def async_decline(self):
        await sip.do_decline_incoming()
        self._touch()


    async def async_hangup(self):
        self._auto_called = False  # chiusa da noi: niente riaggancio automatico
        self._cancel_call_timeout()
        await sip.do_hangup()

    async def async_door(self, target: str | None = None, command: str | None = None) -> tuple[bool, str]:
        """Open door via SIP MESSAGE to targa (PE) address.

        From Tab5S rubrica ACTUATOR_LIST:
          55001 (targa master)  → OPEN_2F = Portone Esterno
          55002 (targa interna) → OPEN_2F = Portone Interno
        The targa forwards the command to its local relay.
        No active call required.
        """
        # Senza target: la targa che apre la porta (R.DOOR_TARGET, dalla
        # rubrica), non l'SGA — su un 2FV2 l'SGA risponde 200 e non apre.
        uri = sip_uri(target) if target else R.DOOR_ESTERNO
        body = command or C.DOOR_COMMAND

        _LOGGER.info("Door command: uri=%s body=%s registered=%s", uri, body, sip.registered)

        ok, msg = await sip.do_system_message(
            uri, body, extra_headers={"Panda": "command"})

        self.stats["last_door_time"] = self._now()
        self.stats["last_door_target"] = target or R.DOOR_TARGET
        self.stats["last_door_result"] = msg
        if ok:
            self.stats["door_count"] += 1
        self._touch()

        if ok:
            _LOGGER.info("Door open OK: %s", msg)
            return ok, msg


        # Retry once after re-registration — handles stale connection
        _LOGGER.warning("Door command failed (%s), retrying after re-register...", msg)
        try:
            reg_ok = await sip.do_register()
            if reg_ok:
                ok2, msg2 = await sip.do_system_message(
                    uri, body, extra_headers={"Panda": "command"})
                self.stats["last_door_result"] = msg2
                if ok2:
                    self.stats["door_count"] += 1
                    self._touch()
                    _LOGGER.info("Door open OK on retry: %s", msg2)
                    return ok2, msg2
                self._touch()
                _LOGGER.error("Door retry also failed: %s", msg2)
                return ok2, msg2
            else:
                _LOGGER.error("Re-registration failed, cannot retry door")
                return False, "Re-registrazione fallita"
        except Exception as e:
            _LOGGER.error("Door retry error: %s", e)
            return False, str(e)

    async def async_send_command(
        self,
        body: str,
        target: str | None = None,
        header_name: str | None = "Panda",
        header_value: str | None = "command",
    ) -> tuple[bool, str]:
        """Invia un SIP MESSAGE arbitrario al citofono (per test / comandi non ancora mappati).

        target può essere un ID (es. "55001") oppure un URI sip: completo.
        """
        target = target or R.SGA_TARGET
        if target.startswith("sip:"):
            uri = target
        else:
            uri = sip_uri(target)
        # Header solo se nome e valore ci sono entrambi: fino alla 1.0.6 un
        # header_value vuoto dal servizio diventava None e partiva «Panda: None».
        # CR/LF vengono rifiutati: finirebbero dentro il messaggio SIP come
        # righe di header aggiuntive.
        name = (header_name or "").strip()
        value = (header_value or "").strip()
        if any(c in name + value for c in "\r\n"):
            return False, "header_name/header_value non possono contenere a capo"
        headers = {name: value} if name and value else None
        _LOGGER.info("Custom command: uri=%s body=%r headers=%s", uri, body, headers)
        try:
            ok, msg = await sip.do_system_message(uri, body, extra_headers=headers)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        self.stats["last_command_time"] = self._now()
        self.stats["last_command_body"] = body
        self.stats["last_command_target"] = target
        self.stats["last_command_result"] = msg
        self._touch()
        return ok, msg

    async def async_probe(self, target: str) -> tuple[bool, str]:
        uri = sip_uri(target)
        return await sip.do_options(target=uri)

    async def async_scan(self, start: int, end: int) -> list[dict]:
        results = []
        for addr in range(start, end + 1):
            uri = sip_uri(addr)
            try:
                ok, msg = await sip.do_options(target=uri)
                results.append({"addr": addr, "ok": ok, "msg": msg})
            except Exception as e:
                results.append({"addr": addr, "ok": False, "msg": str(e)})
            await asyncio.sleep(0.3)
        return results

    async def _handle_broadcast(self, msg_type, msg):
        _LOGGER.debug("[%s] %s", msg_type, msg)
        self._update_stats(msg_type, msg)

        if msg_type in ("ring", "ring_ended", "call_started", "call_ended", "registered", "error"):
            # Don't broadcast "ring" to WS clients if we initiated the call
            if msg_type == "ring" and self._busy_now:
                pass  # Will be handled below (suppress + decline)
            elif self._ws_broadcast_fn:
                try:
                    payload = {
                        "type": msg_type, "msg": msg,
                        "registered": sip.registered, "in_call": sip.in_call,
                    }
                    # Include caller URI so clients can identify which panel is ringing
                    if msg_type == "ring" and sip.pending_incoming.get("caller_uri"):
                        payload["caller_uri"] = sip.pending_incoming["caller_uri"]
                    await self._ws_broadcast_fn(payload)
                except Exception:
                    _LOGGER.exception("WS broadcast error")

        if msg_type == "ring_ended":
            self._cancel_away()  # squillo annullato: niente messaggio per lui
            if not self._busy_now:
                self._video_ended()
            # Finita l'anteprima si chiude /av, e go2rtc lo riapre: non è uno spettatore.
            if self._stream_viewers:
                self._auto_ended_at = time.monotonic()
        elif msg_type == "call_started":
            self._start_call_timeout()
            self._start_keyframe_loop()
        elif msg_type == "call_ended":
            self._cancel_call_timeout()
            self._cancel_keyframe_loop()
            # La chiamata e' chiusa: da qui in poi un INVITE in arrivo e' uno
            # squillo vero, non l'eco della nostra. Senza questo reset il ramo
            # "ring" piu' sotto continuerebbe a rispondere 603 Decline per
            # sempre quando a chiudere e' stato il citofono (il watchdog
            # _delayed_hangup non arriva: la sua guardia richiede sip.in_call).
            self._auto_called = False

        if msg_type == "ring":
            # If we initiated the call (tap to view / auto-call), the Tab5S
            # sends an INVITE back to us. Suppress ring + push — this is NOT
            # a doorbell ring, just the PBX echoing our outgoing call.
            # Non _auto_called: resta vero dal BYE della targa fino al call_ended
            # (dopo stop_media), e un visitatore che suona in quell'istante (la
            # targa chiude la visione e squilla) riceveva 603, che il PBX propaga
            # annullando lo squillo anche sul Tab.
            if self._busy_now:
                _LOGGER.info("Squillo ignorato: è l'eco della nostra chiamata (in_call=%s, calling=%s)",
                             sip.in_call, sip.calling)
                asyncio.create_task(sip.do_decline_incoming())
                return

            self.fire_ring_callbacks()

            # Send VoIP push to wake iOS devices
            sender = push_sender.get_sender()
            if sender:
                caller = _uri_to_id(sip.pending_incoming.get("caller_uri")) or ""
                panel = "esterna"  # TODO: detect panel from caller
                asyncio.create_task(sender.send_voip_push(caller=caller, panel=panel))

            if R.SNAPSHOT_DIR:
                now = datetime.now().astimezone()
                # Millisecondi: due squilli nello stesso secondo (CANCEL e INVITE nuovo)
                # restano due voci, ognuna col suo esito e la sua foto.
                name = now.strftime("squillo_%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}.jpg"
                self._ring_time = now.isoformat(timespec="milliseconds")
                ring = {"time": self._ring_time, "photo": name, "outcome": "missed",
                        "caller": _uri_to_id(sip.pending_incoming.get("caller_uri"))}
                asyncio.create_task(self._ring_log(lambda rings: rings.append(ring)))
                if self._photo_task:
                    self._photo_task.cancel()
                self._photo_task = asyncio.create_task(self._save_ring_photo(name))
            if R.AWAY_MESSAGE_FILE and R.AWAY_MESSAGE_DELAY:
                self._cancel_away()
                self._away_task = asyncio.create_task(
                    self._away_message(sip.pending_incoming["cid"]))

    async def _ring_log(self, change: Callable[[list], None]) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, ring_log.update_ring_log, R.SNAPSHOT_DIR, change)
        except OSError as e:
            _LOGGER.warning("Registro squilli non aggiornato in %s: %s", R.SNAPSHOT_DIR, e)

    def _log_outcome(self, outcome: str) -> None:
        """Esito dell'ultimo squillo nel registro: answered (Rispondi) o away (messaggio)."""
        key = self._ring_time
        if not (R.SNAPSHOT_DIR and key):
            return

        def change(rings):
            for r in rings:
                if r.get("time") == key:
                    r["outcome"] = outcome

        asyncio.create_task(self._ring_log(change))

    async def _save_ring_photo(self, name: str) -> None:
        """Foto di chi ha suonato (anteprima dello squillo) nella cartella delle opzioni.
        Il nome (ora dello squillo) è quello già scritto nel registro."""
        await asyncio.sleep(R.SNAPSHOT_DELAY)
        jpeg = await frame_grabber.wait_frame()
        if not jpeg:
            _LOGGER.warning("Foto squillo: nessuna immagine (anteprima video non arrivata)")
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, ring_log.write_photo, R.SNAPSHOT_DIR, name, jpeg)
        except OSError as e:
            _LOGGER.warning("Foto squillo non salvata in %s: %s", R.SNAPSHOT_DIR, e)

    def _cancel_away(self) -> None:
        if self._away_task and not self._away_task.done():
            self._away_task.cancel()
        self._away_task = None

    async def _away_message(self, ring_cid) -> None:
        """Se dopo AWAY_MESSAGE_DELAY s QUESTO squillo suona ancora (nessuno ha
        risposto: Tab, telefono o HA), risponde, fa sentire il file e riaggancia."""
        await asyncio.sleep(R.AWAY_MESSAGE_DELAY)
        if not sip.ringing(ring_cid) or sip.in_call:
            return
        pcm = await media.load_pcm(R.AWAY_MESSAGE_FILE)
        if not pcm or not sip.ringing(ring_cid):
            return  # file illeggibile: meglio lasciar squillare che rispondere muti
        ok, msg = await sip.do_answer_incoming()
        if not ok:
            _LOGGER.warning("Messaggio di assenza: risposta fallita (%s)", msg)
            return
        self._ring_answered = True
        self._log_outcome("away")

        def alive():
            return sip.in_call and sip.call_state["call_id"] == ring_cid

        await asyncio.sleep(0.5)  # il tempo di aprire il flusso RTP
        await media.send_pcm(pcm, alive)
        if alive():  # solo la nostra chiamata; se annullato ("Rispondi", unload) resta aperta
            await self.async_hangup()

    def _update_stats(self, msg_type: str, msg):
        """Aggiorna le statistiche in base agli eventi SIP."""
        st = self.stats
        now = self._now()
        try:
            if msg_type == "ring":
                # Squillo reale solo se non l'abbiamo originato noi
                if not self._busy_now:
                    caller = sip.pending_incoming.get("caller_uri") or ""
                    st["last_ring_time"] = now
                    st["last_caller_uri"] = caller or None
                    st["last_caller_id"] = _uri_to_id(caller)
                    st["ring_count"] += 1
                    self._ring_answered = False
            elif msg_type == "ring_ended":
                if not self._ring_answered and st["last_ring_time"]:
                    st["missed_count"] += 1
            elif msg_type == "call_started":
                self._call_started_mono = time.monotonic()
                st["last_call_start"] = now
                st["call_count"] += 1
                if not self._ring_answered:
                    st["last_call_direction"] = "out"
            elif msg_type == "call_ended":
                st["last_call_end"] = now
                if self._call_started_mono is not None:
                    st["last_call_duration"] = round(time.monotonic() - self._call_started_mono, 1)
                    self._call_started_mono = None
                self._ring_answered = False
            elif msg_type == "registered":
                st["last_register_time"] = now
            elif msg_type == "error":
                st["last_error"] = str(msg)[:200]
                st["last_error_time"] = now
            elif msg_type == "message":
                st["last_message_in"] = str(msg)[:200]
                st["last_message_in_time"] = now
                self._handle_incoming_message(str(msg))
        except Exception:
            _LOGGER.exception("stats update error")
        self._touch()

    # ─── Parsing dei SIP MESSAGE in ingresso (Panda: blue) ───────────────────
    # Qui si LEGGE soltanto: nessun comando in uscita. Parsing difensivo: alcuni
    # body sono JSON, altri delimitati da ';'. Se non combacia → debug, no crash.
    def _handle_incoming_message(self, body: str) -> None:
        st = self.stats
        raw = (body or "").strip()
        upper = raw.upper()

        # Annunci di stato: "VOICEMAIL;ON|OFF" / "DND;ON|OFF" [VERIFICATO]
        if upper.startswith("VOICEMAIL;"):
            st["voicemail"] = ("ON" in upper and "OFF" not in upper)
            st["mode_seq"] = st.get("mode_seq", 0) + 1
            return
        if upper.startswith("DND;"):
            st["dnd"] = ("ON" in upper and "OFF" not in upper)
            st["mode_seq"] = st.get("mode_seq", 0) + 1
            return

        # SET_APT_PARAMS_REPLY;{"MSGID","ERRCODE"} → risposta a async_set_apt_param
        if upper.startswith("SET_APT_PARAMS_REPLY"):
            self._handle_apt_params_reply(raw)
            return
        # APT_PARAMS_CHANGED;{"PARAM","VALUE"} → un parametro cambiato (da noi, dal
        # Tab o dall'app)
        if upper.startswith("APT_PARAMS_CHANGED"):
            self._handle_apt_params_changed(raw)
            return

        # GET_INIT_STATUS_REPLY;<json array [{PARAM,VALUE}]>
        if upper.startswith("GET_INIT_STATUS_REPLY"):
            self._parse_init_status_reply(raw)
            return

        # MISSED_CALL;{json}  [da confermare sul campo — PROTOCOL.md §4]
        if upper.startswith("MISSED_CALL"):
            self._handle_missed_call(raw)
            return

        # VM;VIDEO_MESSAGE_CHANGE;NEW[;<n>] | ;UPDATE  [da confermare sul campo]
        if upper.startswith("VM;VIDEO_MESSAGE_CHANGE"):
            self._handle_videomessage(raw)
            return

        # FP;{json}  fuoriporta  [da confermare sul campo]
        if upper.startswith("FP;") or upper.startswith("FP{"):
            self._handle_fuoriporta(raw)
            return

        # CALL_INFO;{json}  [da confermare sul campo]
        if upper.startswith("CALL_INFO"):
            self._handle_call_info(raw)
            return

        # NEW_PHONEBOOK;<gid>;<ver>  [da confermare sul campo]
        if upper.startswith("NEW_PHONEBOOK"):
            self._handle_new_phonebook(raw)
            return

        _LOGGER.debug("MESSAGE in ingresso non mappato: %r", raw[:120])

    @staticmethod
    def _split_json_payload(raw: str, prefix_parts: int):
        """Restituisce (json_str | None) dopo aver saltato `prefix_parts`
        segmenti separati da ';'. Es. raw='MISSED_CALL;{...}' → prefix_parts=1."""
        parts = raw.split(";", prefix_parts)
        if len(parts) <= prefix_parts:
            return None
        return parts[prefix_parts].strip()

    def _parse_init_status_reply(self, raw: str) -> None:
        """Parsa GET_INIT_STATUS_REPLY;[{PARAM,VALUE}] in modo generico e robusto.

        NB: il body dei MESSAGE arriva TRONCATO a 200 char da sip_client.broadcast,
        quindi il JSON può essere incompleto → usiamo un fallback a regex sui
        segmenti {PARAM..VALUE} presenti, così estraiamo tutto ciò che c'è.
        """
        import json
        import re

        payload = self._split_json_payload(raw, 1) or ""
        pairs: dict[str, str] = {}
        try:
            arr = json.loads(payload)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict) and "PARAM" in item:
                        pairs[str(item["PARAM"])] = item.get("VALUE")
        except Exception:
            # JSON incompleto/troncato: estrai le coppie PARAM/VALUE via regex.
            for m in re.finditer(
                r'"PARAM"\s*:\s*"([^"]+)"\s*,\s*"VALUE"\s*:\s*"([^"]*)"', payload
            ):
                pairs[m.group(1)] = m.group(2)
            if not pairs:
                _LOGGER.debug("GET_INIT_STATUS_REPLY non parsabile: %r", payload[:120])

        if not pairs:
            return

        st = self.stats
        st["init_status"] = {**st.get("init_status", {}), **pairs}

        def _as_bool(v):
            return str(v).strip() in ("1", "true", "True", "ON", "on")

        if "voicemail" in pairs:
            st["voicemail"] = _as_bool(pairs["voicemail"])
        if "dnd" in pairs:
            st["dnd"] = _as_bool(pairs["dnd"])
        if "voicemail" in pairs or "dnd" in pairs:
            # Una conferma fresca dello stato: gli switch smettono di mostrare il
            # comando in attesa anche se il valore non è cambiato (issue #9).
            st["mode_seq"] = st.get("mode_seq", 0) + 1
        if "vm_level" in pairs:
            st["vm_level"] = pairs["vm_level"]
        if "vm_ver" in pairs:
            st["vm_ver"] = pairs["vm_ver"]
        # Solo nella risposta lunga (40515/2FV2, issue #4): parametri dell'appartamento
        # e cifratura del media. Letti se ci sono, ignorati se mancano.
        self._apply_apt_params(pairs)
        if "GID" in pairs:
            st["apt_gid"] = pairs["GID"]
        if "media_enc" in pairs:
            st["media_enc"] = pairs["media_enc"]
            if R.set_plant_media_enc(pairs["media_enc"]):
                _LOGGER.info("Cifratura del media dall'impianto: media_enc=%s → SRTP %s",
                             pairs["media_enc"], "attivo" if R.MEDIA_ENC else "spento")
        # token / altri param restano in init_status per usi futuri (phonebook cloud)
        if "rubrica_ver" in pairs:
            self._update_rubrica_ver(pairs["rubrica_ver"])

        _LOGGER.info(
            "GET_INIT_STATUS_REPLY: voicemail=%s dnd=%s vm_level=%s rubrica_ver=%s",
            st.get("voicemail"), st.get("dnd"), st.get("vm_level"), st.get("rubrica_ver"),
        )

    def _apply_apt_params(self, pairs: dict) -> None:
        """vm_timeout, vm_timeout_values, apt_names: da GET_INIT_STATUS_REPLY o da
        APT_PARAMS_CHANGED. Valori non validi scartati, non propagati."""
        st = self.stats
        if "vm_timeout_values" in pairs:
            vals = pairs["vm_timeout_values"]
            if isinstance(vals, list):
                clean = [int(v) for v in vals if isinstance(v, int | str) and str(v).strip().isdigit()]
                st["vm_timeout_values"] = clean or None
        if "vm_timeout" in pairs and str(pairs["vm_timeout"]).strip().isdigit():
            st["vm_timeout"] = int(pairs["vm_timeout"])
        if "apt_names" in pairs and isinstance(pairs["apt_names"], list):
            st["apt_names"] = [str(n) for n in pairs["apt_names"]]

    def _handle_apt_params_changed(self, raw: str) -> None:
        import json
        try:
            j = json.loads(self._split_json_payload(raw, 1) or "")
        except ValueError:
            _LOGGER.debug("APT_PARAMS_CHANGED non parsabile: %r", raw[:120])
            return
        if isinstance(j, dict) and "PARAM" in j:
            self._apply_apt_params({str(j["PARAM"]): j.get("VALUE")})
            _LOGGER.info("Parametro dell'appartamento cambiato: %s=%s", j["PARAM"], j.get("VALUE"))

    def _handle_apt_params_reply(self, raw: str) -> None:
        import json
        try:
            j = json.loads(self._split_json_payload(raw, 1) or "")
        except ValueError:
            j = None
        if not isinstance(j, dict):
            _LOGGER.debug("SET_APT_PARAMS_REPLY non parsabile: %r", raw[:120])
            return
        fut = self._apt_param_waiters.pop(str(j.get("MSGID")), None)
        if fut and not fut.done():
            fut.set_result(j.get("ERRCODE"))

    async def async_set_apt_param(self, param: str, value, timeout: float = 10.0) -> tuple[bool, str]:
        """SET_APT_PARAMS;{"MSGID","PARAM","VALUE"} al PICG con `Panda: set`.

        Riuscito solo con `ERRCODE: ERR_NONE` nella risposta: come l'app, una
        risposta senza ERRCODE, o nessuna risposta, è un fallimento.
        """
        import json
        import secrets
        msgid = secrets.token_hex(5)
        body = "SET_APT_PARAMS;" + json.dumps(
            {"MSGID": msgid, "PARAM": param, "VALUE": value}, separators=(",", ":"))
        fut = asyncio.get_running_loop().create_future()
        self._apt_param_waiters[msgid] = fut
        try:
            ok, msg = await self.async_send_command(
                body=body, target=R.PICG_TARGET, header_name="Panda", header_value="set")
            if not ok:
                return False, msg
            try:
                err = await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                return False, "Nessuna risposta dal citofono"
        finally:
            self._apt_param_waiters.pop(msgid, None)
        if err != "ERR_NONE":
            return False, f"Rifiutato dal citofono: {err or 'nessun ERRCODE'}"
        self._apply_apt_params({param: value})
        self._touch()
        return True, "ERR_NONE"

    async def async_request_status(self) -> None:
        """GET_INIT_STATUS al PICG: la risposta aggiorna segreteria, DND e il resto."""
        await self._request_init_status()

    def _update_rubrica_ver(self, new_ver, gid: str | None = None) -> None:
        """Aggiorna rubrica_ver; se CAMBIA (dopo il primo) emette phonebook_changed."""
        new_ver = None if new_ver is None else str(new_ver)
        old = self.stats.get("rubrica_ver")
        self.stats["rubrica_ver"] = new_ver
        if old is not None and new_ver is not None and new_ver != old:
            _LOGGER.info("Rubrica cambiata: %s → %s", old, new_ver)
            self._fire_event(
                C.EVENT_PHONEBOOK_CHANGED,
                {"gid": gid or R.SIP_USER, "rubrica_ver": new_ver},
            )

    def _handle_missed_call(self, raw: str) -> None:
        import json
        data = {"sip_id": None, "ts": None}
        payload = self._split_json_payload(raw, 1)
        if payload:
            try:
                j = json.loads(payload)
                if isinstance(j, dict):
                    data["sip_id"] = j.get("SIP_ID") or j.get("sip_id")
                    data["ts"] = j.get("TS") or j.get("ts")
            except Exception:
                _LOGGER.debug("MISSED_CALL payload non-JSON: %r", payload[:120])
        data["name"] = sip_id_name(str(data["sip_id"])) if data["sip_id"] is not None else None
        self.stats["last_missed_call"] = data
        self.stats["missed_call_count"] += 1
        _LOGGER.info("Chiamata persa: %s", data)
        self._fire_event(C.EVENT_MISSED_CALL, data)

    def _handle_videomessage(self, raw: str) -> None:
        # VM;VIDEO_MESSAGE_CHANGE;NEW[;<n>] | ;UPDATE
        parts = raw.split(";")
        change = parts[2].strip().upper() if len(parts) > 2 else "NEW"
        extra = parts[3].strip() if len(parts) > 3 else None
        is_new = change == "NEW"
        self.stats["new_videomessage"] = is_new
        self.stats["last_videomessage"] = raw[:120]
        _LOGGER.info("Videomessaggio: change=%s extra=%s", change, extra)
        self._fire_event(
            C.EVENT_VIDEOMESSAGE, {"change": change, "extra": extra, "full": raw[:120]}
        )

    def _handle_fuoriporta(self, raw: str) -> None:
        import json
        data = {"sip_id": None, "msg": None}
        payload = self._split_json_payload(raw, 1)
        if payload:
            try:
                j = json.loads(payload)
                if isinstance(j, dict):
                    data["sip_id"] = j.get("SIP_ID") or j.get("sip_id")
                    data["msg"] = j.get("MSG") or j.get("msg")
            except Exception:
                _LOGGER.debug("FP payload non-JSON: %r", payload[:120])
        self.stats["last_fuoriporta"] = data
        _LOGGER.info("Fuoriporta: %s", data)
        self._fire_event(C.EVENT_FUORIPORTA, data)

    def _handle_call_info(self, raw: str) -> None:
        import json
        data = {"sip_id": None, "reason": None, "media_type": None, "video_src": None}
        payload = self._split_json_payload(raw, 1)
        if payload:
            try:
                j = json.loads(payload)
                if isinstance(j, dict):
                    # `is not None`, non `or`: 0 è un valore significativo in tre
                    # campi su quattro — MEDIA_TYPE 0 = audio, REASON 0 = rifiutata,
                    # VIDEO_SRC 0 = sorgente non commutabile. Con `or` fino alla
                    # 1.0.6 diventavano None (MsgCallInfoReceiver.java dell'app).
                    def _pick(upper: str, lower: str):
                        value = j.get(upper)
                        return j.get(lower) if value is None else value
                    data["sip_id"] = _pick("SIP_ID", "sip_id")
                    data["reason"] = _pick("REASON", "reason")
                    data["media_type"] = _pick("MEDIA_TYPE", "media_type")
                    data["video_src"] = _pick("VIDEO_SRC", "video_src")
            except Exception:
                _LOGGER.debug("CALL_INFO payload non-JSON: %r", payload[:120])
        self.stats["last_call_info"] = data
        _LOGGER.info("Call info: %s", data)
        self._fire_event(C.EVENT_CALL_INFO, data)

    def _handle_new_phonebook(self, raw: str) -> None:
        # NEW_PHONEBOOK;<ver>;<gid> — la versione (MD5 del file, = rubrica_ver)
        # viene PRIMA del gid. Fino alla 1.0.6 li leggevamo al contrario, seguendo
        # la nostra documentazione che diceva «da confermare sul campo»: il sensore
        # Versione Rubrica prendeva il GID, e al GET_INIT_STATUS_REPLY successivo
        # il valore «cambiava» di nuovo, con un secondo phonebook_changed spurio.
        # Ordine verificato nel sorgente dell'app (MsgNewPhonebookReceiver:
        # getOrNull(…, 0) = phonebookVersion, getOrNull(…, 1) = gid).
        parts = raw.split(";")
        ver = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        gid = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        _LOGGER.info("NEW_PHONEBOOK gid=%s ver=%s", gid, ver)
        # Aggiorna rubrica_ver ed emette phonebook_changed (anche se primo valore,
        # NEW_PHONEBOOK è per definizione un cambio → forziamo l'evento).
        if ver is not None:
            old = self.stats.get("rubrica_ver")
            self.stats["rubrica_ver"] = str(ver)
            if old != str(ver):
                self._fire_event(
                    C.EVENT_PHONEBOOK_CHANGED, {"gid": gid or R.SIP_USER, "rubrica_ver": str(ver)}
                )
        else:
            self._fire_event(
                C.EVENT_PHONEBOOK_CHANGED, {"gid": gid or R.SIP_USER, "rubrica_ver": None}
            )

    async def _request_init_status(self):
        """Chiede lo stato iniziale al PICG (GET_INIT_STATUS, Panda: blue).

        La risposta arriva in modo asincrono come SIP MESSAGE
        (GET_INIT_STATUS_REPLY) → parsata in _handle_incoming_message.
        Funziona sia in UDP locale sia in cloud TLS.
        """
        try:
            ok, msg = await self.async_send_command(
                body=C.GET_INIT_STATUS,
                target=R.PICG_TARGET,
                header_name="Panda",
                header_value="blue",
            )
            # Solo se l'invio e' riuscito: altrimenti il ramo di retry del
            # keepalive (if not self._init_status_sent) non potrebbe mai
            # scattare e i sensori resterebbero a None fino al riavvio di HA.
            self._init_status_sent = bool(ok)
            _LOGGER.info("GET_INIT_STATUS → %s: ok=%s msg=%s", R.PICG_TARGET, ok, msg)
        except Exception:
            _LOGGER.exception("GET_INIT_STATUS invio fallito")

    async def _auto_startup(self):
        await asyncio.sleep(2)
        try:
            _LOGGER.info("Auto startup: registering SIP...")
            ok = await sip.do_register()
            _LOGGER.info("Auto startup: register result=%s", ok)
            if ok:
                self.stats["last_register_time"] = self._now()
            else:
                self.stats["register_failures"] += 1
            self._touch()
            if ok:
                # Stato iniziale (voicemail/dnd/rubrica_ver/vm_level) dal PICG.
                await self._request_init_status()
                # Identificazione del modello: passiva (header dei messaggi in
                # arrivo) + una sonda OPTIONS se non lo conosciamo ancora.
                self._tasks.append(asyncio.create_task(self._probe_model()))
            if ok and not R.USE_LOCAL_UDP:
                # connectProfiles è solo per la modalità cloud (push notifications)
                await asyncio.sleep(1)
                try:
                    ok2, msg2 = await sip.do_connect_profiles()
                    _LOGGER.info("connectProfiles: ok=%s msg=%s", ok2, msg2)
                except Exception as e:
                    _LOGGER.error("connectProfiles error: %s", e)
            elif not ok:
                _LOGGER.error("SIP registration failed")
        except Exception as e:
            _LOGGER.error("Auto startup error: %s", e, exc_info=True)

    async def _keepalive_loop(self):
        while self._running:
            await asyncio.sleep(120)
            await self._keepalive_tick()

    async def _keepalive_tick(self):
        """Un giro di keepalive. Separato dal loop per poterlo testare."""
        try:
            if sip.registered:
                ok = await sip.do_register()
                _LOGGER.debug("Keepalive: %s", "OK" if ok else "FAILED")
            else:
                # Fino alla 1.0.5 questo ramo non esisteva: la guardia era
                # `if sip.registered`, quindi persa la registrazione il loop
                # girava a vuoto per sempre. In UDP locale — il default —
                # non c'era nessun altro percorso di recupero: il citofono
                # restava scollegato fino al riavvio di Home Assistant.
                _LOGGER.warning("Registrazione SIP assente: provo a recuperarla")
                ok = await sip.reconnect()
                if ok:
                    # Tornati su dopo un'interruzione: mentre eravamo
                    # scollegati lo stato del Tab puo' essere cambiato
                    # (segreteria, DND, versione rubrica). Rifacciamo la
                    # domanda invece di restare con i valori di prima.
                    self._init_status_sent = False
                    _LOGGER.info("Registrazione SIP recuperata")

            if ok:
                self.stats["last_register_time"] = self._now()
                # Se lo stato iniziale non è mai stato ottenuto (primo
                # invio fallito / reconnect dopo offline), riprova ora.
                if not self._init_status_sent:
                    await self._request_init_status()
            else:
                self.stats["register_failures"] += 1
            self._touch()
        except Exception as e:
            _LOGGER.error("Keepalive error: %s", e)
