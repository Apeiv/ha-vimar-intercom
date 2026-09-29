"""Hub lifecycle races seen in the field.

* A call_ended from the previous call arriving after a new call started used
  to cancel the new call's timers and clear its auto-call flag.
* A view opening while the automatic hang-up was sending its BYE cancelled the
  BYE half way: the panel stayed busy and answered 486 to the next call.
* An audio WebSocket open anywhere stopped every HomeKit view from calling.
* Unloading mid-call left the panel on and the next hub started "in a call".
"""
from __future__ import annotations

import asyncio

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")
sip = hub_mod.sip


@pytest.fixture(autouse=True)
def isolated_sip(monkeypatch):
    """Module globals of sip_client these tests write to: a fresh copy each."""
    from custom_components.vimar_intercom.inventory import DeviceInventory
    monkeypatch.setattr(sip, "DEVICES", DeviceInventory())
    monkeypatch.setattr(sip, "_seen_requests", {})
    monkeypatch.setattr(sip, "_last_sweep", 0.0)


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    monkeypatch.setattr(sip, "in_call", False, raising=False)
    monkeypatch.setattr(sip, "calling", False, raising=False)
    monkeypatch.setattr(sip, "registered", True, raising=False)
    return h


def test_a_late_call_ended_leaves_the_new_call_alone(hub, monkeypatch):
    monkeypatch.setattr(sip, "in_call", True)
    hub._auto_called = True
    cancelled = []
    monkeypatch.setattr(hub, "_cancel_call_timeout", lambda: cancelled.append("timeout"))
    monkeypatch.setattr(hub, "_cancel_keyframe_loop", lambda: cancelled.append("keyframes"))
    asyncio.run(hub._handle_broadcast("call_ended", ""))
    assert hub._auto_called is True and cancelled == []


def test_a_late_call_ended_does_not_close_the_new_calls_stats(hub, monkeypatch):
    monkeypatch.setattr(sip, "in_call", True)
    hub.stats["last_call_end"] = None
    hub._update_stats("call_ended", "")
    assert hub.stats["last_call_end"] is None


def test_a_view_opening_during_the_hang_up_does_not_cut_the_bye(hub, monkeypatch):
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0)
    monkeypatch.setattr(sip, "in_call", True)
    hub._auto_called = True
    events = []

    async def slow_hangup():
        events.append("bye sent")
        await asyncio.sleep(0.3)
        sip.in_call = False
        events.append("bye done")

    async def do_call(target=None):
        events.append("new call")
        return True, "200"

    monkeypatch.setattr(sip, "do_hangup", slow_hangup)
    monkeypatch.setattr(sip, "do_call", do_call)

    async def main():
        hub._hangup_task = asyncio.create_task(hub._delayed_hangup())
        await asyncio.sleep(0.05)          # the BYE is on its way
        await hub.stream_opened()          # a view opens and cancels the task
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert events == ["bye sent", "bye done", "new call"]
    assert hub._hanging_up is False


def _auto_hangup_scenario(hub, monkeypatch, hangup):
    monkeypatch.setattr(hub_mod, "STREAM_HANGUP_DELAY", 0)
    monkeypatch.setattr(sip, "in_call", True)
    hub._auto_called = True
    calls = []

    async def do_call(target=None):
        calls.append(target)
        return True, "200"

    monkeypatch.setattr(sip, "do_hangup", hangup)
    monkeypatch.setattr(sip, "do_call", do_call)
    return calls


def test_a_viewer_leaving_during_the_hang_up_wait_gets_no_call(hub, monkeypatch):
    async def slow_hangup():
        await asyncio.sleep(0.3)
        sip.in_call = False

    calls = _auto_hangup_scenario(hub, monkeypatch, slow_hangup)

    async def main():
        hub._hangup_task = asyncio.create_task(hub._delayed_hangup())
        await asyncio.sleep(0.05)
        opened = asyncio.create_task(hub.stream_opened())
        await asyncio.sleep(0.05)
        await hub.stream_closed()          # the viewer goes away while waiting
        result = await opened
        await asyncio.sleep(0.05)
        return result

    assert asyncio.run(main()) is False
    assert calls == []
    assert hub._auto_called is False


