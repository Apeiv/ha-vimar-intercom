"""/av?autocall=0&idle=image: lo standby e l'encoder condiviso, senza targa (il resto,
con lo squillo vero, è in test_e2e_media.py)."""
from __future__ import annotations

import asyncio
import os

import pytest
# FFMPEG in testa al PATH: lo shim di Chocolatey non si lascia uccidere (CONTRIBUTING.md)
from harness.media import FFMPEG

from custom_components.vimar_intercom import av_passive

pytestmark = pytest.mark.skipif(not FFMPEG, reason="servono ffmpeg e ffprobe")


@pytest.fixture(autouse=True)
def _pulito(monkeypatch):
    for k, val in dict(_clients=set(), _task=None, _live=None, _standby=None).items():
        monkeypatch.setattr(av_passive, k, val)


def test_standby_png_e_nel_pacchetto_e_si_rende_alla_misura_del_video():
    assert os.path.isfile(av_passive._STANDBY_PNG)
    frame = asyncio.run(av_passive.standby_frame())
    assert len(frame) == av_passive.W * av_passive.H * 3 // 2
    luma = frame[: av_passive.W * av_passive.H]
    assert sum(luma) / len(luma) < 60, "lo standby deve essere scuro"


def test_encoder_condiviso_nasce_col_primo_client_e_muore_con_l_ultimo():
    async def run():
        keyframes = []
        a = await av_passive.subscribe(lambda: False, lambda: keyframes.append(1))
        b = await av_passive.subscribe(lambda: False, lambda: keyframes.append(1))
        task = av_passive._task
        got_a = await asyncio.wait_for(a.get(), 10)
        got_b = await asyncio.wait_for(b.get(), 10)
        assert got_a and got_a == got_b and got_a[0] == 0x47, "non è MPEG-TS"
        await av_passive.unsubscribe(a)
        assert av_passive._task is task and not task.done()
        await av_passive.unsubscribe(b)
        assert av_passive._task is None and task.done()
        assert not keyframes, "a riposo non si chiedono keyframe (niente decoder)"

    asyncio.run(asyncio.wait_for(run(), 30))
