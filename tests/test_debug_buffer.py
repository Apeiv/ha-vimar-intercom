"""The debug buffer is bounded, and the debug view serves its last lines."""
from __future__ import annotations

import asyncio
import logging
import types

import pytest

from custom_components.vimar_intercom import log_buffer
from harness.web import WEB, Request, load_views


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
