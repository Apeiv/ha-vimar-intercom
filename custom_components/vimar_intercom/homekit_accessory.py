"""The intercom as a HomeKit video doorbell of its own, with two-way audio.

Home Assistant's HomeKit bridge cannot carry the talk direction. Its doorbell
camera advertises a speaker, so the Home app shows Talk, but when the phone
sends SetupEndpoints the bridge answers with the phone's own ports as if they
were the accessory's, and nothing on the Home Assistant side listens there.
The visitor is heard, and never hears whoever answers.

So the integration publishes its own accessory with HAP-python, the library
the bridge is built on, without touching Home Assistant:

- SetupEndpoints announces ports we bind and read;
- the phone's audio is decrypted with the key the phone gave us, decoded, and
  sent to the panel through ``media.send_audio``, the same path as the card's
  microphone;
- doorbell, video, voice and the gate lock are one accessory, so the live view
  of a ring notification has Talk and the gate together.

Video to the phone is either the panel's own H.264 packets restamped for the
phone (fastest), or a shared ffmpeg re-encode with one keyframe per second
(the default: it hides the packets the cloud relay loses). The panel's audio
reaches the view's ffmpeg through a local SDP (``homekit_media.AudioTap``).

During a ring, opening the view previews the early media without answering,
and the first voice from the phone (Talk) answers the call. The option
``homekit_answer`` switches to answering as soon as the view opens.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import secrets
import socket
import struct
import tempfile
import time
from functools import partial
from uuid import UUID

from aiohttp import web
from homeassistant.components import persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from pyhap import tlv
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import (
    SETUP_ADDR_INFO,
    SETUP_SRTP_PARAM,
    SETUP_STATUS,
    SETUP_TYPES,
    SRTP_CRYPTO_SUITES,
    VIDEO_CODEC_PARAM_LEVEL_TYPES,
    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES,
    Camera,
)
from pyhap.const import CATEGORY_VIDEO_DOOR_BELL
from pyhap.util import to_base64_str

from . import homekit_media as hkm
from . import media_handler as media
from . import runtime as R
from . import sip_client as sip
from .const import (
    CONF_HOMEKIT_ANSWER,
    CONF_HOMEKIT_RING_BUTTON,
    CONF_HOMEKIT_SMOOTH,
    DEFAULT_HOMEKIT_ANSWER,
    DEFAULT_HOMEKIT_RING_BUTTON,
    DEFAULT_HOMEKIT_SMOOTH,
    DOMAIN,
    HOMEKIT_ANSWER_OPEN,
    HOMEKIT_DATA,
    HOMEKIT_QR_URL,
)
from .homekit_audio import LOOPBACK_INPUT, AudioBridge, ffmpeg_binary, log_stderr, stop_ffmpeg
from .homekit_files import homekit_files
from .homekit_transcode import Transcoder
from .homekit_video import DirectVideo

_LOGGER = logging.getLogger(__name__)

ACCESSORY_NAME = "Citofono"
GATE_NAME = "Cancello"
# HAP server port: outside the range Home Assistant gives its own bridges
# (21063 and up).
HOMEKIT_PORT = 21099

# The gate opens the same door as the integration's lock (door_target). The
# intercom only pulses the strike and cannot know whether the gate is shut, so
# after opening the lock goes back to "unknown", never to "secured" (iOS
# announced the old automatic relock as "locked again").
GATE_RELOCK_SECONDS = 5.0
LOCK_UNSECURED, LOCK_SECURED, LOCK_UNKNOWN = 0, 1, 3

# How long a view waits for the call (or the ring preview) to carry media.
CALL_WAIT = 15.0
# Then for the panel's first video packets. They come about 0.4 s after the
# answer; without them the view goes on audio only instead of making the
# visitor wait.
VIDEO_WAIT = 2.0
# For a complete keyframe in memory before opening direct video.
KEYFRAME_WAIT = 2.0
# iOS asks for the notification's picture as the ring goes out, about a
# second before the first frame is decoded: during a ring or a call the
# snapshot waits this long for it instead of sending the black placeholder.
SNAPSHOT_WAIT = 3.0
# How often the latest frame of a call or ring is copied for the Home tile:
# the frame grabber drops its picture when the call ends.
FRAME_KEEP_EVERY = 1.0

# Resolutions offered to the phone. The panel sends 320x240 and direct video is
# copied as is: the list only lets iOS pick something.
_RESOLUTIONS = [
    [320, 240, 15], [320, 240, 30], [480, 270, 30], [480, 360, 30],
    [640, 360, 30], [640, 480, 30], [1024, 576, 30], [1024, 768, 30],
    [1280, 720, 30], [1280, 960, 30], [1920, 1080, 30],
]

_PIN_NOTIFICATION = f"{DOMAIN}_homekit_pairing"


class VimarDoorbell(Camera):
    """HomeKit video doorbell: video, two-way audio, ring, gate."""

    category = CATEGORY_VIDEO_DOOR_BELL

    def __init__(self, driver, name: str, *, hass: HomeAssistant, hub,
                 stream_address: str, serial: str, smooth: bool = True,
                 answer_mode: str = DEFAULT_HOMEKIT_ANSWER,
                 ring_button: bool = DEFAULT_HOMEKIT_RING_BUTTON) -> None:
        options = {
            "video": {
                "codec": {
                    "profiles": [VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"]],
                    "levels": [
                        VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"],
                        VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_2"],
                        VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE4_0"],
                    ],
                },
                "resolutions": _RESOLUTIONS,
            },
            "audio": {
                "codecs": [
                    {"type": "OPUS", "samplerate": 16},
                    {"type": "OPUS", "samplerate": 24},
                ],
            },
            "srtp": True,
            "address": stream_address,
            # Views at once (iPhone, Mac, Watch).
            "stream_count": 3,
        }
        super().__init__(options, driver, name)
        self._hass = hass
        self._hub = hub
        self._smooth = smooth
        self._answer_on_open = answer_mode == HOMEKIT_ANSWER_OPEN
        # True once a view answered the current ring: closing the last view
        # then hangs up, since the Home app has no other hang-up button.
        self._answered = False
        # An answer is on its way to the panel (see _answer).
        self._answering = False
        self._last_frame: bytes | None = None
        # After a restart the Home tile shows the last ring photo, read once.
        self._photo_read = False
        self._tasks: set[asyncio.Task] = set()
        self._transcoder: Transcoder | None = None
        self._transcoder_lock = asyncio.Lock()
        # Bumped when a call or ring ends (and at a new ring): media_handler's
        # video protocol is the same for every call, so this is what tells an
        # encoder of this call from one of the previous visitor.
        self._call_gen = 0
        # HomeKit was turned off: nothing new may start.
        self._closed = False
        self.set_info_service(
            manufacturer="Vimar", model="Video intercom",
            serial_number=serial, firmware_revision="1.0")

        # The doorbell is the primary service: it makes this a video doorbell,
        # with the ring notification and the snapshot.
        serv_doorbell = self.add_preload_service("Doorbell")
        self.set_primary_service(serv_doorbell)
        self._char_ring = serv_doorbell.configure_char(
            "ProgrammableSwitchEvent", value=0)
        # The same ring as a button, for Home automations (optional). When it
        # is off its IIDs are still used up, so the speaker and the gate keep
        # the IIDs they had: the Home app keys rooms, names and automations on
        # them, and turning the option on or off must not reset the gate.
        self._char_ring_switch = None
        if ring_button:
            serv_switch = self.add_preload_service("StatelessProgrammableSwitch")
            self._char_ring_switch = serv_switch.configure_char(
                "ProgrammableSwitchEvent", value=0, valid_values={"SinglePress": 0})
        else:
            unused = self.driver.loader.get_service("StatelessProgrammableSwitch")
            self.iid_manager.counter += 1 + len(unused.characteristics)

        # The panel's speaker: with it the Home app shows Talk. Mute and volume
        # are the phone's; the voice itself comes over RTP (AudioBridge).
        serv_speaker = self.add_preload_service("Speaker", chars=["Volume"])
        serv_speaker.configure_char("Mute", value=False)
        serv_speaker.configure_char("Volume", value=100)

        # The gate, in the same accessory, so the live view can open it.
        serv_lock = self.add_preload_service("LockMechanism", chars=["Name"])
        serv_lock.configure_char("Name", value=GATE_NAME)
        self._char_lock_current = serv_lock.configure_char(
            "LockCurrentState", value=LOCK_UNKNOWN)
        self._char_lock_target = serv_lock.configure_char(
            "LockTargetState", value=LOCK_SECURED, setter_callback=self._set_lock_target)

    def setup_message(self) -> None:
        """pyhap prints the setup code and a QR to stdout when the accessory
        is not paired, straight into Home Assistant's container log. The
        HomeKit page of the options (administrators only) shows them."""

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── ring ──

    def ring(self) -> None:
        """Someone rang: the phone's notification starts here."""
        if not self._hub.in_call and not hkm.video_ready(self._hub):
            # A new visitor: an encoder left from the previous one is not
            # reused, even if the end of that call was missed. Not while video
            # flows: that is this visitor's, and open views read its encoder.
            self._call_gen += 1
        self._char_ring.set_value(0)
        if self._char_ring_switch is not None:
            self._char_ring_switch.set_value(0)
        _LOGGER.info("HomeKit: ring sent to the doorbell")
        if self._smooth:
            # Early media brings the video now: the encoder warms up while
            # whoever is at home picks up the phone.
            self._spawn(self._prewarm())

    async def _prewarm(self) -> None:
        waited = 0.0
        while not hkm.video_ready(self._hub) and waited < 4.0 and not self._closed:
            await asyncio.sleep(0.05)
            waited += 0.05
        if hkm.video_ready(self._hub) and not self._closed:
            await self._ensure_transcoder()

    # ── answering ──

    async def _answer(self, why: str) -> None:
        """Answer the ring this view is previewing, once."""
        if self._answering or self._answered or self._hub.in_call or not sip.ringing():
            return
        self._answering = True
        try:
            ok, msg = await self._hub.async_answer()
        finally:
            self._answering = False
        if not ok:
            _LOGGER.warning("HomeKit: answering failed: %s", msg)
            return
        self._answered = True
        _LOGGER.info("HomeKit: ring answered (%s)", why)
        if not self._live_sessions():
            # The view closed while the answer was on its way: its close
            # found nothing to hang up, so it is done here.
            await self._hang_up_if_last()

    async def _answer_for(self, session_info, why: str) -> None:
        await self._answer(why)
        if self._answered:
            _mark(session_info, "answer")

    def _live_sessions(self) -> bool:
        """A view is open, or opening, and not closing."""
        return any(("proc" in i or i.get("starting")) and not i.get("stopping")
                   for i in self.sessions.values())

    def _first_audio(self, session_info) -> None:
        # The stage to compare with the VIEW app: the street is audible on
        # the phone. It usually comes after "stream started", so it logs the
        # timeline so far on its own line.
        _mark(session_info, "first audio")
        _LOGGER.info("HomeKit: first audio to the phone (session %s): %s",
                     session_info.get("id"), _timeline_summary(session_info))

    def _first_voice(self, session_info) -> None:
        _mark(session_info, "first voice")
        self._on_phone_voice()

    def _on_phone_voice(self) -> None:
        """First voice from the phone: whoever pressed Talk takes the call."""
        if sip.ringing() and not self._hub.in_call:
            self._spawn(self._answer("Talk"))

    # ── re-encoding ──

    async def _ensure_transcoder(self, session_info=None, early: bool = False) -> Transcoder | None:
        """The current call's encoder, started if needed; None when there is
        no call video to encode (or the view asking is closing).

        ``early``: a view is placing the call, and its video has not come yet.
        The encoder starts anyway and waits for the first packets on its port
        (see _open_call). If that call never connects, the end of it
        (on_video_ended) stops the encoder, as for any call.

        media_handler's video protocol lives as long as the hub, so the
        protocol cannot tell two calls apart. The call generation can: an
        encoder of an earlier call shows the previous visitor, and is replaced.
        """
        async with self._transcoder_lock:
            gen = self._call_gen
            tc = self._transcoder
            if tc and tc.running and tc.generation == gen:
                if not early and not tc.begun:
                    # Warmed up before the call's video (early): it starts from
                    # the group in sequence order now, as any encoder does.
                    tc.begin()
                return tc
            if tc:
                # Dead, or another call's: detach it and close its sockets first.
                self._transcoder = None
                hkm.remove_video_sink(tc.feed, tc.video_proto)
                await tc.stop()
            if (self._closed or not (early or hkm.video_ready(self._hub))
                    or (early and not self._hub.call_coming)
                    or (session_info is not None and session_info.get("stopping"))):
                # (early) The placed call already failed (a busy panel, #41's
                # silent one) and its end came before this task took the lock:
                # an encoder started now would wait on its port with nobody
                # left to stop it (review of #48).
                # The call is over, or the view is: an encoder started now
                # would read the end of this call, or the next one's start.
                return None
            # Early, the call has not set its panel yet: the parameters of the
            # panel the view calls. Without them the encoder would miss the
            # first keyframe (seen right after a restart: picture at 3.6 s),
            # so it waits for the video like before.
            sps, pps = hkm.parameter_sets(R.INTERCOM) if early else hkm.parameter_sets()
            if early and not (sps and pps):
                return None
            tc = Transcoder(sps, pps)
            tc.generation = gen

            def attach() -> None:
                tc.video_proto = media.video_proto
                hkm.add_video_sink(tc.feed)

            try:
                ok = await tc.start(hkm.gop_for_direct_video, on_ready=attach, defer=early)
            except BaseException:
                # Cancelled (or failed) with ffmpeg possibly running already.
                hkm.remove_video_sink(tc.feed, tc.video_proto)
                await tc.stop()
                raise
            if ok and (gen != self._call_gen or self._closed):
                # The call ended while it started: it belongs to nobody.
                ok = False
            if not ok:
                hkm.remove_video_sink(tc.feed, tc.video_proto)
                await tc.stop()
                if gen == self._call_gen and not self._closed:
                    _LOGGER.warning("HomeKit: re-encoding did not start, using direct video")
                return None
            self._transcoder = tc
            return tc

    async def _stop_transcoder(self) -> None:
        async with self._transcoder_lock:
            tc, self._transcoder = self._transcoder, None
            if tc:
                hkm.remove_video_sink(tc.feed, tc.video_proto)
                await tc.stop()

    # ── gate ──

    def _set_lock_target(self, value: int) -> None:
        if value == LOCK_UNSECURED:
            self._spawn(self._open_gate())

    async def _open_gate(self) -> None:
        """Pulse the strike. Report unlocked, then unknown (never locked), and
        reset the target so the next tap opens again."""
        ok, result, code = await self._hub.async_door()
        if ok:
            _LOGGER.info("HomeKit: gate opened")
            self._char_lock_current.set_value(LOCK_UNSECURED)
            await asyncio.sleep(GATE_RELOCK_SECONDS)
        else:
            _LOGGER.error("HomeKit: opening the gate failed: %s %s", result, code)
        self._char_lock_target.set_value(LOCK_SECURED)
        self._char_lock_current.set_value(LOCK_UNKNOWN)

    # ── snapshot ──

    async def async_get_snapshot(self, image_size) -> bytes:
        """The latest frame of this call or ring, else the last one we saw.

        The frame grabber drops its image when the call ends; the Home tile
        should keep showing who was at the door (_keep_last_frame).
        """
        frame = self._live_picture()
        waited = 0.0
        while not frame and waited < SNAPSHOT_WAIT and (sip.ringing() or self._hub.in_call):
            await asyncio.sleep(0.1)
            waited += 0.1
            frame = self._live_picture()
        if waited:
            _LOGGER.debug("HomeKit: snapshot waited %.1f s for the first frame (%s)",
                          waited, "got it" if frame else "none")
        if frame:
            self._last_frame = frame
        elif self._last_frame is None and not self._photo_read and self._hass is not None:
            # Nothing seen since Home Assistant started: the last ring photo
            # rather than the placeholder, as before the restart.
            self._photo_read = True
            self._last_frame = await self._hass.async_add_executor_job(hkm.last_ring_photo)
        return self._last_frame or hkm.IDLE_IMAGE

    async def _keep_last_frame(self) -> None:
        """Copy the latest live frame, so the tile outlives the call.

        Snapshots are rarely asked for while a view is open, and the frame
        grabber drops its picture when the call ends: without this the tile
        went black once the Home app refreshed it after a view.
        """
        while not self._closed:
            frame = self._live_picture()
            if frame:
                self._last_frame = frame
            await asyncio.sleep(FRAME_KEEP_EVERY)

    def _live_picture(self) -> bytes | None:
        """This call's latest picture.

        The re-encoder's first: it conceals lost packets, so it has a picture
        a few tenths of a second into the ring. The frame grabber waits for a
        complete keyframe: on a 40517, when the relay lost part of the first
        one, that took 3.6 s and the ring notification came out black.
        """
        tc = self._transcoder
        if tc and tc.running and tc.generation == self._call_gen and tc.last_jpeg:
            return tc.last_jpeg
        return hkm.last_frame()

    # ── streaming session ──

    def set_endpoints(self, value, stream_idx=None):
        """Like pyhap's, but with ports of ours that we actually listen on.

        pyhap sends the phone its own ports back as if they were the
        accessory's and opens nothing. For audio that meant nobody ever heard
        whoever answered.
        """
        if stream_idx is None:
            stream_idx = 0
        objs = tlv.decode(value, from_base64=True)
        session_id = UUID(bytes=objs[SETUP_TYPES["SESSION_ID"]])

        addr = tlv.decode(objs[SETUP_TYPES["ADDRESS"]])
        address = addr[SETUP_ADDR_INFO["ADDRESS"]].decode("utf8")
        target_video_port = struct.unpack("<H", addr[SETUP_ADDR_INFO["VIDEO_RTP_PORT"]])[0]
        target_audio_port = struct.unpack("<H", addr[SETUP_ADDR_INFO["AUDIO_RTP_PORT"]])[0]

        video = tlv.decode(objs[SETUP_TYPES["VIDEO_SRTP_PARAM"]])
        audio = tlv.decode(objs[SETUP_TYPES["AUDIO_SRTP_PARAM"]])
        v_key = video[SETUP_SRTP_PARAM["MASTER_KEY"]]
        v_salt = video[SETUP_SRTP_PARAM["MASTER_SALT"]]
        a_key = audio[SETUP_SRTP_PARAM["MASTER_KEY"]]
        a_salt = audio[SETUP_SRTP_PARAM["MASTER_SALT"]]

        self._drop_stale_sessions()
        # Audio socket first: the phone may send as soon as it has the answer.
        a_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        a_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        a_sock.bind(("0.0.0.0", 0))  # noqa: S104
        a_sock.setblocking(False)
        local_a_port = a_sock.getsockname()[1]
        # Video socket: direct video leaves from it and the phone's RTCP comes
        # back to it. For a call without video ffmpeg takes the port over.
        v_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        v_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        v_sock.bind(("0.0.0.0", 0))  # noqa: S104
        v_sock.setblocking(False)
        local_v_port = v_sock.getsockname()[1]

        suite = SRTP_CRYPTO_SUITES["AES_CM_128_HMAC_SHA1_80"]
        video_srtp = tlv.encode(
            SETUP_SRTP_PARAM["CRYPTO"], suite,
            SETUP_SRTP_PARAM["MASTER_KEY"], v_key,
            SETUP_SRTP_PARAM["MASTER_SALT"], v_salt)
        audio_srtp = tlv.encode(
            SETUP_SRTP_PARAM["CRYPTO"], suite,
            SETUP_SRTP_PARAM["MASTER_KEY"], a_key,
            SETUP_SRTP_PARAM["MASTER_SALT"], a_salt)
        video_ssrc = int.from_bytes(os.urandom(3), "big")
        audio_ssrc = int.from_bytes(os.urandom(3), "big")

        res_address = tlv.encode(
            SETUP_ADDR_INFO["ADDRESS_VER"], self.stream_address_isv6,
            SETUP_ADDR_INFO["ADDRESS"], self.stream_address.encode("utf-8"),
            SETUP_ADDR_INFO["VIDEO_RTP_PORT"], struct.pack("<H", local_v_port),
            SETUP_ADDR_INFO["AUDIO_RTP_PORT"], struct.pack("<H", local_a_port))
        response = tlv.encode(
            SETUP_TYPES["SESSION_ID"], session_id.bytes,
            SETUP_TYPES["STATUS"], SETUP_STATUS["SUCCESS"],
            SETUP_TYPES["ADDRESS"], res_address,
            SETUP_TYPES["VIDEO_SRTP_PARAM"], video_srtp,
            SETUP_TYPES["AUDIO_SRTP_PARAM"], audio_srtp,
            SETUP_TYPES["VIDEO_SSRC"], struct.pack("<I", video_ssrc),
            SETUP_TYPES["AUDIO_SSRC"], struct.pack("<I", audio_ssrc),
            to_base64=True)

        self.sessions[session_id] = {
            "id": session_id,
            "stream_idx": stream_idx,
            "address": address,
            "v_port": target_video_port,
            "v_srtp_key": to_base64_str(v_key + v_salt),
            "v_ssrc": video_ssrc,
            "a_port": target_audio_port,
            "a_srtp_key": to_base64_str(a_key + a_salt),
            "a_ssrc": audio_ssrc,
            "local_v_port": local_v_port,
            "local_a_port": local_a_port,
            "a_sock": a_sock,
            "v_sock": v_sock,
            "created": time.monotonic(),
            # Start and close take turns: a stop that arrives while
            # start_stream works waits for it, then closes what it opened.
            "lock": asyncio.Lock(),
        }
        _LOGGER.debug("HomeKit: session %s, phone %s v%d a%d, ours v%d a%d",
                      session_id, address, target_video_port, target_audio_port,
                      local_v_port, local_a_port)
        self._management[stream_idx].get_characteristic("SetupEndpoints").set_value(
            response)

    def _drop_stale_sessions(self) -> None:
        """Sessions prepared and never started (the phone changed its mind),
        and sessions we closed ourselves (the call ended, ffmpeg exited) whose
        stop the phone never sent. Closed ones stay a minute: pyhap deletes
        the session right after our stop_stream returns.

        One never started goes through the normal close: if anything of it
        got open (the hub's viewer, a video sink), closing its sockets alone
        would leave the rest behind."""
        now = time.monotonic()
        for sid, info in list(self.sessions.items()):
            never_started = ("proc" not in info and not info.get("starting")
                             and not info.get("stopping")
                             and now - info.get("created", now) > 60)
            closed = now - info.get("closed_at", now) > 60
            if never_started:
                self._spawn(self._close_session(info))
            elif closed:
                _close_quietly(info.get("a_sock"))
                _close_quietly(info.get("v_sock"))
            if never_started or closed:
                del self.sessions[sid]

    async def start_stream(self, session_info, stream_config) -> bool:
        # A view on its way up: the end of the call closes it too.
        session_info["starting"] = True
        # The view's timeline: every stage in ms from here (see _mark).
        session_info["t0"], session_info["timeline"] = time.monotonic(), {}
        try:
            async with session_info["lock"]:
                ok = await self._open_stream(session_info, stream_config)
        except asyncio.CancelledError:
            # pyhap gave up on it: nobody will call stop_stream.
            self._spawn(self._close_session(session_info))
            raise
        except Exception:  # noqa: BLE001
            # ffmpeg missing, a socket refused, a bug: whatever got open
            # (the hub's viewer, the video sink on the hub's protocol, the
            # sockets) is released below, like any failed start.
            _LOGGER.exception("HomeKit: the view did not start")
            ok = False
        finally:
            session_info.pop("starting", None)
        # Closed while starting: for pyhap it started and then stopped. With
        # False pyhap deleted the session that its own stop was about to delete.
        if session_info.get("stopping"):
            return True
        if not ok:
            # pyhap drops a session that failed to start without calling
            # stop_stream: its sockets and its place among the hub's viewers
            # are released here, or every later auto-call is never hung up.
            await self._close_session(session_info)
        return ok

    async def _open_call(self, session_info) -> bool:
        """Get a call (or the ring preview) carrying media for this view."""
        try:
            opened = await self._hub.stream_opened(reflex_guard=False)
            _mark(session_info, "stream_opened")
        finally:
            # stream_opened counts the viewer on its first line, before any
            # await, even when it says no call is coming or this start is
            # cancelled: the close has to give that place back either way.
            session_info["viewer"] = True
        if not opened or session_info.get("stopping"):
            # Closed while the hub was placing the call: nothing more for it,
            # above all no answer. The close gives the viewer back.
            return False
        if self._smooth and not hkm.video_ready(self._hub) and not sip.ringing():
            # Not during a ring: its panel may not be R.INTERCOM, whose SPS/PPS
            # the early start takes, and its early media brings the video
            # within a few hundred ms anyway (review of #48).
            # The re-encoder's ffmpeg takes about half a second to start. Started
            # only once the call's video arrived, it was the longest stage of a
            # view on a 40515: call at 1.15 s, re-encoder ready at 2.0-2.1 s (#32).
            # Started now, it is ready when the first packets arrive.
            self._spawn(self._ensure_transcoder(session_info, early=True))
        waited = 0.0
        while not (self._hub.video_active or self._hub.in_call) and waited < CALL_WAIT:
            if session_info.get("stopping"):
                return False
            await asyncio.sleep(0.02)
            waited += 0.02
        if not (self._hub.video_active or self._hub.in_call):
            _LOGGER.warning("HomeKit: no call after %.0f s", CALL_WAIT)
            return False
        _mark(session_info, "call")
        if self._answer_on_open:
            # Not awaited: the view goes on opening while the answer travels
            # (a round trip through the relay). Early media already brings
            # the picture and the sound.
            self._spawn(self._answer_for(session_info, "view opened"))
        waited = 0.0
        while not hkm.video_ready(self._hub) and waited < VIDEO_WAIT:
            if session_info.get("stopping"):
                return False
            await asyncio.sleep(0.02)
            waited += 0.02
        return not session_info.get("stopping")

    async def _open_stream(self, session_info, stream_config) -> bool:
        """Open the call, then video and audio side by side. Each piece goes
        into session_info as soon as it exists, so a close finds it mid-way.

        The audio needs only to know whether the call has video (without it,
        its ffmpeg sends a black picture). It used to start after the video,
        re-encoder included: first audio waited about 0.9 s for a picture it
        does not depend on (40515, #32)."""
        if not await self._open_call(session_info) or session_info.get("stopping"):
            return False
        direct = hkm.video_ready(self._hub)
        # gather: a cancelled start cancels both; each part's pieces are in
        # session_info already, and the close releases them.
        video_mode, audio_ok = await asyncio.gather(
            self._open_video(session_info, stream_config, direct),
            self._open_audio(session_info, stream_config, direct),
            return_exceptions=True)
        for result in (video_mode, audio_ok):
            if isinstance(result, BaseException):
                raise result
        if video_mode is None or not audio_ok or session_info.get("stopping"):
            return False
        _LOGGER.info("HomeKit: stream started to %s (PID %d, video %s) in %d ms (%s)",
                     session_info["address"], session_info["proc"].pid, video_mode,
                     _elapsed_ms(session_info), _timeline_summary(session_info))
        return True

    async def _open_video(self, session_info, stream_config, direct: bool) -> str | None:
        """The panel's video to the phone, direct or re-encoded. None: the
        view is closing."""
        address = session_info["address"]
        v_port = session_info["v_port"]
        v_pt = _pt(stream_config.get("v_payload_type"), 99)
        video_mode = "black (audio-only call)"
        if direct:
            video = DirectVideo(session_info["v_sock"], (address, v_port),
                                session_info["v_srtp_key"], session_info["v_ssrc"], v_pt)
            session_info["video"] = video
            video.on_first_packet = lambda: _mark(session_info, "first packet")
            if not hkm.gop_has_keyframe():
                # No whole keyframe cached (e.g. just after a lost packet): ask
                # for one now. A panel that honours it (the 40515: IDR in about
                # 0.25 s) opens the view at once instead of at its next own
                # keyframe (up to 3 s); one that ignores it (the 40517) loses
                # nothing but the request. Both paths below need a panel IDR:
                # the re-encoder decodes from it too.
                self._spawn(sip.send_keyframe_request())
            await video.open()
            tc = await self._ensure_transcoder(session_info) if self._smooth else None
            if tc:
                _mark(session_info, "transcoder")
            if session_info.get("stopping"):
                return None
            if tc:
                waited = 0.0
                while not tc.gop.has_keyframe and waited < KEYFRAME_WAIT:
                    if session_info.get("stopping"):
                        return None
                    await asyncio.sleep(0.02)
                    waited += 0.02
            if tc and tc.gop.has_keyframe:
                _mark(session_info, "keyframe")
                # No await until attached (see below).
                gop = list(tc.gop.packets)
                _mark(session_info, "video begin")
                video.begin(gop, [])
                tc.add_sink(video.on_live)
                session_info["video_detach"] = lambda: tc.remove_sink(video.on_live)
                video_mode = "re-encoded"
            else:
                waited = 0.0
                while not hkm.gop_has_keyframe() and waited < KEYFRAME_WAIT:
                    if session_info.get("stopping"):
                        return None
                    await asyncio.sleep(0.02)
                    waited += 0.02
                if hkm.gop_has_keyframe():
                    _mark(session_info, "keyframe")
                # No await from here until attached: the replayed group and the
                # live packets must meet without a lost or doubled packet.
                prefix, gop = hkm.gop_for_direct_video()
                _mark(session_info, "video begin")
                video.begin(gop, prefix)
                hkm.add_video_sink(video.on_live)
                session_info["video_detach"] = lambda: hkm.remove_video_sink(video.on_live)
                video_mode = "direct"
        else:
            _close_quietly(session_info.pop("v_sock", None))

        return video_mode

    async def _open_audio(self, session_info, stream_config, direct: bool) -> bool:
        """The street's audio to the phone and the phone's to the street, and
        the view's ffmpeg (which also sends the black picture of a call
        without video)."""
        address = session_info["address"]
        v_port = session_info["v_port"]
        v_pt = _pt(stream_config.get("v_payload_type"), 99)
        # A port probe and a temporary SDP file: blocking, so off the loop.
        tap = await asyncio.get_running_loop().run_in_executor(None, hkm.AudioTap)
        session_info["tap"] = tap
        if session_info.get("stopping"):
            return False
        rate = int(stream_config.get("a_sample_rate") or 16) * 1000
        bridge = AudioBridge(
            session_info["a_sock"],
            (session_info["address"], session_info["a_port"]),
            session_info["a_srtp_key"], rate, media.send_audio,
            on_first_voice=lambda: self._first_voice(session_info))
        bridge.on_first_audio = lambda: self._first_audio(session_info)
        session_info["bridge"] = bridge
        await bridge.start()
        if session_info.get("stopping"):
            return False

        a_pt = _pt(stream_config.get("a_payload_type"), 110)
        a_bitrate = int(stream_config.get("a_max_bitrate") or 24)
        a_ptime = int(stream_config.get("a_packet_time") or 20)
        black_video = [] if direct else [
            # A call without video (the indoor monitor calling): a black
            # picture, or the Home app keeps spinning.
            "-map", "1:v:0", "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
            "-g", "10", "-an",
            "-payload_type", str(v_pt), "-ssrc", str(session_info["v_ssrc"]),
            "-f", "rtp",
            "-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80",
            "-srtp_out_params", session_info["v_srtp_key"],
            f"srtp://{address}:{v_port}?rtcpport={v_port}"
            f"&localrtpport={session_info['local_v_port']}&pkt_size=1316",
        ]
        cmd = [
            ffmpeg_binary(), "-hide_banner", "-nostats", "-loglevel", "warning",
            "-protocol_whitelist", "file,udp,rtp",
            # 40 ms instead of ffmpeg's 100 ms default per lost packet: every
            # relay loss used to stall the audio.
            "-fflags", "+genpts", "-max_delay", "40000",
            *LOOPBACK_INPUT, "-i", tap.sdp_path,
            *(["-f", "lavfi", "-i", "color=c=black:s=320x240:r=5"] if not direct else []),
            *black_video,
            # Street audio: Opus in the clear to the bridge, which encrypts it.
            "-map", "0:a:0", "-vn", "-c:a", "libopus", "-application", "lowdelay",
            "-ac", "1", "-ar", str(rate), "-b:a", f"{a_bitrate}k",
            "-frame_duration", str(a_ptime),
            "-payload_type", str(a_pt), "-ssrc", str(session_info["a_ssrc"]),
            # A home hub (Apple TV, HomePod) relaying for a phone away from
            # home asks for 60 ms packets; a 60 ms Opus packet does not fit the
            # default 188-byte RTP packet, ffmpeg exited and the view died.
            "-f", "rtp", f"rtp://127.0.0.1:{bridge.encoder_port}?pkt_size=1316",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except OSError as err:
            # start_stream closes the session.
            _LOGGER.error("HomeKit: ffmpeg did not start: %s", err)
            return False
        session_info["proc"] = proc
        _mark(session_info, "ffmpeg")
        # Only now: the tap sends to a port the ffmpeg just started listening on.
        tap.attach()
        session_info["tasks"] = [
            asyncio.create_task(log_stderr(proc, "ffmpeg HomeKit")),
            asyncio.create_task(self._watch(session_info)),
        ]
        return True

    async def _watch(self, session_info) -> None:
        """When ffmpeg exits by itself, tell the phone the slot is free."""
        proc = session_info["proc"]
        await proc.wait()
        if session_info.get("stopping"):
            return
        _LOGGER.info("HomeKit: stream ended (ffmpeg exited with %s)", proc.returncode)
        await self._close_session(session_info)
        # The session stays: the phone, told here, sends its own stop and pyhap
        # looks for it.
        if session_info["id"] in self.sessions:
            self.set_streaming_available(session_info["stream_idx"])

    # ── the call ends ──

    def on_video_ended(self) -> None:
        """Call or ring over: close the open views instead of freezing them.

        HomeKit gives an accessory no way to close the Home app's viewer, but
        ending the session stops the audio and frees the slot at once.
        """
        self._answered = False
        self._call_gen += 1
        for info in list(self.sessions.values()):
            # Views still starting too: they would come up frozen and silent,
            # and keep their place among the hub's viewers.
            if ("proc" in info or info.get("starting")) and not info.get("stopping"):
                self._spawn(self._end_from_panel(info))
        # Always, not only when there is one: an encoder still starting is not
        # in self._transcoder yet. The lock puts this stop after that start,
        # and the generation bumped above makes that start give its encoder
        # up, so the next call never inherits this call's encoder.
        self._spawn(self._stop_transcoder())

    async def _end_from_panel(self, session_info) -> None:
        _LOGGER.info("HomeKit: the call ended, closing the view")
        for part in (session_info.get("video"), session_info.get("bridge")):
            if part:
                part.send_bye()
        await self._close_session(session_info)
        if session_info["id"] in self.sessions:
            self.set_streaming_available(session_info["stream_idx"])

    async def async_close_views(self) -> None:
        """Close every open view (unload, reload, HomeKit turned off).

        pyhap's driver stop does it too (Camera.stop), but explicitly here the
        views' ffmpegs never outlive the integration, whatever pyhap does.
        """
        await asyncio.gather(
            *(self._close_session(info) for info in list(self.sessions.values())),
            return_exceptions=True)

    async def stop_stream(self, session_info) -> None:
        await self._close_session(session_info)
        _LOGGER.info("HomeKit: view closed")

    async def _close_session(self, session_info) -> None:
        """Close everything of the session once, whoever asks.

        Three parties ask (the phone's stop, the panel hanging up, ffmpeg
        exiting), sometimes together. The close is one task, shielded from the
        cancellation of whoever waits for it.
        """
        session_info["stopping"] = True
        task = session_info.get("closing")
        if task is None:
            session_info["closing_by"] = asyncio.current_task()
            task = session_info["closing"] = asyncio.ensure_future(
                self._do_close(session_info))
        await asyncio.shield(task)

    async def _do_close(self, session_info) -> None:
        lock = session_info.get("lock")
        if lock is None:
            lock = session_info["lock"] = asyncio.Lock()
        async with lock:  # if start_stream is working, it finishes first
            try:
                await stop_ffmpeg(session_info.get("proc"))
                for task in session_info.get("tasks", []):
                    # Not the watcher that asked for this close: it still has
                    # to tell the phone that the slot is free.
                    if task is session_info.get("closing_by"):
                        continue
                    task.cancel()
                tap = session_info.pop("tap", None)
                if tap:
                    tap.close()
                bridge = session_info.pop("bridge", None)
                if bridge:
                    await bridge.stop()
                _close_quietly(session_info.pop("a_sock", None))
                await self._stop_video(session_info)
            finally:
                # Whatever failed above, the hub gets its viewer back, or the
                # call it placed for this view is never hung up.
                session_info["closed_at"] = time.monotonic()
                if session_info.pop("viewer", False):
                    await self._hub.stream_closed()
                    await self._hang_up_if_last()

    async def _hang_up_if_last(self) -> None:
        """The last view of a call HomeKit answered or placed is closed: hang up.

        The Home app has no hang-up button; closing the view is how you end the
        call. Without this a ring answered from HomeKit stayed up until the
        panel's own timeout, and v1.0.9 waits 30 s before ending an auto-call.
        A ring only previewed is left alone: the Tab can still answer it, and
        so is a call someone else (a camera card, /av) is still watching.

        The hang-up is its own task: it runs inside the session's close, under
        its lock, and a BYE the cloud never answers held that close for ~5 s.
        """
        if self._live_sessions():
            return
        if self._hub.should_hang_up_for_viewers(self._answered):
            self._answered = False
            _LOGGER.info("HomeKit: last view closed, hanging up")
            self._spawn(self._hang_up())

    async def _hang_up(self) -> None:
        try:
            await self._hub.async_hangup()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HomeKit: hanging up failed")

    async def _stop_video(self, session_info) -> None:
        video = session_info.pop("video", None)
        if video:
            detach = session_info.pop("video_detach", None)
            if detach:
                detach()
            await video.stop()
        _close_quietly(session_info.pop("v_sock", None))

    async def reconfigure_stream(self, session_info, stream_config) -> bool:
        # Bitrate and resolution are the panel's, not ours to change.
        return True


# ─── the view's timeline ─────────────────────────────────────────────────────

# Guessing from logs with different clocks cost days: every stage of opening a
# view is measured from one origin, start_stream, on the monotonic clock. The
# marks are DEBUG; the INFO line at "stream started" carries the summary.

def _elapsed_ms(session_info) -> int:
    t0 = session_info.get("t0")
    return round((time.monotonic() - t0) * 1000) if t0 is not None else 0


def _mark(session_info, stage: str) -> None:
    """Note a stage of the view, once, in ms since start_stream."""
    timeline = session_info.get("timeline")
    if timeline is None or stage in timeline:
        return
    timeline[stage] = ms = _elapsed_ms(session_info)
    _LOGGER.debug("HomeKit: timeline %s +%d ms (session %s)", stage, ms,
                  session_info.get("id"))


def _timeline_summary(session_info) -> str:
    """'call 120 ms, keyframe 480 ms, ...' in the order the stages happened."""
    return ", ".join(f"{stage} {ms} ms"
                     for stage, ms in (session_info.get("timeline") or {}).items())


def _pt(value, default: int) -> int:
    if isinstance(value, (bytes, bytearray)) and value:
        return value[0]
    if isinstance(value, int):
        return value
    return default


def _close_quietly(sock) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


# ─── HAP server and pairing ─────────────────────────────────────────


# Wrong setup codes in a row before the code changes. pyhap itself has no
# limit: without one, the eight digits can be tried one after another.
MAX_PAIR_FAILURES = 10


class _CountingVerifier:
    """pyhap's SRP verifier, telling whether each pair-setup proof (M3, the
    step that checks the setup code) matched."""

    def __init__(self, inner, on_result) -> None:
        self._inner = inner
        self._on_result = on_result

    def verify(self, proof):
        hamk = self._inner.verify(proof)
        self._on_result(hamk is not None)
        return hamk

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _Driver(AccessoryDriver):
    """pyhap's driver, telling us when the pairing changes, and when too many
    wrong setup codes came in a row."""

    def __init__(self, *, on_pairing_change, on_pair_failures=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._on_pairing_change = on_pairing_change
        self._on_pair_failures = on_pair_failures
        self._pair_failures = 0

    def persist(self) -> None:
        """The state file holds the accessory's long-term key and the paired
        controllers: 0600, like the pin file. pyhap writes it through a
        temporary file that is 0600 today; this keeps it so whatever pyhap
        does. Runs in the executor, as pyhap's own persist does."""
        super().persist()
        os.chmod(self.persist_file, 0o600)

    def setup_srp_verifier(self) -> None:
        """pyhap builds a verifier at every pair-setup M1 and checks the code
        with it at M3: ours counts the failures."""
        super().setup_srp_verifier()
        self.srp_verifier = _CountingVerifier(self.srp_verifier, self._pair_attempt)

    def _pair_attempt(self, ok: bool) -> None:
        if ok:
            self._pair_failures = 0
            return
        self._pair_failures += 1
        if self._pair_failures >= MAX_PAIR_FAILURES:
            failures, self._pair_failures = self._pair_failures, 0
            if self._on_pair_failures:
                self._on_pair_failures(failures)

    def pair(self, client_username_bytes, client_public, client_permissions) -> bool:
        ok = super().pair(client_username_bytes, client_public, client_permissions)
        if ok:
            self._pair_failures = 0
            self._on_pairing_change(True)
        return ok

    def unpair(self, client_uuid) -> None:
        super().unpair(client_uuid)
        if not self.state.paired:
            self._on_pairing_change(False)


def _new_pin() -> str:
    digits = f"{secrets.randbelow(10**8):08d}"
    # Apple refuses trivial codes.
    while len(set(digits)) == 1 or digits in ("12345678", "87654321"):
        digits = f"{secrets.randbelow(10**8):08d}"
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


def _save_pin(path: str, pin: str) -> None:
    """Mode 0600: whoever has the code can pair the intercom.

    Written to a private temporary file next to it, then renamed over it: a
    crash never leaves half a file, and there is no moment when the name is
    free for someone else to take."""
    fd, tmp = tempfile.mkstemp(prefix=".homekit_pin_", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pincode": pin}, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def _store_pin(hass: HomeAssistant, path: str, pin: str) -> None:
    """_save_pin in the executor, for a code already in use: a failure is
    logged, since the next restart would bring the previous code back."""
    try:
        await hass.async_add_executor_job(_save_pin, path, pin)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("HomeKit: the new setup code was not saved; after a "
                          "restart the previous one would pair again")


def _load_or_create_pin(path: str) -> str:
    """The setup code stays the same across restarts, until the accessory is
    unpaired (see _rotate_pin)."""
    try:
        with open(path, encoding="utf-8") as f:
            pin = json.load(f)["pincode"]
        os.chmod(path, 0o600)
        return pin
    except (OSError, ValueError, KeyError):
        pass
    pin = _new_pin()
    _save_pin(path, pin)
    return pin


def _rotate_pin(driver: AccessoryDriver) -> str:
    """A new setup code: after the last controller removed the accessory,
    after too many wrong codes, or when HomeKit is turned off unpaired.

    The old code may be in a screenshot or with a previous owner. pyhap reads
    ``driver.state.pincode`` for every pairing attempt and for the QR payload,
    so the new code counts from this moment; the file only carries it across
    restarts.
    """
    pin = _new_pin()
    driver.state.pincode = pin.encode()
    return pin


class _PairingQRView(HomeAssistantView):
    """The pairing QR for the HomeKit options page: administrators only, and
    behind a random token that stops working once the accessory is paired.

    Pairing gives live video, Talk and the gate, which the integration's
    ``allowed_users`` option keeps from other users; the QR must not be
    reachable by them either. The options page shows the image through a
    signed path, which authenticates as the administrator viewing it."""

    url = HOMEKIT_QR_URL
    name = "api:vimar_intercom:homekit_qr"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return web.Response(status=403)
        given = request.query.get("t", "").encode()
        for info in _pairing_data(self._hass).values():
            if info.get("svg") and secrets.compare_digest(given, info["token"].encode()):
                return web.Response(body=info["svg"], content_type="image/svg+xml",
                                    headers={"Cache-Control": "no-store"})
        return web.Response(status=404)


def _qr_svg(uri: str) -> bytes:
    import pyqrcode  # noqa: PLC0415 - only needed for pairing

    buf = io.BytesIO()
    pyqrcode.create(uri).svg(buf, scale=6, module_color="#000", background="#FFF")
    return buf.getvalue()


async def async_setup_homekit(hass: HomeAssistant, entry: ConfigEntry, hub):
    """Publish the video doorbell to HomeKit. Returns the stop function."""
    from homeassistant.components import network, zeroconf  # noqa: PLC0415

    state_path, pin_path = homekit_files(hass, entry.entry_id)
    pincode = await hass.async_add_executor_job(_load_or_create_pin, pin_path)
    address = await network.async_get_source_ip(hass)
    aiozc = await zeroconf.async_get_async_instance(hass)

    holder: dict = {"entry_id": entry.entry_id}
    # pyhap's driver reads its state file and generates keys as it is built:
    # blocking, so in the executor, as Home Assistant's own HomeKit does.
    driver = await hass.async_add_executor_job(partial(
        _Driver,
        on_pairing_change=_pairing_callback(hass, holder, pin_path),
        on_pair_failures=_pair_failures_callback(hass, holder, pin_path),
        loop=hass.loop, address=address, port=HOMEKIT_PORT,
        persist_file=state_path, pincode=pincode.encode(),
        async_zeroconf_instance=aiozc))
    acc = VimarDoorbell(
        driver, ACCESSORY_NAME, hass=hass, hub=hub,
        stream_address=address, serial=entry.entry_id[:8],
        smooth=bool(entry.options.get(CONF_HOMEKIT_SMOOTH, DEFAULT_HOMEKIT_SMOOTH)),
        answer_mode=entry.options.get(CONF_HOMEKIT_ANSWER, DEFAULT_HOMEKIT_ANSWER),
        ring_button=bool(entry.options.get(CONF_HOMEKIT_RING_BUTTON, DEFAULT_HOMEKIT_RING_BUTTON)))
    holder["acc"], holder["driver"] = acc, driver
    await hass.async_add_executor_job(driver.add_accessory, acc)
    try:
        await driver.async_start()
    except BaseException:
        # Half started (the server bound, zeroconf failed): free the port for
        # the next try. Nothing of ours is registered with the hub yet.
        try:
            await driver.async_stop()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("HomeKit: stopping a driver that did not start", exc_info=True)
        raise
    # Only now: a ring or the end of a call reaches an accessory that is up.
    hub.register_ring_callback(acc.ring)
    unregister_video_end = hub.register_video_end_callback(acc.on_video_ended)
    acc._spawn(acc._keep_last_frame())
    _LOGGER.info("HomeKit: video doorbell published on %s:%d (%s, video %s, answer on %s)",
                 address, HOMEKIT_PORT, "paired" if driver.state.paired else "not paired",
                 "re-encoded" if acc._smooth else "direct",
                 "open" if acc._answer_on_open else "Talk")

    hk_data = hass.data.setdefault(HOMEKIT_DATA, {})
    if not hk_data.get("qr_view"):
        hass.http.register_view(_PairingQRView(hass))
        hk_data["qr_view"] = True
    if not driver.state.paired:
        _show_pairing(hass, entry.entry_id, acc, pincode)

    async def _stop() -> None:
        acc._closed = True
        hub.unregister_ring_callback(acc.ring)
        unregister_video_end()
        # The notification and the QR link would otherwise outlive HomeKit.
        _hide_pairing(hass, entry.entry_id)
        for task in list(acc._tasks):
            task.cancel()
        await acc.async_close_views()
        await acc._stop_transcoder()
        await driver.async_stop()
        if not driver.state.paired:
            # Turned off before pairing: the code shown so far stops working.
            await _store_pin(hass, pin_path, _rotate_pin(driver))

    return _stop


def _pairing_callback(hass: HomeAssistant, holder: dict, pin_path: str):
    """What happens when the pairing changes. ``holder`` gets the accessory
    and the driver once they exist (the driver needs the callback first)."""

    def changed(paired: bool) -> None:
        if paired:
            _hide_pairing(hass, holder.get("entry_id"))
            _LOGGER.info("HomeKit: intercom paired")
            return
        # Before anything else: from here on only the new code pairs.
        pincode = _rotate_pin(holder["driver"])
        _LOGGER.info("HomeKit: intercom unpaired, new setup code on the HomeKit options page")
        _show_pairing(hass, holder.get("entry_id"), holder["acc"], pincode)
        hass.async_create_task(_store_pin(hass, pin_path, pincode))

    return changed


def _pair_failures_callback(hass: HomeAssistant, holder: dict, pin_path: str):
    """Too many wrong setup codes in a row: someone may be guessing it."""

    def too_many(failures: int) -> None:
        pincode = _rotate_pin(holder["driver"])
        _LOGGER.warning("HomeKit: %d wrong setup codes in a row, the setup code has been "
                        "changed; the new one is on the HomeKit options page", failures)
        _show_pairing(hass, holder.get("entry_id"), holder["acc"], pincode)
        hass.async_create_task(_store_pin(hass, pin_path, pincode))

    return too_many


def _pairing_data(hass: HomeAssistant) -> dict:
    """entry_id → {"pin", "token", "svg"} of each accessory waiting to be paired."""
    return hass.data.setdefault(HOMEKIT_DATA, {}).setdefault("pairing", {})


def _notification_id(entry_id: str | None) -> str:
    return f"{_PIN_NOTIFICATION}_{entry_id}"


def _show_pairing(hass: HomeAssistant, entry_id: str | None, acc: VimarDoorbell,
                  pincode: str) -> None:
    """Waiting to be paired: the code and the QR go to the HomeKit options
    page, which only administrators can open, and a notification says where
    they are. A notification is shown to every user: with the code in it any
    of them could pair and get video, Talk and the gate, past allowed_users."""
    info = {"pin": pincode, "token": secrets.token_urlsafe(16), "svg": None}
    try:
        info["svg"] = _qr_svg(acc.xhm_uri())
    except Exception:  # noqa: BLE001
        _LOGGER.exception("HomeKit: pairing QR not generated")
    _pairing_data(hass)[entry_id] = info
    if (hass.config.language or "").startswith("it"):
        title = "Citofono: abbinamento HomeKit"
        text = ("Il videocitofono HomeKit aspetta l'abbinamento. Il codice e il QR sono in "
                "**Impostazioni → Dispositivi e servizi → Vimar Intercom → Configura → "
                "HomeKit**, visibile solo agli amministratori.")
    else:
        title = "Intercom: HomeKit pairing"
        text = ("The HomeKit video doorbell is waiting to be paired. The setup code and the "
                "QR code are in **Settings → Devices & services → Vimar Intercom → "
                "Configure → HomeKit**, which only administrators can open.")
    persistent_notification.async_create(
        hass, text, title=title, notification_id=_notification_id(entry_id))


def _hide_pairing(hass: HomeAssistant, entry_id: str | None) -> None:
    """Paired, or HomeKit turned off: no notification, no code, no QR link."""
    persistent_notification.async_dismiss(hass, _notification_id(entry_id))
    hass.data.get(HOMEKIT_DATA, {}).get("pairing", {}).pop(entry_id, None)
