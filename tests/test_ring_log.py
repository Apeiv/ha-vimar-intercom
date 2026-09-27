"""Registro degli squilli per la card: squillo.json accanto alle foto, esito
aggiornato alla risposta, foto servite solo se sono davvero foto squillo."""
from __future__ import annotations

import asyncio
import os

import pytest

hub_mod = pytest.importorskip("custom_components.vimar_intercom.hub")
from custom_components.vimar_intercom import ring_log as rl  # noqa: E402

sip = hub_mod.sip
R = hub_mod.R


def test_registro_limitato_e_dal_piu_recente(tmp_path):
    folder = str(tmp_path / "foto")
    for i in range(rl.RING_LOG_MAX + 5):
        rl.update_ring_log(folder, lambda r, i=i: r.append({"time": str(i), "photo": None}))
    rings = rl.read_ring_log(folder)
    assert len(rings) == rl.RING_LOG_MAX and rings[-1]["time"] == str(rl.RING_LOG_MAX + 4)
    assert [r["time"] for r in rl.recent_rings(folder, 2)] == ["204", "203"]
    assert os.listdir(folder) == [rl.RING_LOG], "file temporanei rimasti"


def test_le_foto_degli_squilli_usciti_dal_registro_si_cancellano(tmp_path):
    """Oltre RING_LOG_MAX la voce sparisce e con lei la sua foto; restano ultimo_squillo.jpg,
    le foto delle voci ancora in registro e i file che non sono foto squillo registrate."""
    folder = str(tmp_path)
    vecchia, ultima = "squillo_20260101_000000.jpg", "squillo_20260927_101500.jpg"
    for n in (vecchia, ultima, "ultimo_squillo.jpg", "squillo_20260102_000000.jpg"):
        (tmp_path / n).write_bytes(b"jpg")
    rl.update_ring_log(folder, lambda r: r.append({"time": "0", "photo": vecchia}))
    rl.update_ring_log(folder, lambda r: r.extend(
        {"time": str(i), "photo": ultima if i == rl.RING_LOG_MAX else None}
        for i in range(1, rl.RING_LOG_MAX + 1)))
    assert not (tmp_path / vecchia).exists()
    assert sorted(os.listdir(folder)) == [rl.RING_LOG, "squillo_20260102_000000.jpg", ultima, "ultimo_squillo.jpg"]


def test_registro_rotto_riparte_vuoto(tmp_path):
    (tmp_path / rl.RING_LOG).write_text("{non json")
    assert rl.read_ring_log(str(tmp_path)) == []


def test_registro_con_voci_non_oggetti(tmp_path):
    """squillo.json scritto a mano o da altro: niente 500 all'endpoint né squilli persi."""
    (tmp_path / rl.RING_LOG).write_text('[1, "x", null, {"time": "a", "photo": 5}]')
    assert rl.recent_rings(str(tmp_path), 10) == [{"time": "a", "photo": None}]
    rl.update_ring_log(str(tmp_path), lambda r: r.append({"time": "b"}))
    assert [r["time"] for r in rl.read_ring_log(str(tmp_path))] == ["a", "b"]


def test_il_temporaneo_e_sempre_un_file_nuovo(tmp_path):
    """Il temporaneo non ha un nome fisso: qualcosa messo al posto di «squillo.json.tmp»
    (un symlink verso un altro file, qui una cartella) non viene né seguito né usato."""
    (tmp_path / (rl.RING_LOG + ".tmp")).mkdir()
    rl.update_ring_log(str(tmp_path), lambda r: r.append({"time": "a"}))
    assert rl.read_ring_log(str(tmp_path)) == [{"time": "a"}]


def test_foto_solo_squillo_dentro_la_cartella(tmp_path):
    folder = tmp_path / "foto"
    folder.mkdir()
    (folder / "squillo_20260927_101500.jpg").write_bytes(b"jpg")
    (folder / "ultimo_squillo.jpg").write_bytes(b"jpg")
    (tmp_path / "squillo_20260927_101501.jpg").write_bytes(b"fuori")
    ok = rl.ring_photo_path(str(folder), "squillo_20260927_101500.jpg")
    assert ok and os.path.samefile(ok, folder / "squillo_20260927_101500.jpg")
    for bad in ("ultimo_squillo.jpg", "squillo.json", "../squillo_20260927_101501.jpg",
                "squillo_20260927_101500.jpg/..", "squillo_20260927_999999.jpg",
                "squillo_20260927_101500.jpgx", "..%2Fsquillo_20260927_101501.jpg"):
        assert rl.ring_photo_path(str(folder), bad) is None, bad
    assert rl.ring_photo_path("", "squillo_20260927_101500.jpg") is None


def test_lista_toglie_le_foto_mancanti(tmp_path):
    (tmp_path / "squillo_20260927_101500.jpg").write_bytes(b"jpg")
    rl.update_ring_log(str(tmp_path), lambda r: r.extend([
        {"time": "a", "photo": "squillo_20260927_101500.jpg"},
        {"time": "b", "photo": "squillo_20260927_101600.jpg"}]))  # foto mai arrivata
    assert [r["photo"] for r in rl.recent_rings(str(tmp_path), 10)] == [
        None, "squillo_20260927_101500.jpg"]


def test_squillo_registrato_poi_risposto(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(R, "AWAY_MESSAGE_FILE", "")
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setattr(sip, "pending_incoming", {"caller_uri": "sip:55001@dom", "cid": "c1"})
    monkeypatch.setattr(hub_mod.push_sender, "get_sender", lambda: None)

    async def answer():
        return True, "ok"

    monkeypatch.setattr(sip, "do_answer_incoming", answer)

    async def scenario():
        hub = hub_mod.VimarIntercomHub()
        hub._save_ring_photo = lambda name: asyncio.sleep(0)
        await hub._handle_broadcast("ring", "")
        await asyncio.sleep(0.2)
        [ring] = rl.read_ring_log(str(tmp_path))
        assert ring["outcome"] == "missed" and ring["caller"] == "55001"
        assert rl.RING_PHOTO.fullmatch(ring["photo"])
        assert (await hub.async_answer())[0]
        await asyncio.sleep(0.2)
        assert rl.read_ring_log(str(tmp_path))[0]["outcome"] == "answered"

    asyncio.run(scenario())
