"""The debug buffer is bounded, and the debug view serves its last lines."""
from __future__ import annotations

import asyncio
import logging
import types

import pytest
from harness.web import WEB, Request, load_views

from custom_components.vimar_intercom import log_buffer


class _Response:
    def __init__(self, status=200, text="", **kwargs):
        self.status, self.text = status, text


@pytest.fixture
def buffer():
    saved = list(log_buffer.debug_log)
    log_buffer.debug_log.clear()
    yield log_buffer.debug_log
    log_buffer.debug_log.clear()
    log_buffer.debug_log.extend(saved)


def test_the_buffer_keeps_the_last_max_lines(buffer):
    handler = log_buffer.DebugBufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    for i in range(log_buffer.MAX_LINES + 50):
        handler.emit(logging.makeLogRecord({"msg": f"line {i}"}))
    assert buffer.maxlen == log_buffer.MAX_LINES
    assert len(buffer) == log_buffer.MAX_LINES
    assert buffer[0] == "line 50"
    assert buffer[-1] == f"line {log_buffer.MAX_LINES + 49}"


def test_tail_returns_the_last_n_lines(buffer):
    buffer.extend(f"l{i}" for i in range(10))
    assert log_buffer.tail(3) == ["l7", "l8", "l9"]
    assert log_buffer.tail(100) == [f"l{i}" for i in range(10)]


def test_the_debug_view_returns_the_last_n_lines(buffer, monkeypatch):
    buffer.extend(f"l{i}" for i in range(10))
    web = types.SimpleNamespace(**{**vars(WEB), "Response": _Response})
    view = load_views(monkeypatch, web).VimarDebugView()
    response = asyncio.run(view.get(Request(query={"lines": "4"})))
    assert response.text == "l6\nl7\nl8\nl9"


def test_a_record_that_cannot_be_formatted_is_dropped_without_raising(buffer):
    """A log call with arguments that do not match its format string must not
    break the caller: the buffer drops the line instead."""
    handler = log_buffer.DebugBufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(logging.makeLogRecord({"msg": "count %d", "args": ("not a number",)}))
    assert list(buffer) == []


def test_a_record_that_cannot_be_redacted_is_not_forwarded(monkeypatch):
    """Better to lose the line than to show it unredacted in the Home Assistant log."""
    owner = logging.getLogger("vimar_test_forward_owner")
    owner.setLevel(logging.NOTSET)
    forwarded = []
    monkeypatch.setattr(logging.getLogger(), "handle", forwarded.append)
    handler = log_buffer.ForwardToRootHandler(owner)
    handler.emit(logging.makeLogRecord(
        {"msg": "count %d", "args": ("not a number",), "levelno": logging.ERROR}))
    assert forwarded == []
    handler.emit(logging.makeLogRecord({"msg": "plain", "levelno": logging.ERROR}))
    handler.emit(logging.makeLogRecord({"msg": "chatty", "levelno": logging.INFO}))  # below WARNING
    assert [r.msg for r in forwarded] == ["plain"]


def test_install_twice_keeps_one_pair_of_handlers():
    """A reload runs install() again: the handlers are replaced, not stacked,
    or every line would reach the buffer and the Home Assistant log twice."""
    name = "vimar_test_install_twice"
    other = logging.NullHandler()  # someone else's handler stays where it is
    logging.getLogger(name).addHandler(other)
    log = log_buffer.install(name)
    log_buffer.install(name)
    try:
        kinds = [type(h) for h in log.handlers]
        assert other in log.handlers
        assert kinds.count(log_buffer.DebugBufferHandler) == 1
        assert kinds.count(log_buffer.ForwardToRootHandler) == 1
        assert log.level == log_buffer.LEVEL_PIN and log.propagate is False
    finally:
        log.handlers.clear()


def test_both_destinations_mask_the_plant_data(buffer, monkeypatch):
    """#146: the debug buffer and the line forwarded to the HA log, not only the credentials."""
    line = "REGISTER from 192.168.1.23 MyName: Telefono di Anna password=hidden"
    handler = log_buffer.DebugBufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(logging.makeLogRecord({"msg": line}))
    forwarded = []
    monkeypatch.setattr(logging.getLogger(), "handle", forwarded.append)
    owner = logging.getLogger("test_plant_owner")
    owner.setLevel(logging.DEBUG)
    log_buffer.ForwardToRootHandler(owner).emit(logging.makeLogRecord({"msg": line, "levelno": logging.INFO}))
    for out in (buffer[-1], forwarded[0].msg):
        assert "192.168.1.23" not in out and "Anna" not in out and "hidden" not in out
        assert "192.x.x.23" in out


def test_a_traceback_forwarded_to_the_ha_log_is_masked_too(monkeypatch):
    """HA formats exc_info itself: an OSError with the panel's address went out in clear."""
    try:
        raise OSError("connect to ('192.168.1.23', 5060) failed")
    except OSError:
        import sys
        exc_info = sys.exc_info()
    forwarded = []
    monkeypatch.setattr(logging.getLogger(), "handle", forwarded.append)
    owner = logging.getLogger("test_plant_owner")
    owner.setLevel(logging.DEBUG)
    log_buffer.ForwardToRootHandler(owner).emit(
        logging.makeLogRecord({"msg": "send failed", "levelno": logging.ERROR, "exc_info": exc_info,
                               "stack_info": "Stack (most recent call last):\n  peer 10.0.0.7"}))
    record = forwarded[0]
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    assert "10.0.0.7" not in record.msg and "10.x.x.7" in record.msg
    assert "192.168.1.23" not in record.msg and "192.x.x.23" in record.msg and "OSError" in record.msg
