"""End-to-end col media vero: la targa manda H.264 (e PCMU) in RTP/SRTP a ritmo reale,
/av passa dall'ffmpeg vero dietro un aiohttp vero, ffprobe conta i fotogrammi che
escono. Solo i casi in cui il bug era nel media; il resto è in test_e2e_sip.py.

    PYTHONPATH=<aiohttp> python -m pytest -m media
"""
from __future__ import annotations

import asyncio

import pytest
from harness.media import decodable_frames
from harness.peer import is_
from harness.rig import Rig, run, wait_until
from harness.web import AvClient

from custom_components.vimar_intercom import av_stream, frame_grabber
from custom_components.vimar_intercom import media_handler as media

pytestmark = pytest.mark.media


def test_cloud_srtp_rollover_e_foto_decodificabili(monkeypatch):
    """Cloud TLS+SRTP, la sequenza RTP parte a ridosso di 65535 (rollover SRTP dentro la
    chiamata). "Vedi esterno", la targa chiude dopo 4 s: fin lì video decodificabile, poi
    /av finisce e non si richiama. Foto dal frame grabber durante la chiamata."""
    async def s():
        async with Rig(monkeypatch, "tls", real_av=True, http=True, srtp=True) as rig:
            await rig.register()
            rig.answer(media_on=True, bye_after=4, seq0=65400)
            ok, msg = await rig.hub.async_call()
            assert ok, msg
            av = AvClient(rig.base).start()
            jpeg = await frame_grabber.wait_frame(8)
            assert jpeg and jpeg[:2] == b"\xff\xd8", "nessuna foto dalla chiamata"
            await wait_until(lambda: rig.hub.status == "idle", 10, "BYE della targa")
            srtp_fail = media.video_proto._srtp_fail
            await asyncio.sleep(2)
            await av.close()
            segs = [decodable_frames(x) for x in av.segments]
            assert segs and segs[0] >= 15, f"segmento senza video: {segs}"
            assert srtp_fail == 0, f"SRTP fail {srtp_fail}"
            assert 503 in av.statuses and len(rig.peer.got(is_("INVITE"))) == 1, av.statuses
    run(s())


def test_anteprima_poi_risposta_senza_riavviare_ffmpeg(monkeypatch):
    """Squillo con anteprima: la targa offre lei (PT 99, non il 96 dell'SDP di ffmpeg).
    /av mostra l'anteprima, si risponde: stesso ffmpeg, video che continua."""
    async def s():
        async with Rig(monkeypatch, real_av=True, http=True) as rig:
            await rig.register()
            rig.peer.pt = 99
            rig.ring()
            r183 = await rig.peer.wait_for(is_(code=183))
            rig.start_media(r183.body)
            av = AvClient(rig.base, reconnect=False).start()
            await wait_until(lambda: av.bytes > 20000, 10, "anteprima su /av")
            proc = av_stream.av_ffmpeg_proc
            assert (await rig.hub.async_answer())[0]
            n = av.bytes
            await asyncio.sleep(3)
            assert av_stream.av_ffmpeg_proc is proc, "ffmpeg di /av riavviato alla risposta"
            assert av.bytes > n + 10000, "/av fermo dopo la risposta"
            await rig.hub.async_hangup()
            await asyncio.wait_for(av.task, 5)
            assert decodable_frames(av.segments[0]) >= 40
            assert not [m for m in rig.peer.got(is_("INVITE")) if m.cid != "ring-1"], "auto-call"
    run(s())


def test_seconda_chiamata_video_e_foto_prima_dell_sps_in_banda(monkeypatch):
    """Dal campo (40515 via cloud): dopo il 200 OK il primo RTP porta PPS+IDR, l'SPS
    solo ogni ~6 s, e la targa chiude dopo ~10 s: ffmpeg di /av («non-existing PPS 0
    referenced») aspettava l'SPS, ~3,5 s di video visti. Seconda chiamata senza mai
    un SPS in banda: /av e la foto devono uscire lo stesso, con SPS/PPS della prima.
    Chiude la targa (come sul campo): il suo media si ferma prima del BYE."""
    async def s():
        async with Rig(monkeypatch, real_av=True, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True, bye_after=3)
            assert (await rig.hub.async_call())[0]
            av = AvClient(rig.base).start()
            await wait_until(lambda: av.bytes > 20000, 10, "video su /av")
            await wait_until(lambda: rig.hub.status == "idle", 5, "BYE della targa")
            await wait_until(lambda: 503 in av.statuses, 5, "/av chiuso a fine chiamata")
            rig.panel_media.aus = [[n for n in au if n[0] & 0x1F != 7] for au in rig.panel_media.aus]
            assert (await rig.hub.async_call())[0]
            jpeg = await frame_grabber.wait_frame(4)
            await wait_until(lambda: rig.hub.status == "idle", 5, "BYE della targa")
            await av.close()
            assert jpeg and jpeg[:2] == b"\xff\xd8", "nessuna foto senza SPS in banda"
            assert len(av.segments) == 2, av.statuses
            n = decodable_frames(av.segments[1])
            assert n >= 15, f"seconda chiamata: {n} fotogrammi decodificabili senza SPS in banda"
    run(s())


def test_encoder_riavviato_a_meta_chiamata_av_continua(monkeypatch):
    """La targa riparte a metà chiamata (SSRC, seq e timestamp nuovi, seq "indietro"):
    ffmpeg scartava tutto come «received too late» e /av restava fermo."""
    async def s():
        async with Rig(monkeypatch, real_av=True, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            assert (await rig.hub.async_call())[0]
            av = AvClient(rig.base, reconnect=False).start()
            await wait_until(lambda: av.bytes > 20000, 10, "video su /av")
            await asyncio.sleep(1)
            n1 = decodable_frames(av.segments[0])
            inv = rig.peer.got(is_("INVITE"))[0]
            rig.start_media(inv.body, seq0=(rig.panel_media.seq - 1000) & 0xFFFF)
            await asyncio.sleep(4)
            n2 = decodable_frames(av.segments[0])
            assert n2 >= n1 + 40, f"/av fermo dopo il riavvio dell'encoder: {n1} → {n2}"
            await rig.hub.async_hangup()
            await av.close()
    run(s())
