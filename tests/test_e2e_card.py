"""La card vera nel browser (Chromium e WebKit = Safari/iPhone) contro l'impianto di
prova: hub, sip_client, /av, /audio_ws e /rings veri, targa finta (harness.card).

Il video dal vivo ha due strade: WebCodecs sui NAL del WebSocket (Chromium, Safari
16.4+) e lo stream di HA (/av) dove WebCodecs manca. I test sullo stream di HA aprono la
card con webcodecs=False; il bench e la prova di ripiego stanno in fondo.

    PYTHONPATH=<aiohttp> python -m pytest -m browser
"""
from __future__ import annotations

import asyncio
import re
import struct
import time

import pytest
from harness import media as hm
from harness.card import Card, engine  # noqa: F401  (fixture)
from harness.peer import answer_200, is_
from harness.rig import Rig, run, wait_until

from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import ring_log
from custom_components.vimar_intercom import runtime as R

pytestmark = pytest.mark.browser

IDLE = "info().pill === 'Pronto' && info().video === 'auto'"
# Il canvas riempie il riquadro 4:3 e, in "overlay", i tasti stanno sopra il video e ricevono il tocco.
GEOMETRY = """(() => { const r = card.shadowRoot, q = (s) => r.querySelector(s).getBoundingClientRect();
  const m = q('.media'), cv = q('#video > canvas'), o = q('#open');
  const top = r.elementFromPoint(o.x + o.width / 2, o.y + o.height / 2);
  return { canvas_fills_media: Math.abs(cv.width - m.width) < 1 && Math.abs(cv.height - m.height) < 1,
           ratio_4_3: Math.abs(m.width / m.height - 4 / 3) < 0.02,
           open_over_video: o.y >= m.y && o.y + o.height <= m.y + m.height,
           open_on_top: !!top?.closest('#open') }; })()"""


async def decline(peer, inv):
    peer.reply(inv, 100, "Trying")
    peer.reply(inv, 603, "Decline")


def test_dashboard_a_riposo_non_chiama_mai(monkeypatch, engine):  # noqa: F811
    """Requisito: aprire (o ricaricare) una dashboard con la card a riposo non chiama la
    targa e non apre /av. Né quando il citofono passa da offline a pronto, né con stati
    strani (riavvio di HA), né quando finisce uno squillo, né dopo una visione chiusa."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, webcodecs=False) as c:  # aperta PRIMA della registrazione
                await rig.register()                     # offline → idle
                await c.until(IDLE)
                for st in ("unavailable", "unknown", "offline", None):
                    rig.state_override = st
                    await asyncio.sleep(0.3)
                for _ in range(2):                       # cambio dashboard / ricarica
                    await c.open()
                    await asyncio.sleep(2)
                t = await c.T()
                assert not rig.peer.got(is_("INVITE")), "chiamata partita da sola"
                assert t["av"] == [] and t["ws"] == 0 and "live" not in t["created"], t
                assert rig.hub._stream_viewers == 0 and not rig.services
                rig.ring()                               # squillo: anteprima sì, chiamate no
                await rig.peer.wait_for(is_(code=183))
                await c.until("T.av.includes(200)")
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until(IDLE)
                await asyncio.sleep(3)
                assert not [m for m in rig.peer.got(is_("INVITE")) if m.cid != "ring-1"]
                assert (await c.T())["live"] == 0 and rig.hub._stream_viewers == 0
                await c.tap("view")                      # visione chiusa con "Riaggancia"
                await c.until("info().pill === 'In chiamata'")
                await c.tap("hangup")
                await c.until(IDLE)
                n = len(rig.peer.got(is_("INVITE")))
                await c.open()
                await asyncio.sleep(3)
                assert len(rig.peer.got(is_("INVITE"))) == n
                assert rig.hub._stream_viewers == 0 and rig.hub.status == "idle"
                assert rig.services == ["vimar_intercom.call", "vimar_intercom.hangup"]
                assert not (await c.T())["errors"]
    run(s(), 120)


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_squillo_rispondi_parla_riaggancia(monkeypatch, engine):  # noqa: F811
    """Squillo → anteprima dal vivo → "Rispondi" (doppio tocco: una risposta sola) col
    microfono → voce nei due sensi a 20 ms → "Riaggancia": microfono e WS chiusi."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, webcodecs=False) as c:
                rig.ring("ring-card")
                await c.until("info().pill === 'Suonano alla porta' && T.av.includes(200) && T.avBytes > 0")
                assert (await c.info())["talk"] == "Rispondi"
                await c.page.evaluate("T.gumDelay = 500")
                await c.tap("talk")
                await c.tap("talk")                      # doppio tocco durante il permesso
                await c.until("info().pill === 'In chiamata' && T.ws === 1")
                ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-card"))
                rig.peer.request("ACK", "ring-card", 1, "pnl", to_tag=ok200.h("to").split("tag=")[1])
                for i in range(100):                     # la targa parla
                    rig.peer.rtp_audio.sendto(struct.pack("!BBHII", 0x80, 0, i, i * 160, 1234) + b"\x7f" * 160,
                                              ("127.0.0.1", media.RTP_AUDIO_PORT))
                    await asyncio.sleep(0.02)
                await c.until("T.rx > 20")               # la targa si sente
                await wait_until(lambda: len(rig.peer.audio_rx) > 20, 5, "voce alla targa")
                assert {len(p) - 12 for p in rig.peer.audio_rx} == {160}
                t = await c.T()
                assert rig.services == ["vimar_intercom.answer"], rig.services
                assert all(f[0] == 2 for f in t["frames"]), t["frames"]
                await c.tap("hangup")
                await rig.peer.wait_for(is_("BYE", cid="ring-card"))
                await c.until(IDLE + " && info().audio === 'off' && T.wsClosed === 1")
                n = len(rig.peer.audio_rx)
                await asyncio.sleep(0.5)
                assert len(rig.peer.audio_rx) == n, "voce ancora in uscita dopo il riaggancio"
                assert not (await c.T())["errors"]
    run(s())