def test_a_view_after_a_local_hang_up_does_not_wait_for_the_bye_answer(hub, monkeypatch):
    """The cloud never answers our BYE: do_hangup ends the call locally, then
    waits 5 s for nothing. The view must not wait for that answer."""
    monkeypatch.setattr(hub_mod, "HANGUP_LOCAL_SETTLE", 0.1)

    async def hangup_without_answer():
        sip.in_call = False
        hub._on_sip_state_change()         # what _set_in_call does for real
        await asyncio.sleep(5)             # the BYE answer that never comes

    calls = _auto_hangup_scenario(hub, monkeypatch, hangup_without_answer)

    async def main():
        hub._hangup_task = asyncio.create_task(hub._delayed_hangup())
        await asyncio.sleep(0.02)
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await hub.stream_opened() is True
        waited = loop.time() - started
        await asyncio.sleep(0)
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            task.cancel()
        return waited

    waited = asyncio.run(main())
    assert waited < 1.0
    assert calls == [None]


def test_a_failing_hang_up_clears_the_flag_and_is_logged(hub, monkeypatch, caplog):
    async def broken_hangup():
        raise OSError("socket gone")

    _auto_hangup_scenario(hub, monkeypatch, broken_hangup)

    async def main():
        await hub._delayed_hangup()
        await asyncio.sleep(0)

    with caplog.at_level("WARNING"):
        asyncio.run(main())
    assert hub._hanging_up is False
    assert hub._hangup_done is not None and hub._hangup_done.is_set()
    assert "socket gone" in caplog.text


def test_an_open_audio_websocket_does_not_stop_a_view_from_calling(hub, monkeypatch):
    calls = []

    async def do_call(target=None):
        calls.append(target)
        return True, "200"

    monkeypatch.setattr(sip, "do_call", do_call)
    hub._has_ws_clients = lambda: True

    async def main():
        assert await hub.stream_opened() is True
        await asyncio.sleep(0)

    asyncio.run(main())
    assert calls == [None]


def test_unloading_mid_call_hangs_up_and_resets_the_sip_state(hub, monkeypatch):
    monkeypatch.setattr(sip, "in_call", True)
    hung_up = []

    async def do_hangup():
        hung_up.append(True)

    async def nothing():
        return None

    monkeypatch.setattr(sip, "do_hangup", do_hangup)
    monkeypatch.setattr(hub_mod.media, "stop_media", nothing)
    monkeypatch.setattr(hub_mod.media, "close_transports", lambda: None)
    monkeypatch.setattr(sip, "writer", None, raising=False)
    monkeypatch.setattr(sip, "_udp_sock", None, raising=False)
    monkeypatch.setattr(sip, "_state_change_callback", None, raising=False)
    sip.pending_incoming["active"] = True
    asyncio.run(hub.async_stop())
    assert hung_up == [True]
    assert sip.in_call is False and sip.registered is False
    assert sip.pending_incoming["active"] is False


def test_the_echo_of_our_own_call_is_declined_as_busy(hub, monkeypatch):
    """603 would tell the PBX to stop the ring everywhere, the Tab included."""
    monkeypatch.setattr(sip, "in_call", True)
    reasons = []

    async def decline(reason="603 Decline"):
        reasons.append(reason)

    monkeypatch.setattr(sip, "do_decline_incoming", decline)

    async def main():
        await hub._handle_broadcast("ring", "")
        await asyncio.sleep(0)

    asyncio.run(main())
    assert reasons == ["486 Busy Here"]


def _requests(monkeypatch):
    sent, broadcasts = [], []

    async def send(msg):
        sent.append(msg)

    async def broadcast(kind, body):
        broadcasts.append(body)

    monkeypatch.setattr(sip, "send", send)
    monkeypatch.setattr(sip, "broadcast", broadcast)

    def deliver(raw):
        asyncio.run(sip._process_request(raw, lambda coro, name: coro.close()))
    return sent, broadcasts, deliver


def _message(cseq=7, cid="m1", body="DOOR;OPEN;1"):
    return ("MESSAGE sip:60999@d SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bK1\r\n"
            f"From: <sip:55001@d>;tag=a\r\nTo: <sip:60999@d>\r\nCall-ID: {cid}\r\n"
            f"CSeq: {cseq} MESSAGE\r\nContent-Length: {len(body)}\r\n\r\n{body}")


def test_a_duplicate_message_is_broadcast_once(monkeypatch):
    sent, broadcasts, deliver = _requests(monkeypatch)
    for _ in range(2):
        deliver(_message())
    assert broadcasts == ["DOOR;OPEN;1"]
    assert len(sent) == 2, "every copy is still answered"


def test_the_same_call_id_with_a_new_cseq_is_a_new_message(monkeypatch):
    _, broadcasts, deliver = _requests(monkeypatch)
    deliver(_message(cseq=7, body="VOICEMAIL;ON"))
    deliver(_message(cseq=8, body="VOICEMAIL;OFF"))
    assert broadcasts == ["VOICEMAIL;ON", "VOICEMAIL;OFF"]


