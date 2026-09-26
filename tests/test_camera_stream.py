"""Issue #8: la camera era sempre nera.

Tre cause, tre guardie:
1. le porte dell'ffmpeg AV si sovrapponevano (ffmpeg apre RTP **e** RTCP = RTP+1
   per ogni riga m=): 19201/19202 → l'RTCP del video cadeva sull'audio, bind
   fallito, /av mai partito;
2. il lettore dello stderr usciva appena il processo era già morto, cioè proprio
   quando lo stderr spiegava il perché;
3. la camera dichiarava MJPEG senza `CameraEntityFeature.STREAM`, quindi HA non
   apriva mai `stream_source()`.
"""
from __future__ import annotations

import asyncio
import collections
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from custom_components.vimar_intercom import const as C
from custom_components.vimar_intercom import media_handler as media

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "vimar_intercom"


def _porte_ffmpeg() -> set[int]:
    """Porte che ffmpeg apre davvero: RTP e RTCP (+1) di ogni flusso."""
    porte: list[int] = []
    for p in (C.FFMPEG_AV_VIDEO_PORT, C.FFMPEG_AV_AUDIO_PORT):
        porte += [p, p + 1]
    assert len(porte) == len(set(porte)), f"porte RTP/RTCP sovrapposte: {porte}"
    return set(porte)


def test_porte_av_pari_e_senza_sovrapposizioni():
    assert C.FFMPEG_AV_VIDEO_PORT % 2 == 0
    assert C.FFMPEG_AV_AUDIO_PORT % 2 == 0
    _porte_ffmpeg()


def test_porte_av_non_toccano_quelle_della_chiamata():
    chiamata = {C.RTP_AUDIO_PORT, C.RTP_AUDIO_PORT + 1, C.RTP_VIDEO_PORT, C.RTP_VIDEO_PORT + 1}
    assert not (_porte_ffmpeg() & chiamata)


def test_l_sdp_di_ffmpeg_usa_le_costanti(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_AV_SDP_PATH", str(tmp_path / "av.sdp"))
    sdp = Path(media._write_av_sdp()).read_text()
    porte = {int(p) for p in re.findall(r"^m=\w+ (\d+) ", sdp, flags=re.M)}
    assert porte == {C.FFMPEG_AV_VIDEO_PORT, C.FFMPEG_AV_AUDIO_PORT}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg non installato")
def test_ffmpeg_riesce_a_fare_il_bind(tmp_path, monkeypatch):
    """Prova vera: ffmpeg apre l'SDP e resta in ascolto (con le porte vecchie
    usciva subito con «bind failed»)."""
    monkeypatch.setattr(media, "_AV_SDP_PATH", str(tmp_path / "av.sdp"))
    sdp = media._write_av_sdp()
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-protocol_whitelist", "file,udp,rtp",
         "-i", sdp, "-c", "copy", "-f", "mpegts", "-y", str(tmp_path / "out.ts")],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return  # ancora vivo = porte aperte, in attesa di RTP: giusto
        err = proc.stderr.read().decode(errors="replace")
        pytest.fail(f"ffmpeg è uscito subito (rc={proc.returncode}): {err}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_lo_stderr_si_legge_anche_da_processo_morto():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.write('bind failed\\n')"],
        stderr=subprocess.PIPE)
    proc.wait()
    tail: collections.deque[str] = collections.deque(maxlen=5)
    asyncio.run(media._read_av_ffmpeg_stderr(proc, tail))
    assert list(tail) == ["bind failed"]


def _codice(nome: str) -> str:
    righe = (COMPONENT / nome).read_text(encoding="utf-8").splitlines()
    return "\n".join(r for r in righe if not r.lstrip().startswith("#"))


def test_la_camera_dichiara_lo_stream():
    src = _codice("camera.py")
    assert "_attr_supported_features = CameraEntityFeature.STREAM" in src
    assert "def frontend_stream_type" not in src, "MJPEG forzato: HA non userebbe stream_source()"
    assert "def stream_source" in src


def test_anteprime_dallo_stream_solo_in_chiamata():
    """Da ferma, una miniatura non deve far chiamare la targa."""
    src = _codice("camera.py")
    corpo = src.split("def use_stream_for_stills", 1)[1].split("def ", 1)[0]
    assert "return self._hub.in_call" in corpo
