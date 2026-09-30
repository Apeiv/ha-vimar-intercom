"""Regenerates the README screenshots in docs/images/ (synthetic scene, no real photos).

    README_SHOTS=1 PYTHONPATH=<aiohttp> python -m pytest -m browser tests/test_readme_shots.py

Skipped unless README_SHOTS=1, so the normal suite never rewrites the images.
"""
from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

import pytest
from harness.card import Card, engine  # noqa: F401  (fixture)
from harness.rig import Rig, run

from custom_components.vimar_intercom import ring_log
from custom_components.vimar_intercom import runtime as R

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(not os.environ.get("README_SHOTS"), reason="set README_SHOTS=1 to regenerate")]
OUT = Path(__file__).resolve().parents[1] / "docs" / "images"

LIGHT = """:root, body { --primary-color:#03a9f4; --primary-text-color:#212121; --secondary-text-color:#727272;
  --text-primary-color:#fff; --success-color:#43a047; --error-color:#db4437; --warning-color:#ffa726; --info-color:#4285f4; --card-background-color:#fff; --divider-color:#e0e0e0; --primary-background-color:#fafafa;
  --ha-card-border-radius:12px; color-scheme: light; }
body { background:#f0efeb; color:#212121 }"""

# The user's own HA theme (CasaAL, light): teal success, terracotta warning, warm background. The card takes its colors from these.
CASAAL = """:root, body { --primary-color:#2A9D8F; --success-color:#2A9D8F; --warning-color:#C8623A; --error-color:#D24A3F; --info-color:#3D8FC4;
  --primary-text-color:#1B1812; --secondary-text-color:#6B6557; --state-icon-color:#6B6557; --text-primary-color:#fff;
  --card-background-color:#fff; --ha-card-background:#fff; --secondary-background-color:#F5F2EA; --divider-color:rgba(40,30,16,.07);
  --ha-card-border-radius:16px; --ha-card-box-shadow:none; color-scheme: light; }
body { background:#F5F2EA; color:#1B1812 }"""

DARK = """:root, body { --primary-color:#03a9f4; --primary-text-color:#e1e1e1; --secondary-text-color:#9b9b9b;
  --text-primary-color:#fff; --success-color:#43a047; --error-color:#db4437; --warning-color:#ffa726; --info-color:#4285f4; --card-background-color:#1c1c1c; --divider-color:#3a3a3a; --primary-background-color:#111;
  --ha-card-border-radius:12px; color-scheme: dark; }
body { background:#111; margin:0; padding:12px; font-family:Roboto,system-ui,sans-serif; color:#e1e1e1 }
ha-form label { display:block; margin:0 0 14px; font-size:12px; color:#9b9b9b }
ha-form input, ha-form select { display:block; width:100%; box-sizing:border-box; margin-top:4px; padding:10px;
  background:#2a2a2a; color:#e1e1e1; border:0; border-bottom:1px solid #777; border-radius:4px; font-size:16px }
.edit { background:#1c1c1c; border-radius:12px; padding:16px; margin-top:12px }"""