def test_a_duplicate_notify_is_broadcast_once(monkeypatch):
    sent, broadcasts, deliver = _requests(monkeypatch)
    raw = ("NOTIFY sip:60999@d SIP/2.0\r\nVia: SIP/2.0/TLS 1.2.3.4;branch=z9hG4bK2\r\n"
           "From: <sip:55001@d>;tag=a\r\nTo: <sip:60999@d>\r\nCall-ID: n1\r\n"
           "CSeq: 3 NOTIFY\r\nEvent: dnd\r\nContent-Length: 6\r\n\r\nDND;ON")
    deliver(raw)
    deliver(raw)
    assert broadcasts == ["NOTIFY dnd: DND;ON"]
    assert len(sent) == 2


def test_an_old_copy_outside_the_window_is_a_new_request(monkeypatch):
    _, broadcasts, deliver = _requests(monkeypatch)
    deliver(_message())
    for key in sip._seen_requests:
        sip._seen_requests[key] -= sip.DUPLICATE_WINDOW + 1
    deliver(_message())
    assert len(broadcasts) == 2


def test_unloading_forgets_the_seen_requests_keys_and_ring(monkeypatch):
    sip._seen_requests[("MESSAGE", "m1", "7 MESSAGE")] = 1.0
    monkeypatch.setattr(sip, "_local_crypto_key", "k1")
    monkeypatch.setattr(sip, "_local_video_crypto_key", "k2")
    monkeypatch.setattr(sip, "_state_change_callback", None, raising=False)
    sip.pending_incoming.update(cid="c1", caller_uri="sip:55001@d", body="v=0")
    sip.DEVICES.note_binding('<sip:60999@1.1.1.1>;+sip.instance="<urn:uuid:aaa>";expires=900')
    sip.reset_state()
    assert sip._seen_requests == {}
    assert sip._local_crypto_key is None and sip._local_video_crypto_key is None
    assert sip.pending_incoming == sip._PENDING_INITIAL
    assert sip.DEVICES.snapshot()[0]["registered"] is False


def test_a_sip_uri_that_would_split_the_request_line_is_refused(hub):
    ok, msg = asyncio.run(hub.async_send_command("X", target="sip:55001@d\r\nVia: evil"))
    assert not ok


@pytest.mark.parametrize("target", [
    "sip:55001@elsewhere.example", "sip:55001@d;transport=udp", "sip:admin@d",
    "sip:55001@d>", "sip:@d", "sip:55001@d\r\nVia: evil"])
def test_only_a_plant_id_on_the_plant_domain_is_accepted(hub, monkeypatch, target):
    monkeypatch.setattr(hub_mod.R, "SIP_DOMAIN", "d")
    sent = []

    async def message(uri, body, extra_headers=None):
        sent.append(uri)
        return True, "OK"

    monkeypatch.setattr(sip, "do_system_message", message)
    ok, _ = asyncio.run(hub.async_send_command("X", target=target))
    assert not ok and sent == []


@pytest.mark.parametrize("domain_attr", ["SIP_DOMAIN", "LOCAL_DOMAIN", "CLOUD_DOMAIN"])
def test_a_plant_id_on_any_plant_domain_is_sent(hub, monkeypatch, domain_attr):
    for attr in ("SIP_DOMAIN", "LOCAL_DOMAIN", "CLOUD_DOMAIN"):
        monkeypatch.setattr(hub_mod.R, attr, "")
    monkeypatch.setattr(hub_mod.R, domain_attr, "plant.example")
    sent = []

    async def message(uri, body, extra_headers=None):
        sent.append(uri)
        return True, "OK"

    monkeypatch.setattr(sip, "do_system_message", message)
    ok, _ = asyncio.run(hub.async_send_command("X", target="sip:55001@plant.example"))
    assert ok and sent == ["sip:55001@plant.example"]


def test_a_status_refresh_is_not_recorded_as_the_users_command(hub, monkeypatch):
    monkeypatch.setattr(hub_mod.R, "SIP_DOMAIN", "d")
    monkeypatch.setattr(hub_mod.R, "PICG_TARGET", "55001")
    sent = []

    async def message(uri, body, extra_headers=None):
        sent.append((uri, body, extra_headers))
        return True, "OK"

    monkeypatch.setattr(sip, "do_system_message", message)
    hub.stats["last_command_body"] = "VOICEMAIL;ON"
    asyncio.run(hub.async_request_status())
    assert sent == [("sip:55001@d", hub_mod.C.GET_INIT_STATUS, {"Panda": "blue"})]
    assert hub.stats["last_command_body"] == "VOICEMAIL;ON"