def test_vedi_esterno_finisce_col_bye_della_targa(monkeypatch, engine):  # noqa: F811
    """La targa chiude "Vedi esterno" dopo ~10 s: la card torna subito a riposo (video
    chiuso, nessun errore) e non si richiama. Per guardare ancora si ripreme il tasto."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, webcodecs=False) as c:
                await c.tap("view")
                await c.until("info().pill === 'In chiamata' && T.av.includes(200)")
                await asyncio.sleep(0.5)
                rig.bye(rig.peer.got(is_("INVITE"))[0])
                await c.until(IDLE, 3)
                await asyncio.sleep(2)
                assert len(rig.peer.got(is_("INVITE"))) == 1, "richiamata dopo il BYE"
                assert not (await c.T())["errors"] and "non ha accettato" not in (await c.info())["err"]
                await c.tap("view")
                await c.until("info().pill === 'In chiamata'")
                assert len(rig.peer.got(is_("INVITE"))) == 2
    run(s())


def test_video_dal_vivo_solo_con_squillo_o_chiamata(monkeypatch, engine):  # noqa: F811
    """Stato → riquadro video: dal vivo in ringing/calling/in_call, altrimenti fermo;
    passaggi rapidissimi: vince l'ultimo. Nessun servizio chiamato da solo."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, webcodecs=False) as c:
                await c.until(IDLE)
                for st, view in (("calling", "live"), ("in_call", "live"), ("calling", "live"),
                                 ("idle", "auto"), ("ringing", "live"), ("offline", "auto")):
                    rig.state_override = st
                    await c.until(f"info().video === '{view}'", 3)
                await c.page.evaluate("['idle','in_call','idle','ringing'].forEach("
                                      "(s) => card.hass = {...card._hass, states: {...card._hass.states,"
                                      " 'sensor.vimar_intercom_intercom_stato': {state: s}}})")
                rig.state_override = "ringing"
                await asyncio.sleep(0.5)
                assert (await c.info())["video"] == "live"
                assert not rig.services and not (await c.T())["errors"]
                assert not rig.peer.got(is_("INVITE"))
    run(s())


def test_rispondi_ma_lo_squillo_finisce_durante_il_permesso(monkeypatch, engine):  # noqa: F811
    """iPhone, primo "Rispondi": il permesso del microfono resta a schermo e intanto ha
    risposto il Tab (CANCEL). Concesso il permesso, la card non chiama la targa."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine) as c:
                if not await c.page.evaluate("!!window.AudioContext"):
                    pytest.skip("questo motore headless non ha Web Audio")
                rig.ring()
                await c.until("info().talk === 'Rispondi'")
                await c.page.evaluate("T.gumDelay = 1500")
                await c.tap("talk")
                await asyncio.sleep(0.3)
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until(IDLE)
                await asyncio.sleep(2)
                assert not rig.services, rig.services
                assert not [m for m in rig.peer.got(is_("INVITE")) if m.cid != "ring-1"]
                t = await c.T()  # niente audio; il WS video dello squillo (WebCodecs) si è chiuso
                assert (await c.info())["audio"] == "off" and t["ws"] == t["wsClosed"], t
    run(s())


def test_rispondi_senza_https_solo_video(monkeypatch, engine):  # noqa: F811
    """HA in HTTP: niente microfono, ma "Rispondi" risponde (solo video)."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine, insecure=True, webcodecs=False) as c:
                rig.ring("ring-http")
                await c.until("info().talk === 'Rispondi' && T.av.includes(200)")
                await c.tap("talk")
                await c.until("info().pill === 'In chiamata'")
                assert (await c.T())["ws"] == 0 and (await c.info())["video"] == "live"
                ok200 = await rig.peer.wait_for(is_(code=200, cid="ring-http"))
                rig.peer.request("BYE", "ring-http", 2, "pnl", to_tag=ok200.h("to").split("tag=")[1])
                await c.until(IDLE)
                assert not (await c.T())["errors"]
    run(s())


