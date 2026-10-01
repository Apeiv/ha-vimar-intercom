"""/av?autocall=0&idle=image: lo standby e l'encoder condiviso, senza targa (il resto,
con lo squillo vero, è in test_e2e_media.py)."""
from __future__ import annotations

import asyncio
import os

import pytest

# FFMPEG in testa al PATH: lo shim di Chocolatey non si lascia uccidere (CONTRIBUTING.md)
from harness.media import FFMPEG

from custom_components.vimar_intercom import av_passive

# Solo i test con ffmpeg vero; quelli con l'encoder finto girano ovunque (anche in CI).
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="servono ffmpeg e ffprobe")


@pytest.fixture(autouse=True)
def _pulito(monkeypatch):
    for k, val in dict(_clients=set(), _task=None, _enc=None, _live=None, _standby=None).items():
        monkeypatch.setattr(av_passive, k, val)


@needs_ffmpeg
def test_standby_png_e_nel_pacchetto_e_si_rende_alla_misura_del_video():
    assert os.path.isfile(av_passive._STANDBY_PNG)
    frame = asyncio.run(av_passive.standby_frame())
    assert len(frame) == av_passive.W * av_passive.H * 3 // 2
    luma = frame[: av_passive.W * av_passive.H]
    assert sum(luma) / len(luma) < 60, "lo standby deve essere scuro"


@needs_ffmpeg
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


class _FakeEnc:
    """Encoder finto: registra il kill, niente ffmpeg vero."""
    returncode = None

    def __init__(self):
        self.killed = False
        self.stdin = self.stdout = None

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        pass


@pytest.fixture
def enc_finto(monkeypatch):
    encs = []

    async def _spawn(*a, **k):
        encs.append(_FakeEnc())
        return encs[-1]

    async def _standby():
        return b""

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(av_passive, "standby_frame", _standby)
    return encs


def test_primo_client_via_prima_che_run_parta_non_lascia_l_encoder_orfano(enc_finto):
    async def run():
        q = await av_passive.subscribe(lambda: False, lambda: None)
        await av_passive.unsubscribe(q)  # _run non ha mai girato: il suo finally non c'e'
        assert enc_finto[0].killed

    asyncio.run(run())


def test_client_dopo_la_morte_dell_encoder_ne_avvia_uno_nuovo(enc_finto):
    async def run():
        async def _morto():
            pass

        av_passive._task = asyncio.create_task(_morto())
        await av_passive._task  # encoder morto da solo: _task resta impostato, ma done
        q = await av_passive.subscribe(lambda: False, lambda: None)
        assert len(enc_finto) == 1
        await av_passive.unsubscribe(q)

    asyncio.run(run())


def test_stop_ferma_encoder_e_client(enc_finto):
    async def run():
        q = await av_passive.subscribe(lambda: False, lambda: None)
        await av_passive.stop()
        assert enc_finto[0].killed and av_passive._task is None and not av_passive._clients
        assert q.get_nowait() is None

    asyncio.run(run())
