"""What the plant tells us at runtime: the detected model and its media encryption.

The settings live in runtime.py. This module holds the values that change
while the integration runs, reset by runtime.configure() at setup.
"""

from __future__ import annotations

# ─── Media encryption ────────────────────────────────────────────────────────
# MEDIA_ENC è il valore effettivo, quello che legge sdp.build_sdp:
# l'opzione (runtime.MEDIA_ENC_OPTION) se forzata, altrimenti quanto dichiara
# l'impianto.
MEDIA_ENC_PLANT: bool | None = None   # None = l'impianto non l'ha dichiarato
MEDIA_ENC: bool = False


def recompute_media_enc(option: str) -> None:
    global MEDIA_ENC
    if option == "on":
        MEDIA_ENC = True
    elif option == "off":
        MEDIA_ENC = False
    else:
        MEDIA_ENC = bool(MEDIA_ENC_PLANT)


def set_plant_media_enc(value, option: str) -> bool:
    """`media_enc` dal GET_INIT_STATUS_REPLY ("srtp", "none", ...). Restituisce True
    se il valore effettivo è cambiato."""
    global MEDIA_ENC_PLANT
    before = MEDIA_ENC
    MEDIA_ENC_PLANT = None if value is None else str(value).strip().lower() == "srtp"
    recompute_media_enc(option)
    return MEDIA_ENC != before


# ─── Modello rilevato via SIP (vedi model_detect.py) ─────────────────────────
# Popolato all'avvio dal config entry (ultimo valore rilevato) e aggiornato a
# runtime appena il citofono si presenta con il suo User-Agent SIP.
DETECTED_MODEL:    str = ""   # es. "Elvox Tab 7S"
DETECTED_FW:       str = ""   # versione firmware, se presente nello User-Agent
DETECTED_UA:       str = ""   # User-Agent grezzo, per diagnostica
DETECTED_PRIORITY: int = 99   # indice del pattern che ha rilevato il modello


def reset(data: dict, media_enc_option: str) -> None:
    """Called by runtime.configure() at setup: forget what the plant said and
    start again from the model saved in the entry."""
    global MEDIA_ENC_PLANT
    global DETECTED_MODEL, DETECTED_FW, DETECTED_UA, DETECTED_PRIORITY

    MEDIA_ENC_PLANT  = None   # lo ridice l'impianto al prossimo GET_INIT_STATUS_REPLY
    recompute_media_enc(media_enc_option)

    # Modello rilevato in una sessione precedente: riparte da lì, così le
    # entità mostrano subito il valore giusto anche prima del primo dialogo SIP.
    DETECTED_MODEL    = data.get("detected_model", "") or ""
    DETECTED_FW       = data.get("detected_fw", "") or ""
    DETECTED_UA       = data.get("detected_ua", "") or ""
    DETECTED_PRIORITY = 99 if not DETECTED_MODEL else int(data.get("detected_priority", 98))