def test_apri_doppio_tocco(monkeypatch, engine):  # noqa: F811
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine) as c:
                await c.tap("open")
                await asyncio.sleep(0.5)
                assert not rig.peer.got(is_("MESSAGE")), "un tocco solo ha aperto"
                await c.tap("open")
                m = await rig.peer.wait_for(is_("MESSAGE"))
                assert m.body == "OPEN_2F" and m.h("panda") == "command"
                await c.until("card.shadowRoot.querySelector('#open .lbl').textContent === 'Aperto'")
    run(s())


def test_chiamata_rifiutata_la_card_lo_dice_e_si_chiude(monkeypatch, engine):  # noqa: F811
    """Dal campo: la targa rifiuta (603) "Vedi esterno". La card non resta su
    "Collegamento…" col riquadro video vuoto: si richiude e lo dice."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.peer.on_invite = decline
            async with Card(rig, engine) as c:
                await c.tap("view")                           # "Vedi esterno" → 603
                await c.until(IDLE + " && info().err.includes('603')", 8)
                assert not (await c.page.evaluate("card.shadowRoot.getElementById('view').disabled"))
                await c.tap("view")
                await c.until("info().pill.startsWith('Collegamento') || info().pill === 'Pronto'")
                await c.until(IDLE, 8)
    run(s())


def test_socchiusa_e_ultimi_squilli_con_foto(monkeypatch, engine, tmp_path):  # noqa: F811
    """A riposo solo stato e tasti (riquadro video chiuso), si apre con lo squillo;
    nel cassetto, gli ultimi squilli dal registro, con la foto da un percorso firmato.
    Da fermo il cassetto si apre dalla foto dell'ultimo squillo (= tasto cronologia),
    sopra la foto grande, senza chiamare la targa."""
    ring_log.update_ring_log(str(tmp_path), lambda r: r.extend([
        {"time": "2026-09-27T09:00:00+02:00", "photo": None, "outcome": "missed"},
        {"time": "2026-09-27T10:15:00+02:00", "photo": "squillo_20260927_101500.jpg", "outcome": "away"}]))
    (tmp_path / "squillo_20260927_101500.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    js = """(() => { const r = card.shadowRoot;
      return { media: r.querySelector('.media').getBoundingClientRect().height,
               hist: [...r.querySelectorAll('.hist button')].map(b => [b.textContent, b.querySelector('img')?.getAttribute('src')]) }; })()"""

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            await rig.register()
            async with Card(rig, engine) as c:
                await c.page.emulate_media(reduced_motion="reduce")
                await c.until("card.shadowRoot.querySelectorAll('.hist button').length === 2")
                st = await c.page.evaluate(js)
                assert st["media"] == 0, st
                src = st["hist"][0][1]
                assert re.fullmatch(r"/api/vimar_intercom/rings/squillo_20260927_101500\.jpg\?v=\d+&authSig=x", src), st
                assert "Messaggio di assenza" in st["hist"][0][0] and "Nessuna risposta" in st["hist"][1][0]
                assert await c.page.evaluate(f"fetch('{src}').then(r => r.status)") == 200
                assert await c.page.evaluate("fetch('/api/vimar_intercom/rings/ultimo_squillo.jpg')"
                                             ".then(r => r.status)") == 404
                for q, n in (("0", 1), ("1", 1), ("x", 2), ("999", 2)):   # ?limit= fra 1 e 50, 10 se strano
                    got = await c.page.evaluate(f"fetch('/api/vimar_intercom/rings?limit={q}').then(r => r.json())")
                    assert len(got) == n, (q, got)
                assert await c.page.evaluate("card.shadowRoot.querySelector('#photo img').getAttribute('src')") == src
                await c.tap("photo")                     # cassetto da fermo: foto, non video
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height > 100")
                assert await c.page.evaluate("card.shadowRoot.querySelector('.still').getAttribute('src')") == src
                assert (await c.T())["live"] == 0 and not rig.services and not rig.peer.got(is_("INVITE"))
                await c.tap("photo")
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height === 0")
                rig.ring()  # squillo vero: senza WebCodecs il riquadro apre /av, che a hub fermo chiamerebbe
                await rig.peer.wait_for(is_(code=183))
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height > 100")
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height === 0")
                assert not rig.services
    run(s())


def test_clip_nella_cronologia_play_e_video(monkeypatch, engine, tmp_path):  # noqa: F811
    """Squillo con clip: la miniatura ha il tasto play (anche senza foto) e il tocco apre il
    video (<video controls playsinline>, percorso firmato) al posto della foto; un tocco sui
    controlli del video non chiude la finestra, uno fuori sì e il video si ferma. Lo squillo
    con la sola foto apre la foto. Quando arrivano foto e clip di uno squillo nuovo (attributi
    del sensore) la lista si ricarica da sola."""
    ring_log.update_ring_log(str(tmp_path), lambda r: r.extend([
        {"time": "2026-09-27T09:00:00+02:00", "photo": "squillo_20260927_090000.jpg", "outcome": "missed"},
        {"time": "2026-09-27T10:15:00+02:00", "photo": None, "clip": "squillo_20260927_101500.mp4", "outcome": "missed"},
        {"time": "2026-09-27T10:20:00+02:00", "photo": "squillo_20260927_102000.jpg",
         "clip": "squillo_20260927_102000.mp4", "outcome": "answered"}]))
    for n in ("squillo_20260927_090000.jpg", "squillo_20260927_102000.jpg"):
        (tmp_path / n).write_bytes(b"\xff\xd8\xff\xd9")
    for n in ("squillo_20260927_101500.mp4", "squillo_20260927_102000.mp4"):
        (tmp_path / n).write_bytes(b"\x00\x00\x00\x18ftypmp42")
    js = """(() => { const r = card.shadowRoot, d = r.querySelector('dialog.photo'), v = d.querySelector('video');
      return { rows: [...r.querySelectorAll('.hist button')].map(b => [!!b.querySelector('.play'), !!b.querySelector('img'), b.disabled]),
               open: d.open, video: !v.hidden && !!v.getAttribute('src') ? v.getAttribute('src') : null, paused: v.paused,
               img: !d.querySelector('img').hidden, controls: v.controls && v.hasAttribute('playsinline') }; })()"""

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            await rig.register()
            async with Card(rig, engine) as c:
                await c.until("card.shadowRoot.querySelectorAll('.hist button').length === 3")
                st = await c.page.evaluate(js)
                assert st["rows"] == [[True, True, False], [True, False, False], [False, True, False]], st
                await c.page.evaluate("card.shadowRoot.querySelectorAll('.hist button')[0].click()")
                await c.until("card.shadowRoot.querySelector('dialog.photo video').getAttribute('src')")
                st = await c.page.evaluate(js)
                assert st["open"] and st["controls"] and not st["img"], st
                assert st["video"] == "/api/vimar_intercom/rings/squillo_20260927_102000.mp4?authSig=x", st
                assert await c.page.evaluate(f"fetch('{st['video']}').then(r => r.status)") == 200
                await c.page.evaluate("card.shadowRoot.querySelector('dialog.photo video').click()")
                assert (await c.page.evaluate(js))["open"], "il tocco sul video ha chiuso la finestra"
                await c.page.evaluate("card.shadowRoot.querySelector('dialog.photo .cap').click()")
                await c.until("!card.shadowRoot.querySelector('dialog.photo').open")  # l'evento close arriva dopo
                await c.until("!card.shadowRoot.querySelector('dialog.photo video').getAttribute('src')")
                assert (await c.page.evaluate(js))["paused"]
                await c.page.evaluate("card.shadowRoot.querySelectorAll('.hist button')[2].click()")
                await c.until("card.shadowRoot.querySelector('dialog.photo').open")
                st = await c.page.evaluate(js)
                assert st["img"] and st["video"] is None, st
                await c.page.evaluate("card.shadowRoot.querySelector('dialog.photo').close()")
                # Squillo nuovo: la foto arriva ~1 s dopo (sensore), poi il clip: la lista si aggiorna da sola
                rig.hub.stats.update(last_photo="squillo_20260927_110000.jpg", last_photo_v=1)
                await asyncio.sleep(0.5)
                assert await c.page.evaluate("card.shadowRoot.querySelectorAll('.hist button').length") == 3
                ring_log.update_ring_log(str(tmp_path), lambda r: r.append(
                    {"time": "2026-09-27T11:00:00+02:00", "photo": "squillo_20260927_110000.jpg",
                     "clip": "squillo_20260927_110000.mp4", "outcome": "missed"}))
                (tmp_path / "squillo_20260927_110000.jpg").write_bytes(b"\xff\xd8\xff\xd9")
                rig.hub.stats.update(last_photo_v=2)
                await c.until("card.shadowRoot.querySelectorAll('.hist button').length === 4")
                assert (await c.page.evaluate(js))["rows"][0] == [False, True, False]
                (tmp_path / "squillo_20260927_110000.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
                rig.hub.stats.update(last_clip="squillo_20260927_110000.mp4")
                await c.until("!!card.shadowRoot.querySelector('.hist button').querySelector('.play')")
                assert (await c.page.evaluate(js))["rows"][0] == [True, True, False]
                assert not rig.services and not rig.peer.got(is_("INVITE")) and not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_video_webcodecs_primo_fotogramma_subito(monkeypatch, engine):  # noqa: F811
    """Dal campo (2026-09-27): INVITE, 200 OK a 1,1 s, primo IDR a ~2 s, ma il video in card
    a 4-6 s (stream di HA) e la targa chiude a ~10 s. Con WebCodecs il canvas dipinge il
    primo IDR entro 300 ms da quando lo manda la targa, senza aprire /av; allo squillo,
    con la card che si collega a GOP già iniziato, il server le rimanda il GOP corrente e
    il fotogramma arriva senza aspettare il prossimo IDR (3 s). Con -s stampa i tempi."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")
    aus = hm.access_units(45)  # IDR ogni 3 s, come la targa
    monkeypatch.setattr(hm, "access_units", lambda gop=15: aus)
    tl = {}

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()

            async def on_invite(peer, inv):
                tl["invite"] = time.time()
                peer.reply(inv, 100, "Trying")
                peer.reply(inv, 180, "Ringing")
                await asyncio.sleep(1.0)                   # il cloud: 200 OK dopo ~1 s
                peer.pending_invite = None
                peer.reply(inv, 200, "OK", body=peer.sdp())
                tl["ok200"] = time.time()
                rig.start_media(inv.body)                  # IDR subito dopo il 200 OK
            rig.peer.on_invite = on_invite

            async with Card(rig, engine) as c:
                await c.until(IDLE)
                tl["tap"] = time.time()
                await c.tap("view")                        # "Vedi esterno"
                await c.until("info().player?.frames > 0", 8)
                p = (await c.info())["player"]
                idr = rig.panel_media.idr_at
                ms = lambda t: f"{(t - tl['tap']) * 1000:6.0f} ms"  # noqa: E731
                print(f"\n[Vedi esterno, dal tocco]  INVITE {ms(tl['invite'])}  WS aperto {ms(p['ws'] / 1000)}"
                      f"  200 OK {ms(tl['ok200'])}  IDR targa {ms(idr)}  primo NAL {ms(p['nal'] / 1000)}"
                      f"  primo fotogramma {ms(p['frame'] / 1000)}  ->  IDR->canvas {p['frame'] - idr * 1000:.0f} ms")
                assert p["frame"] - idr * 1000 < 300, p
                await asyncio.sleep(1)
                t = await c.T()
                assert (await c.info())["player"]["frames"] >= 10
                assert await c.page.evaluate(  # dipinto davvero: 320x240 e non nero
                    "(() => { const cv = card._player.canvas, d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;"
                    " return cv.width === 320 && cv.height === 240 && d.some((v) => v > 128); })()")
                assert await c.page.evaluate(GEOMETRY) == {"canvas_fills_media": True, "ratio_4_3": True,
                                                            "open_over_video": True, "open_on_top": True}
                assert t["av"] == [] and "live" not in t["created"] and t["ws"] == 1, t
                assert rig.hub._stream_viewers == 0
                await c.tap("hangup")
                await c.until(IDLE + " && T.wsClosed === 1 && !info().player")

                # Squillo: la card vede "ringing" (e apre il WS) 0,5 s dopo che la targa ha
                # mandato l'IDR; il prossimo sarebbe fra 2,5 s.
                rig.state_override = "idle"
                rig.ring()
                r183 = await rig.peer.wait_for(is_(code=183))
                rig.start_media(r183.body)
                await wait_until(lambda: rig.panel_media.idr_at, 3, "IDR della targa")
                await asyncio.sleep(0.5)
                rig.state_override = None
                await c.until("info().player?.frames > 0", 8)
                p = (await c.info())["player"]
                print(f"[Squillo, WS aperto {(p['ws'] / 1000 - rig.panel_media.idr_at) * 1000:.0f} ms dopo l'IDR]"
                      f"  WS->primo fotogramma {p['frame'] - p['ws']:.0f} ms")
                assert p["frame"] - p["ws"] < 300, p
                rig.peer.request("CANCEL", "ring-1", 1, "pnl")
                await c.until(IDLE + " && T.wsClosed === 2 && !info().player")
                t = await c.T()
                assert t["av"] == [] and t["ws"] == 2 and not t["errors"], t
                assert len(rig.peer.got(is_("INVITE"))) == 1  # solo la nostra "Vedi esterno"
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)  # WebKit di Playwright: niente WebCodecs
def test_pacchetto_perso_a_meta_gop_niente_video_smerigliato(monkeypatch, engine):  # noqa: F811
    """Dal campo (40515 via cloud, 2026-09-28): un pacchetto RTP perso per strada dava un NAL
    col buco e il video smerigliato (righe nere, sbavate) fino all'IDR dopo, ~3 s. La targa
    finta perde un pacchetto a metà GOP: alla card non arriva nessun NAL col buco né P che
    riferiscano il fotogramma perso (il canvas resta fermo sull'ultimo buono), HA chiede
    subito un keyframe (INFO nel dialogo) e al prossimo IDR i fotogrammi riprendono."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")
    aus = hm.access_units(45)  # IDR ogni 3 s, come la targa
    monkeypatch.setattr(hm, "access_units", lambda gop=15: aus)
    ok_nals = {n for au in aus for n in au}
    seen: list = []  # NAL usciti dal server (frame_sink: gli stessi del WS) e le perdite, in ordine
    orig_lost = media.RTPVideoProtocol._lost
    monkeypatch.setattr(media.RTPVideoProtocol, "_lost",
                        lambda self, why: (seen.append(why), orig_lost(self, why)))

    async def s():
        async with Rig(monkeypatch, "tls", http=True) as rig:  # tls: INFO accettato solo con la Route giusta
            media.video_proto.frame_sink = seen.append  # il rig non avvia il frame grabber
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                pm = rig.panel_media
                pm.drop = {pm.sent + 20}                     # un pacchetto, fra poco, a metà GOP
                await wait_until(lambda: pm.sent > max(pm.drop), 5, "pacchetto perso")
                n_info = len(rig.peer.got(is_("INFO")))
                frames = (await c.info())["player"]["frames"]
                await wait_until(lambda: len(rig.peer.got(is_("INFO"))) == n_info + 1, 2, "INFO keyframe")
                info = rig.peer.got(is_("INFO"))[-1]
                assert "picture_fast_update" in info.body and info not in rig.peer.dropped
                await c.until(f"info().player?.frames > {frames} + 10", 6)  # riparte dall'IDR
                t, p = await c.T(), (await c.info())["player"]
                assert p["resets"] == 0 and t["av"] == [] and not t["errors"], (p, t)
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())
    nals = [x for x in seen if isinstance(x, bytes)]
    assert nals and all(n in ok_nals for n in nals), "NAL col buco emesso"
    i = next(k for k, x in enumerate(seen) if isinstance(x, str))   # la perdita
    after = [x[0] & 0x1F for x in seen[i:] if isinstance(x, bytes)]
    assert 5 in after and 1 not in after[:after.index(5)], after[:20]  # niente P prima dell'IDR
    assert 1 in after[after.index(5):], "video fermo dopo l'IDR"


def test_layout_sotto_video_sopra_tasti_sotto(monkeypatch, engine, tmp_path):  # noqa: F811
    """`layout: sotto`: da fermo la cronologia è una lista sotto la riga (niente palco 4:3,
    niente foto grande); in diretta il video 4:3 sta sopra la riga e i tasti restano
    sotto, fuori dal video. Stessi posti fissi: "Vedi esterno" → "Riaggancia"."""
    ring_log.update_ring_log(str(tmp_path), lambda r: r.append(
        {"time": "2026-09-27T10:15:00+02:00", "photo": "squillo_20260927_101500.jpg", "outcome": "answered"}))
    (tmp_path / "squillo_20260927_101500.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    js = """(() => { const r = card.shadowRoot, q = (s) => r.querySelector(s).getBoundingClientRect();
      const m = q('.media'), t = q('#talk'), o = q('#open');
      return { media: m.height, ratio_4_3: Math.abs(m.width / m.height - 4 / 3) < 0.02, drawer: q('.drawer').height,
               still: getComputedStyle(r.querySelector('.still')).display,
               below: t.y >= m.y + m.height && o.y >= m.y + m.height,
               hangup: r.querySelector('#hangup').hidden, view: r.querySelector('#view').hidden }; })()"""

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
            await rig.register()
            rig.peer.on_invite = answer_200
            async with Card(rig, engine, layout="sotto") as c:
                await c.until(IDLE + " && card.shadowRoot.querySelectorAll('.hist button').length === 1")
                assert await c.page.evaluate("card.getAttribute('layout')") == "sotto"
                assert (await c.page.evaluate(js))["media"] == 0
                await c.tap("photo")                     # lista sotto la riga, senza foto grande
                await c.until("card.shadowRoot.querySelector('.drawer').getBoundingClientRect().height > 40")
                st = await c.page.evaluate(js)
                assert st["still"] == "none" and not st["ratio_4_3"] and st["media"] > 40, st
                await c.tap("photo")
                await c.until("card.shadowRoot.querySelector('.media').getBoundingClientRect().height === 0")
                await c.tap("view")
                await c.until("info().pill === 'In chiamata' && info().video !== 'auto'")
                st = await c.page.evaluate(js)
                assert st["ratio_4_3"] and st["below"] and not st["hangup"] and st["view"], st
                await c.tap("hangup")
                await c.until(IDLE)
                assert (await c.page.evaluate(js))["media"] == 0
                assert rig.services == ["vimar_intercom.call", "vimar_intercom.hangup"]
                assert not (await c.T())["errors"]
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_editor_visuale(monkeypatch, engine):  # noqa: F811
    """L'editor (getConfigElement, ha-form finto) manda `config-changed` con le sole chiavi
    diverse dai default; la card viva riceve setConfig e cambia layout e nome subito."""
    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.page.evaluate("""(() => {
                  const ed = customElements.get('vimar-intercom-card').getConfigElement();
                  window.changed = [];
                  ed.addEventListener('config-changed', (e) => {  // come HA: editor e card ricevono la config nuova
                    changed.push(e.detail.config); ed.setConfig(e.detail.config); card.setConfig(e.detail.config); });
                  document.body.append(ed); ed.hass = card._hass; ed.setConfig({ type: 'custom:vimar-intercom-card' });
                  window.ed = ed; })()""")
                assert await c.page.evaluate("ed.querySelector('select[name=layout]').value") == "overlay"
                await c.page.select_option("ha-form select[name=layout]", "sotto")
                await c.page.fill("ha-form input[name=name]", "Portone")
                await c.page.press("ha-form input[name=name]", "Tab")
                got = await c.page.evaluate("({ changed, layout: card.getAttribute('layout'),"
                                            " name: card.shadowRoot.querySelector('.name').textContent })")
                assert got["changed"][-1] == {"type": "custom:vimar-intercom-card", "layout": "sotto", "name": "Portone"}
                assert got["layout"] == "sotto" and got["name"] == "Portone", got
                await c.page.select_option("ha-form select[name=layout]", "overlay")  # default: chiave via dallo YAML
                got = await c.page.evaluate("({ last: changed.at(-1), layout: card.getAttribute('layout') })")
                assert got["last"] == {"type": "custom:vimar-intercom-card", "name": "Portone"} and got["layout"] == "overlay"
                stub = await c.page.evaluate("customElements.get('vimar-intercom-card').getStubConfig(card._hass, ['camera.x'])")
                assert stub == {"camera": "camera.vimar_intercom_intercom", "name": "Citofono", "layout": "overlay", "history": 8}
                assert not (await c.T())["errors"] and not rig.services
    run(s())


def test_video_senza_webcodecs_stream_di_ha(monkeypatch, engine):  # noqa: F811
    """Senza VideoDecoder (WebKit di Playwright, Safari vecchio, HA in HTTP) o con un
    decoder che si rompe (Chromium, VideoDecoder finto che fallisce la configurazione al
    primo IDR): il riquadro torna alla picture-entity di HA su /av, senza errori, e la
    chiamata continua; a riposo il WS è chiuso."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine, badwc=True) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().video === 'live' && T.av.includes(200) && T.avBytes > 0", 8)
                assert not (await c.info())["player"]
                await c.tap("hangup")
                await c.until(IDLE + " && T.live === 0")
                t = await c.T()
                assert t["ws"] == t["wsClosed"] and not t["errors"], t
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
@pytest.mark.parametrize("gap", [0.3, 2.5], ids=["durante_l_attesa", "dopo_l_attesa"])
def test_seconda_vedi_esterno_subito_dopo_resta_su_webcodecs(monkeypatch, engine, gap):  # noqa: F811
    """Dal campo (iPhone su 5G, 2026-09-27): tre "Vedi esterno" di fila, la seconda 3 s dopo
    la fine della prima → card bianca (stream di HA aperto: «Stream opened (1 viewers)»).
    La targa manda RTP anche dopo il nostro BYE (dal cloud ~100 ms; qui la finta non
    smette mai): il server lo scartava? No: «First video RTP» ricontato da zero,
    depacketizzato e mandato al WS della card in attesa. E una chiamata nuova durante
    l'attesa (1,5 s) riusava player, WebSocket e decoder della chiamata prima.
    Ora: dopo stop_media niente RTP ai WS; chiamata nuova = player nuovo, il cui primo
    NAL è della chiamata nuova (dopo il suo 200 OK); canvas con fotogrammi, /av mai."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")
    tl = {}

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            orig = rig.peer.on_invite

            async def on_invite(peer, inv):
                await orig(peer, inv)
                tl["ok200"] = time.time()
            rig.peer.on_invite = on_invite

            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                await asyncio.sleep(0.5)
                await c.tap("hangup")
                await c.until("info().pill === 'Pronto'")
                await asyncio.sleep(0.3)
                assert media.video_proto.remote_addr is None
                rx = (await c.T())["rx"]
                await asyncio.sleep(0.5)  # la targa manda ancora: al WS della card in attesa niente
                assert (await c.T())["rx"] == rx, "RTP dopo il BYE arrivato alla card"
                assert media.video_proto._gop_msgs == [] and media.video_proto.pkt_count == 0
                await asyncio.sleep(max(0.0, gap - 0.8))
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                p = (await c.info())["player"]
                assert p["nal"] >= tl["ok200"] * 1000 - 5, "NAL della chiamata prima al player nuovo"
                await asyncio.sleep(1)
                t = await c.T()
                info = await c.info()
                assert info["video"] == "canvas" and info["player"]["frames"] >= 15, (info, t)
                assert info["player"]["resets"] == 0
                assert t["av"] == [] and "live" not in t["created"] and not t["errors"], t
                assert t["ws"] == 2 and t["wsClosed"] == 1, t  # un player per chiamata
                assert rig.hub._stream_viewers == 0
                assert len(rig.peer.got(is_("INVITE"))) == 2
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_decoder_rotto_riparte_dal_prossimo_idr_senza_stream_di_ha(monkeypatch, engine):  # noqa: F811
    """VideoDecoder che dà errore a metà (dati corrotti, riferimento perso: su iPhone
    VideoToolbox lo fa): niente stream di HA (bianco su 5G), decoder nuovo al prossimo
    IDR e i fotogrammi continuano sul canvas."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine, flakywc=True) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("T.wcBroken === 1", 8)
                n = (await c.info())["player"]["frames"]
                await c.until(f"info().player?.frames > {n} + 10", 5)  # IDR ogni 1 s qui
                t = await c.T()
                info = await c.info()
                assert info["video"] == "canvas" and info["player"]["resets"] == 1, (info, t)
                assert t["av"] == [] and "live" not in t["created"] and not t["errors"], t
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_cambio_vista_e_ritorno_a_chiamata_in_corso(monkeypatch, engine):  # noqa: F811
    """Dal campo: a chiamata in corso si va su un'altra vista e si torna: card bianca.
    HA stacca la card (disconnectedCallback: player chiuso) e la riattacca; il video
    deve tornare subito sul canvas (GOP corrente rimandato dal server), senza /av.
    Provato sia col solo rientro in pagina sia con `hass` rimesso da HA."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")
    aus = hm.access_units(45)  # IDR ogni 3 s, come la targa: il rientro cade a metà GOP
    monkeypatch.setattr(hm, "access_units", lambda gop=15: aus)

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                for i, rehass in enumerate((False, True)):
                    await asyncio.sleep(1.2)
                    await c.page.evaluate("card.remove()")
                    await asyncio.sleep(1)
                    assert not (await c.info())["player"]
                    t0 = time.time()
                    await c.page.evaluate("document.body.appendChild(card)")
                    if rehass:
                        await c.page.evaluate("card.hass = { ...card._hass }")
                    await c.until("info().player?.frames > 0", 3)
                    p = (await c.info())["player"]
                    print(f"\n[rientro {i + 1}] primo fotogramma {p['frame'] / 1000 - t0:.3f} s dopo il rientro,"
                          f" {p['frame'] - p['ws']:.0f} ms dal WS")
                    assert p["frame"] - p["ws"] < 300, p
                    await asyncio.sleep(1)
                    t = await c.T()
                    info = await c.info()
                    print(f"[rientro {i + 1}] fotogrammi in 1 s: {info['player']['frames']}")
                    assert info["video"] == "canvas" and info["player"]["frames"] >= 10, (info, t)
                    assert t["av"] == [] and "live" not in t["created"] and not t["errors"], t
                    assert t["ws"] == 2 + i and t["wsClosed"] == 1 + i, t
                assert rig.hub._stream_viewers == 0 and rig.hub.status == "in_call"
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_websocket_video_caduto_si_riapre_senza_stream_di_ha(monkeypatch, engine):  # noqa: F811
    """Il WebSocket del video cade a chiamata in corso (rete, HA che riparte): la card
    non passa allo stream di HA, lo riapre entro ~1 s, il server le rimanda il GOP
    corrente e i fotogrammi riprendono sul canvas."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                for ws in list(rig.audio_ws_clients):  # il server chiude il WS della card
                    await ws.close()
                await c.until("info().player?.resets === 1")
                n = (await c.info())["player"]["frames"]
                await c.until(f"T.ws === 2 && info().player?.frames > {n} + 10", 5)
                t = await c.T()
                info = await c.info()
                assert info["video"] == "canvas" and t["av"] == [] and "live" not in t["created"], (info, t)
                assert not t["errors"] and rig.hub.status == "in_call"
                await c.tap("hangup")
                await c.until(IDLE)
    run(s())


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_websocket_video_caduto_di_continuo_riapre_con_attesa_crescente(monkeypatch, engine):  # noqa: F811
    """HA fermo: il WebSocket del video cade a ogni riapertura. La card non riprova ogni
    secondo per sempre: 1, 2, 4… s (massimo 10), e da capo dopo una riapertura riuscita."""
    if not hm.FFMPEG:
        pytest.skip("serve ffmpeg per il video della targa finta")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            await rig.register()
            rig.answer(media_on=True)
            async with Card(rig, engine) as c:
                await c.until(IDLE)
                await c.tap("view")
                await c.until("info().player?.frames > 0", 8)
                # HA giù: auth/sign_path fallisce a ogni tentativo di riapertura
                # (`; 0`: Playwright chiama da solo un'espressione che vale una funzione)
                await c.page.evaluate("window._cw = card._player._hass.callWS; "
                                      "card._player._hass.callWS = async () => { throw new Error('HA giù'); }; 0")
                for ws in list(rig.audio_ws_clients):
                    await ws.close()
                await c.until("info().player?.resets === 1 && info().player?.wait === 1")
                t0 = time.monotonic()
                await c.until("info().player?.wait === 4", 6)     # 1 s → 2 s → 4 s
                assert 2.5 < time.monotonic() - t0 < 5, "non ha aspettato 1 + 2 s"
                assert (await c.T())["ws"] == 1
                await c.page.evaluate("card._player._hass.callWS = window._cw; 0")
                await c.until("T.ws === 2 && info().player?.wait === 0", 6)  # riaperto: da capo
                n = (await c.info())["player"]["frames"]
                await c.until(f"info().player?.frames > {n} + 5", 5)
                await c.tap("hangup")
                await c.until(IDLE)
                ws_n = (await c.T())["ws"]
                await asyncio.sleep(1.5)
                assert (await c.T())["ws"] == ws_n, "il player chiuso non deve riaprire"
    run(s())