# Material Design Icons paths (Apache-2.0), for the stand-in <ha-icon>.
ICONS = {
 "cctv": "M6.03 12.03L8.03 15.5L5.5 18.68L2 12.62L6.03 12.03M17 18V15.29C17.88 14.9 18.5 14.03 18.5 13C18.5 12.43 18.3 11.9 17.97 11.5L19.94 10.35C20.95 9.76 21.3 8.47 20.71 7.46L19.33 5.06C18.74 4.05 17.45 3.7 16.44 4.28L8.31 9C7.36 9.53 7.03 10.75 7.58 11.71L9.08 14.31C9.63 15.26 10.86 15.59 11.81 15.04L13.69 13.96C13.94 14.55 14.41 15.03 15 15.29V18C15 19.1 15.9 20 17 20H22V18H17Z",
 "check": "M21,7L9,19L3.5,13.5L4.91,12.09L9,16.17L19.59,5.59L21,7Z",
 "door-open": "M12,3C10.89,3 10,3.89 10,5H3V19H2V21H22V19H21V5C21,3.89 20.11,3 19,3H12M12,5H19V19H12V5M5,11H7V13H5V11Z",
 "menu-close": "M3 6H13V8H3V6M3 16H13V18H3V16M3 11H15V13H3V11M16 7L14.58 8.39L18.14 12L14.58 15.61L16 17L21 12L16 7Z",
 "menu-open": "M21,15.61L19.59,17L14.58,12L19.59,7L21,8.39L17.44,12L21,15.61M3,6H16V8H3V6M3,13V11H13V13H3M3,18V16H16V18H3Z",
 "microphone": "M12,2A3,3 0 0,1 15,5V11A3,3 0 0,1 12,14A3,3 0 0,1 9,11V5A3,3 0 0,1 12,2M19,11C19,14.53 16.39,17.44 13,17.93V21H11V17.93C7.61,17.44 5,14.53 5,11H7A5,5 0 0,0 12,16A5,5 0 0,0 17,11H19Z",
 "microphone-off": "M19,11C19,12.19 18.66,13.3 18.1,14.28L16.87,13.05C17.14,12.43 17.3,11.74 17.3,11H19M15,11.16L9,5.18V5A3,3 0 0,1 12,2A3,3 0 0,1 15,5V11L15,11.16M4.27,3L21,19.73L19.73,21L15.54,16.81C14.77,17.27 13.91,17.58 13,17.72V21H11V17.72C7.72,17.23 5,14.41 5,11H6.7C6.7,14 9.24,16.1 12,16.1C12.81,16.1 13.6,15.91 14.31,15.58L12.65,13.92L12,14A3,3 0 0,1 9,11V10.28L3,4.27L4.27,3Z",
 "phone": "M6.62,10.79C8.06,13.62 10.38,15.94 13.21,17.38L15.41,15.18C15.69,14.9 16.08,14.82 16.43,14.93C17.55,15.3 18.75,15.5 20,15.5A1,1 0 0,1 21,16.5V20A1,1 0 0,1 20,21A17,17 0 0,1 3,4A1,1 0 0,1 4,3H7.5A1,1 0 0,1 8.5,4C8.5,5.25 8.7,6.45 9.07,7.57C9.18,7.92 9.1,8.31 8.82,8.59L6.62,10.79Z",
 "phone-hangup": "M12,9C10.4,9 8.85,9.25 7.4,9.72V12.82C7.4,13.22 7.17,13.56 6.84,13.72C5.86,14.21 4.97,14.84 4.17,15.57C4,15.75 3.75,15.86 3.5,15.86C3.2,15.86 2.95,15.74 2.77,15.56L0.29,13.08C0.11,12.9 0,12.65 0,12.38C0,12.1 0.11,11.85 0.29,11.67C3.34,8.77 7.46,7 12,7C16.54,7 20.66,8.77 23.71,11.67C23.89,11.85 24,12.1 24,12.38C24,12.65 23.89,12.9 23.71,13.08L21.23,15.56C21.05,15.74 20.8,15.86 20.5,15.86C20.25,15.86 20,15.75 19.82,15.57C19.03,14.84 18.14,14.21 17.16,13.72C16.83,13.56 16.6,13.22 16.6,12.82V9.72C15.15,9.25 13.6,9 12,9Z",
 "volume-high": "M14,3.23V5.29C16.89,6.15 19,8.83 19,12C19,15.17 16.89,17.84 14,18.7V20.77C18,19.86 21,16.28 21,12C21,7.72 18,4.14 14,3.23M16.5,12C16.5,10.23 15.5,8.71 14,7.97V16C15.5,15.29 16.5,13.76 16.5,12M3,9V15H7L12,20V4L7,9H3Z",
 "volume-off": "M12,4L9.91,6.09L12,8.18M4.27,3L3,4.27L7.73,9H3V15H7L12,20V13.27L16.25,17.53C15.58,18.04 14.83,18.46 14,18.7V20.77C15.38,20.45 16.63,19.82 17.68,18.96L19.73,21L21,19.73L12,10.73M19,12C19,12.94 18.8,13.82 18.46,14.64L19.97,16.15C20.62,14.91 21,13.5 21,12C21,7.72 18,4.14 14,3.23V5.29C16.89,6.15 19,8.83 19,12M16.5,12C16.5,10.23 15.5,8.71 14,7.97V10.18L16.45,12.63C16.5,12.43 16.5,12.21 16.5,12Z",
 "cog": "M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.21,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.21,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.67 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z",
 "bell": "M21,19V20H3V19L5,17V11C5,7.9 7.03,5.17 10,4.29C10,4.19 10,4.1 10,4A2,2 0 0,1 12,2A2,2 0 0,1 14,4C14,4.1 14,4.19 14,4.29C16.97,5.17 19,7.9 19,11V17L21,19M14,21A2,2 0 0,1 12,23A2,2 0 0,1 10,21",
 "bell-off-outline": "M22.11,21.46L2.39,1.73L1.11,3L5.83,7.72C5.29,8.73 5,9.86 5,11V17L3,19V20H18.11L20.84,22.73L22.11,21.46M7,18V11C7,10.39 7.11,9.79 7.34,9.23L16.11,18H7M10,21H14A2,2 0 0,1 12,23A2,2 0 0,1 10,21M8.29,5.09C8.82,4.75 9.4,4.5 10,4.29C10,4.19 10,4.1 10,4A2,2 0 0,1 12,2A2,2 0 0,1 14,4C14,4.1 14,4.19 14,4.29C16.97,5.17 19,7.9 19,11V15.8L17,13.8V11A5,5 0 0,0 12,6C11.22,6 10.45,6.2 9.76,6.56L8.29,5.09Z",
 "arrow-expand-all": "M10,21V19H6.41L10.91,14.5L9.5,13.09L5,17.59V14H3V21H10M14.5,10.91L19,6.41V10H21V3H14V5H17.59L13.09,9.5L14.5,10.91Z",
 "history": "M13.5,8H12V13L16.28,15.54L17,14.33L13.5,12.25V8M13,3A9,9 0 0,0 4,12H1L4.96,16.03L9,12H6A7,7 0 0,1 13,5A7,7 0 0,1 20,12A7,7 0 0,1 13,19C11.07,19 9.32,18.21 8.06,16.94L6.64,18.36C8.27,20 10.5,21 13,21A9,9 0 0,0 22,12A9,9 0 0,0 13,3",
 "close": "M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z",
 "doorbell-video": "M14 15C14 16.11 13.11 17 12 17S10 16.11 10 15 10.9 13 12 13 14 13.9 14 15M18 4V20C18 21.1 17.11 22 16 22H8C6.9 22 6 21.11 6 20V4C6 2.9 6.9 2 8 2H16C17.11 2 18 2.9 18 4M10.5 7C10.5 7.83 11.17 8.5 12 8.5S13.5 7.83 13.5 7 12.83 5.5 12 5.5 10.5 6.17 10.5 7M16 10H8V20H16V10Z",
 "image-off-outline": "M22 20.7L3.3 2L2 3.3L3 4.3V19C3 20.1 3.9 21 5 21H19.7L20.7 22L22 20.7M5 19V6.3L12.6 13.9L11.1 15.8L9 13.1L6 17H15.7L17.7 19H5M8.8 5L6.8 3H19C20.1 3 21 3.9 21 5V17.2L19 15.2V5H8.8",
 "play-circle": "M10,16.5V7.5L16,12M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z"
}
ICON_JS = """(icons) => customElements.define('ha-icon', class extends HTMLElement {
  static get observedAttributes() { return ['icon']; }
  constructor() { super(); if (Object.hasOwn(this, 'icon')) { const v = this.icon; delete this.icon; this.icon = v; } }  // set before the upgrade
  get icon() { return this.getAttribute('icon'); }
  set icon(v) { this.setAttribute('icon', v); }
  attributeChangedCallback() { this.connectedCallback(); }
  connectedCallback() { const p = icons[(this.getAttribute('icon') || '').slice(4)] || '';
    const r = this.shadowRoot || this.attachShadow({mode:'open'});  // :host loses to the card's own display:none
    r.innerHTML = `<style>:host{display:inline-flex;width:var(--mdc-icon-size,24px);height:var(--mdc-icon-size,24px)}</style><svg viewBox="0 0 24 24" width="100%" height="100%" fill="currentColor"><path d="${p}"/></svg>`; } })"""

