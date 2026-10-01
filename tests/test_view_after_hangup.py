"""A view opened right after a hang-up (#41).

On local UDP the panel answers our BYE and is busy until it has; a new call
placed before then got no answer, and /av gave up after 25 s. The view now
waits for the BYE's answer there, and a view's call that gets no answer is
tried once more. The cloud relay never answers a BYE: there nothing changes.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import runtime as R
from custom_components.vimar_intercom import sip_client as sip


@pytest.fixture
def hub(monkeypatch):
    h = hub_mod.VimarIntercomHub()
    monkeypatch.setattr(h, "_touch", lambda: None)
    monkeypatch.setattr(sip, "in_call", False, raising=False)
    monkeypatch.setattr(sip, "calling", False, raising=False)
    monkeypatch.setattr(sip, "registered", True, raising=False)
    monkeypatch.setattr(sip, "ringing", lambda cid=None: False)
    return h


def _view_during_hang_up(hub, monkeypatch, bye_answer_after):
    """The Hang up button, then a view at once. The call ends locally at
    once; the BYE's answer comes `bye_answer_after` seconds later."""
    monkeypatch.setattr(hub_mod, "HANGUP_LOCAL_SETTLE", 0.05)
    monkeypatch.setattr(sip, "in_call", True)
    events = []

    async def do_hangup(on_local_end=None):
        monkeypatch.setattr(sip, "in_call", False)
        hub._on_sip_state_change()            # what _set_in_call does for real
        if on_local_end:
            on_local_end()
        events.append("local end")
        await asyncio.sleep(bye_answer_after)
        events.append("bye answered")

    async def do_call(target=None, **_kw):
        events.append("new call")
        return True, "200"

    monkeypatch.setattr(sip, "do_hangup", do_hangup)
    monkeypatch.setattr(sip, "do_call", do_call)

    async def main():
        hang = asyncio.create_task(hub.async_hangup())
        await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await hub.stream_opened() is True
        waited = loop.time() - started
        await hang
        await asyncio.sleep(0.05)
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            task.cancel()
        return waited

    return asyncio.run(main()), events


