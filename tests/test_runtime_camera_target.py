"""Test per runtime.py: risoluzione di camera_target e retrocompatibilità.

CONTRIBUTING regola 1: un target sbagliato può fisicamente non aprire (o
aprire) la porta giusta. camera_target è opzionale e deve lasciare
INTERCOM/DOOR_ESTERNO sull'SGA quando non è impostato: sono questi due valori
a finire nel "Chiama" e nell'apri-porta OPEN_2F, non solo CAMERA_TARGET
(usato dalla sola autoaccensione video). Vedi anche test_runtime.py per lo
stesso pattern su sga_target/picg_target.
"""
from __future__ import annotations

from custom_components.vimar_intercom import const, runtime


def _base_data(**overrides) -> dict:
    data = {
        "sip_user": "u",
        "sip_password": "p",
        "sip_domain": "example.test",
    }
    data.update(overrides)
    return data


def test_camera_target_empty_falls_back_to_const_default():
    runtime.configure(_base_data())
    assert runtime.CAMERA_TARGET == const.CAMERA_TARGET


def test_camera_target_empty_keeps_intercom_and_door_on_sga():
    """Il comportamento storico: senza camera_target esplicito, "Chiama" e
    l'apri-porta restano sull'SGA, anche se CAMERA_TARGET (solo autoaccensione
    video) ha comunque il suo default storico."""
    runtime.configure(_base_data(sga_target="99999"))
    assert runtime.INTERCOM == "sip:99999@example.test"
    assert runtime.DOOR_ESTERNO == "sip:99999@example.test"
    assert runtime.CAMERA_TARGET == const.CAMERA_TARGET


def test_camera_target_set_moves_intercom_door_and_camera():
    """Quando la targa video NON coincide con l'SGA (es. Tab 5S Up: SGA 61000,
    targa PE 55001), camera_target vale per call, video e apri-porta insieme:
    è quello a cui risponde davvero la targa."""
    runtime.configure(_base_data(sga_target="61000", camera_target="55001"))
    assert runtime.CAMERA_TARGET == "55001"
    assert runtime.INTERCOM == "sip:55001@example.test"
    assert runtime.DOOR_ESTERNO == "sip:55001@example.test"


def test_camera_target_whitespace_is_stripped_and_falls_back():
    runtime.configure(_base_data(sga_target="55001", camera_target="   "))
    assert runtime.CAMERA_TARGET == const.CAMERA_TARGET
    assert runtime.INTERCOM == "sip:55001@example.test"


def test_reconfigure_resets_previous_camera_override():
    runtime.configure(_base_data(sga_target="55001", camera_target="55100"))
    assert runtime.CAMERA_TARGET == "55100"
    assert runtime.INTERCOM == "sip:55100@example.test"
    runtime.configure(_base_data(sga_target="55001"))
    assert runtime.CAMERA_TARGET == const.CAMERA_TARGET
    assert runtime.INTERCOM == "sip:55001@example.test"