# A made-up front door drawn on a canvas: no camera, no real place.
SCENE = """(() => { const host = card.shadowRoot.getElementById('video');
  for (const e of host.children) e.style.display = 'none';
  const c = document.createElement('canvas'); c.width = 640; c.height = 480;
  c.style.cssText = 'width:100%;height:100%;display:block'; host.append(c);
  const x = c.getContext('2d'), g = x.createLinearGradient(0, 0, 0, 480);
  g.addColorStop(0, '#8fa6b8'); g.addColorStop(1, '#5d6f7c'); x.fillStyle = g; x.fillRect(0, 0, 640, 480);
  x.fillStyle = '#7a6a5a'; x.fillRect(0, 380, 640, 100);
  x.fillStyle = '#d9d2c5'; x.fillRect(150, 60, 340, 320);
  x.fillStyle = '#4a5a6a'; x.fillRect(180, 90, 280, 290);
  x.fillStyle = '#5b6d7e'; x.fillRect(200, 110, 110, 120); x.fillRect(330, 110, 110, 120);
  x.fillRect(200, 250, 110, 110); x.fillRect(330, 250, 110, 110);
  x.fillStyle = '#e6c15a'; x.beginPath(); x.arc(425, 240, 9, 0, 7); x.fill();
  x.fillStyle = '#3f6b4a'; x.beginPath(); x.ellipse(90, 390, 48, 60, 0, 0, 7); x.fill();
  x.fillStyle = '#a4593e'; x.fillRect(60, 400, 60, 50);
  x.fillStyle = 'rgba(255,240,200,.9)'; x.beginPath(); x.arc(320, 30, 14, 0, 7); x.fill();
})()"""


