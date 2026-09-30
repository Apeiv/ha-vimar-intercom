"""A view opened right after a hang-up (#41).

On local UDP the panel answers our BYE and is busy until it has; a new call
placed before then got no answer, and /av gave up after 25 s. The view now
waits for the BYE's answer there, and a view's call that gets no answer is
tried once more. The cloud relay never answers a BYE: there nothing changes.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import sip_client as sip
from custom_components.vimar_intercom import runtime as R


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
    assert 0.25 <= waited < 1.0, "until the BYE's answer, not the local end + 0.05 s"
    assert events == ["local end", "bye answered", "new call"]


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
    assert 0.15 <= waited < 1.0
    assert "new call" in events


# ─── one more try when the panel does not answer ─────────────────────────────

def _auto_call(hub, monkeypatch, results, *, viewer_leaves_in_pause=False):
    """A view's auto-call; do_call answers with `results` in turn."""
    monkeypatch.setattr(hub_mod, "AUTO_CALL_RETRY_PAUSE", 0.01)
    calls = []

    async def do_call(target=None, silence_limit=None, answer_timeout=None):
        calls.append(answer_timeout)
        if viewer_leaves_in_pause:
            hub._stream_viewers = 0
        return results[len(calls) - 1]

    monkeypatch.setattr(sip, "do_call", do_call)
    hub._stream_viewers = 1
    hub._auto_called = True
    hub._auto_gen += 1
    asyncio.run(hub._do_auto_call(hub._auto_gen))
    return calls


def test_on_local_udp_an_unanswered_view_call_is_tried_once_more(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    calls = _auto_call(hub, monkeypatch, [(False, f"{sip.NO_ANSWER} (8s)"), (True, "Connesso!")])
    assert calls == [hub_mod.AUTO_CALL_ANSWER_TIMEOUT] * 2
    assert hub._auto_called, "the second try connected"


def test_only_once(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    no = (False, f"{sip.NO_ANSWER} (8s)")
    calls = _auto_call(hub, monkeypatch, [no, no, no])
    assert len(calls) == 2
    assert not hub._auto_called, "a failed auto-call is over"


def test_no_second_try_once_the_viewer_has_left(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    calls = _auto_call(hub, monkeypatch, [(False, f"{sip.NO_ANSWER} (8s)")],
                       viewer_leaves_in_pause=True)
    assert len(calls) == 1


def test_a_refusal_is_not_retried(hub, monkeypatch):
    """Busy (486) or missing (404) is an answer: only silence is retried."""
    monkeypatch.setattr(R, "USE_LOCAL_UDP", True)
    calls = _auto_call(hub, monkeypatch, [(False, "486 Busy Here")])
    assert len(calls) == 1


def test_on_the_cloud_the_view_call_keeps_its_45_s(hub, monkeypatch):
    monkeypatch.setattr(R, "USE_LOCAL_UDP", False)
    calls = _auto_call(hub, monkeypatch, [(False, "Timeout (45s)")])
    assert calls == [None]