def test_on_local_udp_a_view_waits_for_the_panel_to_answer_the_bye(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    waited, events = _view_during_hang_up(hub, monkeypatch, bye_answer_after=0.3)
    assert waited < 1.0
    assert events == ["local end", "bye answered", "new call"], "the BYE's answer first"


def test_on_the_cloud_a_view_waits_only_for_the_local_end(hub, monkeypatch):
    """The relay never answers the BYE: waiting for it cost 5 s every time."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    waited, events = _view_during_hang_up(hub, monkeypatch, bye_answer_after=0.3)
    assert waited < 0.2
    assert events == ["local end", "new call"], "called before the BYE's answer"


def test_on_local_udp_a_bye_never_answered_still_frees_the_view(hub, monkeypatch):
    """The wait is bounded: HANGUP_SETTLE, then the view calls anyway."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(hub_mod, "HANGUP_SETTLE", 0.2)
    waited, events = _view_during_hang_up(hub, monkeypatch, bye_answer_after=5)
    assert waited < 1.0
    assert events == ["local end", "new call"], "called at HANGUP_SETTLE, not after 5 s"


# ─── one more try when the panel does not answer ─────────────────────────────

def _auto_call(hub, monkeypatch, results, *, during_pause=None):
    """A view's auto-call; do_call answers with `results` in turn.
    `during_pause` runs 5 ms into the 50 ms pause between the two tries."""
    monkeypatch.setattr(hub_mod, "LOCAL_UDP_SETTLE", 0.05)
    calls = []

    async def do_call(target=None, silence_limit=None, answer_timeout=None, ring_timeout=None):
        calls.append(answer_timeout)
        if during_pause and len(calls) == 1:
            asyncio.get_running_loop().call_later(0.005, during_pause)
        return results[min(len(calls), len(results)) - 1]   # the last one repeats

    monkeypatch.setattr(sip, "do_call", do_call)
    hub._stream_viewers = 1
    hub._auto_called = True
    hub._auto_gen += 1
    asyncio.run(hub._do_auto_call(hub._auto_gen))
    return calls


NO = (False, f"{sip.NO_ANSWER} (8s)")


def test_on_local_udp_an_unanswered_view_call_is_tried_once_more(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    calls = _auto_call(hub, monkeypatch, [NO, (True, "Connesso!")])
    assert calls == [hub_mod.LOCAL_UDP_ANSWER_TIMEOUT] * 2
    assert hub._auto_called, "the second try connected"


def test_the_retries_stop_when_the_budget_runs_out(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(hub_mod, "LOCAL_UDP_CALL_BUDGET", 0.3)
    monkeypatch.setattr(hub_mod, "LOCAL_UDP_MIN_TRY", 0.05)
    calls = _auto_call(hub, monkeypatch, [NO])
    assert 2 <= len(calls) < 10, "a few tries, then it gives up"
    assert not hub._auto_called, "a failed auto-call is over"


def test_the_other_face_of_the_stuck_panel_gets_another_try(hub, monkeypatch):
    """#44 on a 40507: the first try rang (180) and was never answered, the retry
    got only 100 Trying. A single retry failed the view; a third try connects."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    rang = (False, f"{sip.NO_ANSWER} (8s)")
    silent = (False, f"{sip.NO_ANSWER} (no 180 after 3s)")
    calls = _auto_call(hub, monkeypatch, [rang, silent, (True, "Connesso!")])
    assert len(calls) == 3 and hub._auto_called


def test_the_tries_fit_in_the_budget(hub, monkeypatch):
    """Each try's answer timeout is capped by what is left, and no try is started
    without room for an answer: 6 + 6 + 6 s within the 21 s."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(hub_mod, "LOCAL_UDP_SETTLE", 0.01)
    clock = {"t": 0.0}
    monkeypatch.setattr(hub_mod, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))
    calls = []

    async def do_call(target=None, silence_limit=None, answer_timeout=None, ring_timeout=None):
        calls.append(answer_timeout)
        clock["t"] += answer_timeout             # each try runs out its timeout
        return NO

    monkeypatch.setattr(sip, "do_call", do_call)
    hub._stream_viewers, hub._auto_called = 1, True
    hub._auto_gen += 1
    asyncio.run(hub._do_auto_call(hub._auto_gen))
    assert calls == [6.0, 6.0, 6.0]
    assert sum(calls) <= hub_mod.LOCAL_UDP_CALL_BUDGET


def test_no_second_try_once_the_viewer_has_left_during_the_pause(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    calls = _auto_call(hub, monkeypatch, [NO],
                       during_pause=lambda: setattr(hub, "_stream_viewers", 0))
    assert len(calls) == 1


def test_no_second_try_when_a_ring_arrives_during_the_pause(hub, monkeypatch):
    """The view follows the ring's early media instead: a call now would
    replace the ring's media and keys."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    ringing = {"now": False}
    monkeypatch.setattr(sip, "ringing", lambda cid=None: ringing["now"])
    calls = _auto_call(hub, monkeypatch, [NO], during_pause=lambda: ringing.update(now=True))
    assert len(calls) == 1


def test_no_second_try_for_an_auto_call_a_newer_one_replaced(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    calls = _auto_call(hub, monkeypatch, [NO],
                       during_pause=lambda: setattr(hub, "_auto_gen", hub._auto_gen + 1))
    assert len(calls) == 1


def test_the_camera_fallback_call_keeps_the_answer_timeout(hub, monkeypatch):
    """The 404 fallback to the panel that last rang stays inside /av's 25 s."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(hub, "_camera_fallback", lambda msg: "55002" if msg.startswith("404") else None)
    monkeypatch.setattr(hub, "_learn_camera_target", lambda alt: None)
    calls = _auto_call(hub, monkeypatch, [(False, "404 Not Found"), (True, "Connesso!")])
    assert calls == [hub_mod.LOCAL_UDP_ANSWER_TIMEOUT] * 2


def test_a_refusal_is_not_retried(hub, monkeypatch):
    """Busy (486) or missing (404) is an answer: only silence is retried."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    calls = _auto_call(hub, monkeypatch, [(False, "486 Busy Here")])
    assert len(calls) == 1


def test_on_the_cloud_the_view_call_keeps_its_45_s(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    calls = _auto_call(hub, monkeypatch, [(False, "Timeout (45s)")])
    assert calls == [None]


# ─── the card's "view outside" and the call buttons: explicit calls (#44) ────

def _explicit(hub, monkeypatch, results, target=None, *, during_pause=None):
    monkeypatch.setattr(hub_mod, "LOCAL_UDP_SETTLE", 0.05)
    calls, pending = [], []

    async def do_call(target=None, silence_limit=None, answer_timeout=None, ring_timeout=None):
        calls.append((target, answer_timeout, ring_timeout))
        if during_pause and len(calls) == 1:
            def check():
                pending.append(hub._call_in_view)
                during_pause()
            asyncio.get_running_loop().call_later(0.005, check)
        return results[len(calls) - 1]

    monkeypatch.setattr(sip, "do_call", do_call)
    ok, msg = asyncio.run(hub.async_call(target))
    return ok, calls, pending


def test_the_cards_view_outside_is_retried_like_a_views_auto_call(hub, monkeypatch):
    """#44: the card calls the panel itself (async_call), so #41's retry never ran:
    the panel's proxy said 100 Trying, the panel never sent its 180, and /av gave
    up after 25 s. A 100 with no 180 is given up after 3 s and tried once more."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    ok, calls, pending = _explicit(hub, monkeypatch, [NO, (True, "Connesso!")],
                                   during_pause=lambda: None)
    assert ok
    timeouts = (hub_mod.LOCAL_UDP_ANSWER_TIMEOUT, hub_mod.LOCAL_UDP_RING_TIMEOUT)
    assert calls == [(None, *timeouts)] * 2
    assert pending == [True], "during the pause /av and a new view see a call coming"


def test_an_explicit_call_to_the_video_panel_by_its_address_is_retried(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(R, "INTERCOM", "sip:55100@plant.example.test")
    monkeypatch.setattr(hub_mod, "sip_uri", lambda t: f"sip:{t}@plant.example.test")
    ok, calls, _ = _explicit(hub, monkeypatch, [NO, (True, "Connesso!")], target="55100")
    assert ok and len(calls) == 2


def test_a_hang_up_during_the_pause_stops_the_retry(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)

    def hang_up():
        hub._explicit_gen += 1                  # what async_hangup does
    ok, calls, _ = _explicit(hub, monkeypatch, [NO], during_pause=hang_up)
    assert not ok and len(calls) == 1


def test_a_call_to_a_flat_keeps_its_45_s_and_no_retry(hub, monkeypatch):
    """A person answers a flat or the switchboard: it may ring for long."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(R, "INTERCOM", "sip:55100@plant.example.test")
    monkeypatch.setattr(hub_mod, "sip_uri", lambda t: f"sip:{t}@plant.example.test")
    ok, calls, _ = _explicit(hub, monkeypatch, [(False, "Timeout (45s)")], target="60001")
    assert calls == [("sip:60001@plant.example.test", None, None)]


def test_on_the_cloud_an_explicit_call_is_unchanged(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    ok, calls, _ = _explicit(hub, monkeypatch, [(False, "Timeout (45s)")])
    assert calls == [(None, None, None)]


def test_async_hangup_stops_a_pending_retry(hub, monkeypatch):
    before = hub._explicit_gen

    async def do_hangup(on_local_end=None):
        if on_local_end:
            on_local_end()

    monkeypatch.setattr(sip, "do_hangup", do_hangup)
    asyncio.run(hub.async_hangup())
    assert hub._explicit_gen == before + 1


# ─── letting the panel settle after a dialog (#44, 35 openings on a 40507) ────

def _first_try_delay(hub, monkeypatch, ended_ago, *, local=True):
    """How long the first INVITE waits when the last dialog ended `ended_ago`
    seconds before the call, and what /av saw meanwhile."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", local)
    monkeypatch.setattr(hub_mod, "LOCAL_UDP_SETTLE", 0.3)
    seen = {}

    async def do_call(target=None, silence_limit=None, answer_timeout=None, ring_timeout=None):
        seen["at"] = asyncio.get_running_loop().time()
        return True, "Connesso!"

    monkeypatch.setattr(sip, "do_call", do_call)

    async def main():
        loop = asyncio.get_running_loop()
        hub._dialog_ended_at = hub_mod.time.monotonic() - ended_ago
        started = loop.time()
        call = asyncio.create_task(hub.async_call())
        await asyncio.sleep(0.05)
        seen["coming"] = hub._call_in_view
        assert (await call)[0]
        return seen["at"] - started

    return asyncio.run(main()), seen


def test_a_call_right_after_a_dialog_waits_for_the_panel_to_settle(hub, monkeypatch):
    """An INVITE less than 1 s after the previous dialog ended was rung and never
    answered, 8 times out of 8; from 5 s on, almost always answered."""
    waited, seen = _first_try_delay(hub, monkeypatch, ended_ago=0.1)
    assert 0.15 <= waited < 1.0, "until LOCAL_UDP_SETTLE after the end, not at once"
    assert seen["coming"], "meanwhile /av keeps waiting and no view places its own call"


def test_a_call_long_after_the_last_dialog_goes_at_once(hub, monkeypatch):
    waited, _ = _first_try_delay(hub, monkeypatch, ended_ago=10)
    assert waited < 0.1


def test_on_the_cloud_a_call_never_waits_to_settle(hub, monkeypatch):
    waited, _ = _first_try_delay(hub, monkeypatch, ended_ago=0.1, local=False)
    assert waited < 0.1


def test_the_next_try_settles_after_our_own_cancelled_try(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    monkeypatch.setattr(hub_mod, "LOCAL_UDP_SETTLE", 0.2)
    at = []

    async def do_call(target=None, silence_limit=None, answer_timeout=None, ring_timeout=None):
        at.append(asyncio.get_running_loop().time())
        return NO if len(at) == 1 else (True, "Connesso!")

    monkeypatch.setattr(sip, "do_call", do_call)
    ok, _ = asyncio.run(hub.async_call())
    assert ok and len(at) == 2
    assert at[1] - at[0] >= 0.18, "the panel settles after our CANCEL too"


def test_a_call_that_ends_on_the_line_starts_the_settle_clock(hub, monkeypatch):
    monkeypatch.setattr(sip, "in_call", True)
    hub._on_sip_state_change()
    monkeypatch.setattr(sip, "in_call", False)
    before = hub_mod.time.monotonic()
    hub._on_sip_state_change()
    assert hub._dialog_ended_at >= before


def test_a_settle_before_the_first_try_leaves_room_for_a_second(hub, monkeypatch):
    """#44 on the 40507: the call had to settle 4.9 s after a hang-up, its first try
    (with a 407 round trip) rang and was never answered, and 21 s counted from the
    tap left no room for another try. The budget counts from the first INVITE."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    clock = {"t": 0.0}
    monkeypatch.setattr(hub_mod, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))
    calls = []

    async def settle(still_wanted, last_try):
        clock["t"] += hub_mod.LOCAL_UDP_SETTLE        # right after a dialog, every time
        return still_wanted()

    async def do_call(target=None, silence_limit=None, answer_timeout=None, ring_timeout=None):
        calls.append(answer_timeout)
        clock["t"] += answer_timeout + 0.5            # rang, no answer, plus a 407 round trip
        return (False, f"{sip.NO_ANSWER} (6s)") if len(calls) == 1 else (True, "Connesso!")

    monkeypatch.setattr(hub, "_settle", settle)
    monkeypatch.setattr(sip, "do_call", do_call)
    ok, _ = asyncio.run(hub.async_call())
    assert ok and len(calls) == 2, "the second try happens"
    assert clock["t"] <= hub_mod.LOCAL_UDP_SETTLE + hub_mod.LOCAL_UDP_CALL_BUDGET + 1


def test_no_try_runs_past_av_s_wait(hub, monkeypatch):
    """With the budget counted from the first INVITE, quick "no 180" tries after a
    settle could leave room for a try ending 26 s after the tap, past /av's 25 s:
    a call answered then is one nobody watches. Every try ends by the tap limit."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    clock = {"t": 0.0}
    monkeypatch.setattr(hub_mod, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))
    ends = []

    async def settle(still_wanted, last_try):
        clock["t"] += hub_mod.LOCAL_UDP_SETTLE
        return still_wanted()

    async def do_call(target=None, silence_limit=None, answer_timeout=None, ring_timeout=None):
        quick = len(ends) < 2                    # two "100, no 180", then one that rings
        clock["t"] += min(ring_timeout, answer_timeout) if quick else answer_timeout
        ends.append(clock["t"])
        return False, f"{sip.NO_ANSWER} (6s)"

    monkeypatch.setattr(hub, "_settle", settle)
    monkeypatch.setattr(sip, "do_call", do_call)
    ok, _ = asyncio.run(hub.async_call())
    assert not ok and len(ends) >= 2
    assert max(ends) < 25, ends              # /av's wait; without the tap limit: 26 s