def synth_jpg(seed: int) -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (320, 240), (110 + seed * 25, 130, 150 - seed * 20))
    d = ImageDraw.Draw(im)
    d.rectangle((90, 30, 230, 200), fill=(215, 208, 195))
    d.rectangle((110, 50, 210, 200), fill=(74 + seed * 10, 90, 106))
    d.ellipse((190, 120, 200, 130), fill=(230, 193, 90))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=60)
    return b.getvalue()


def compress(png: Path, dest: Path, limit=150_000):
    from PIL import Image
    im = Image.open(png).convert("RGB")
    for colors in (256, 128, 64, 32):
        im.quantize(colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(dest, optimize=True)
        if dest.stat().st_size < limit:
            return
    raise AssertionError(f"{dest} is {dest.stat().st_size} bytes")


@pytest.mark.parametrize("engine", ["chromium"], indirect=True)
def test_readme_shots(monkeypatch, engine, tmp_path):
    OUT.mkdir(exist_ok=True)
    snap = tmp_path / "snap"
    snap.mkdir()
    times = ("090200", "124000", "181500")
    ring_log.update_ring_log(str(snap), lambda r: r.extend(
        {"time": f"2026-09-27T{t[:2]}:{t[2:4]}:00+02:00", "photo": f"squillo_20260927_{t}.jpg", "outcome": o}
        for t, o in zip(times, ("missed", "answered", "away"))))
    for i, t in enumerate(times):
        (snap / f"squillo_20260927_{t}.jpg").write_bytes(synth_jpg(i))

    async def ringing(rig, c, name):
        await c.until("info().pill === 'Pronto'")
        rig.ring(f"shot-{name}")
        await c.until("info().pill === 'Suonano alla porta' && info().video !== 'auto'")
        await c.page.evaluate(SCENE)

    async def popup(rig, c, name):
        await c.until("info().pill === 'Pronto'")
        rig.ring(f"shot-{name}")
        await c.until("info().pop && info().video !== 'auto'")
        await c.tap("x")  # closed by hand: the compact card behind shows the real ringing state
        await c.until("!info().pop && info().pill.startsWith('Tocca per vedere')")
        await asyncio.sleep(0.5)
        await c.page.screenshot(path=str(tmp_path / "compact.png"), clip={"x": 0, "y": 0, "width": 390, "height": 96})
        await c.page.evaluate("card._openPop()")
        await c.until("info().pop && info().video !== 'auto'")
        await c.page.evaluate(SCENE)

    async def tile(rig, c, name):
        async def shot(tag):
            await asyncio.sleep(0.5)
            h = await c.page.evaluate("card.getBoundingClientRect().bottom + 12")
            await c.page.screenshot(path=str(tmp_path / f"tile-{tag}.png"), clip={"x": 0, "y": 0, "width": 390, "height": h})
        await c.until("info().pill === 'Pronto' && !card.shadowRoot.querySelector('#hist').disabled")
        await shot("dark-rest")
        rig.ring(f"shot-{name}")
        await c.until("info().pop")
        await c.tap("x")
        await c.until("!info().pop && info().pill.startsWith('Tocca per vedere')")
        await shot("dark-ring")
        await c.page.add_style_tag(content=LIGHT)
        await c.page.emulate_media(reduced_motion="reduce", color_scheme="light")
        await shot("light-ring")
        rig.peer.request("CANCEL", f"shot-{name}", 1, "pnl")
        await c.until("info().pill === 'Pronto'")
        await shot("light-rest")

    async def popup_desktop(rig, c, name):
        await c.until("info().pill === 'Pronto'")
        rig.ring(f"shot-{name}")
        await c.until("info().pop && info().video !== 'auto'")
        await c.page.evaluate(SCENE)

    async def pillola(rig, c, name):
        async def shot(tag):
            await asyncio.sleep(0.5)
            h = await c.page.evaluate("card.getBoundingClientRect().bottom + 12")
            await c.page.screenshot(path=str(tmp_path / f"pillola-{tag}.png"), clip={"x": 0, "y": 0, "width": 390, "height": h})
        await c.page.add_style_tag(content=CASAAL)
        await c.page.emulate_media(reduced_motion="reduce", color_scheme="light")
        await c.until("info().pill === 'Pronto' && !card.shadowRoot.querySelector('#hist').disabled")
        await shot("rest")
        rig.ring(f"shot-{name}")
        await c.until("info().pop")
        await c.tap("x")
        await c.until("!info().pop && info().pill.startsWith('Tocca per vedere')")
        await shot("ring")

    async def drawer(rig, c, name):
        await c.until("card.shadowRoot.querySelectorAll('.hist button').length === 3"
                      " && !card.shadowRoot.querySelector('#photo').disabled")
        await c.tap("photo")
        await c.until("card._card.dataset.drawer === 'true'")

    async def s():
        async with Rig(monkeypatch, http=True) as rig:
            monkeypatch.setattr(R, "SNAPSHOT_DIR", str(snap))
            await rig.register()
            for name, w, h, layout, prep in (("card-history", 500, 460, "sotto", drawer),
                                              ("card-overlay", 500, 500, "overlay", ringing),
                                              ("card-below", 500, 470, "sotto", ringing),
                                              ("card-popup", 390, 844, "popup", popup),
                                              ("card-compact-tile", 390, 300, "popup", tile),
                                              ("card-compact-pillola", 390, 200, "popup", pillola),
                                              ("card-popup-desktop", 1280, 800, "popup", popup_desktop)):
                async with Card(rig, engine, layout=layout, compact="tile" if name == "card-compact-tile" else None) as c:
                    await c.page.set_viewport_size({"width": w, "height": h})
                    await c.page.add_style_tag(content=DARK)
                    await c.page.evaluate(ICON_JS, ICONS)
                    await c.page.emulate_media(reduced_motion="reduce", color_scheme="dark")
                    await prep(rig, c, name)
                    await asyncio.sleep(0.6)
                    png = tmp_path / f"{name}.png"
                    if name == "card-compact-pillola":  # CasaAL light theme: idle above, ringing below
                        from PIL import Image
                        a, b = Image.open(tmp_path / "pillola-rest.png"), Image.open(tmp_path / "pillola-ring.png")
                        m = Image.new("RGB", (390, a.height + b.height), (245, 242, 234))
                        m.paste(a, (0, 0))
                        m.paste(b, (0, a.height))
                        m.save(png)
                    elif name == "card-compact-tile":  # dark | light columns, idle above, ringing below
                        from PIL import Image
                        ims = {t: Image.open(tmp_path / f"tile-{t}.png") for t in ("dark-rest", "dark-ring", "light-rest", "light-ring")}
                        m = Image.new("RGB", (390 * 2, ims["dark-rest"].height + ims["dark-ring"].height), (17, 17, 17))
                        for col, th in enumerate(("dark", "light")):
                            m.paste(ims[f"{th}-rest"], (390 * col, 0))
                            m.paste(ims[f"{th}-ring"], (390 * col, ims[f"{th}-rest"].height))
                        m.save(png)
                    elif name in ("card-overlay", "card-below"):  # crop to the card, no empty band below
                        h = await c.page.evaluate("card.getBoundingClientRect().bottom + 12")
                        await c.page.screenshot(path=str(png), clip={"x": 0, "y": 0, "width": w, "height": h})
                    else:
                        await c.page.screenshot(path=str(png))
                rig.peer.request("CANCEL", f"shot-{name}", 1, "pnl")
                await asyncio.sleep(0.5)
                if name == "card-popup":  # compact dashboard card above the open popup
                    from PIL import Image
                    a, b = Image.open(tmp_path / "compact.png"), Image.open(png)
                    m = Image.new("RGB", (390, a.height + 8 + b.height), (17, 17, 17))
                    m.paste(a, (0, 0))
                    m.paste(b, (0, a.height + 8))
                    m.save(png)
                compress(png, OUT / f"{name}.png")
    run(s(), 180)
