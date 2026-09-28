"""Foto di chi suona: salvata nella cartella delle opzioni dopo SNAPSHOT_DELAY."""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")
from custom_components.vimar_intercom import frame_grabber  # noqa: E402

R = hub_mod.R


def _run(monkeypatch, jpeg):
    folder = os.path.join(tempfile.mkdtemp(), "citofono")
    monkeypatch.setattr(R, "SNAPSHOT_DIR", folder)
    monkeypatch.setattr(R, "SNAPSHOT_DELAY", 0)

    async def _wait(timeout=6):
        return jpeg

    monkeypatch.setattr(frame_grabber, "wait_frame", _wait)
    asyncio.run(hub_mod.VimarIntercomHub()._save_ring_photo("squillo_20260927_101500.jpg"))
    return folder


def test_salva_foto_con_data_e_ultimo_squillo(monkeypatch):
    folder = _run(monkeypatch, b"\xff\xd8JPEG\xff\xd9")
    assert sorted(os.listdir(folder)) == ["squillo_20260927_101500.jpg", "ultimo_squillo.jpg"]


def test_senza_anteprima_non_salva(monkeypatch):
    folder = _run(monkeypatch, None)
    assert not os.path.exists(folder)
