"""Messaggio di assenza dalla pagina del dispositivo (text, select).

Fonte unica = le opzioni della config entry. Le entità scrivono qui: si aggiorna
R.* in memoria e si salvano le opzioni SENZA ricaricare l'integrazione (un reload
riavvia la registrazione SIP per cambiare un testo): il listener delle opzioni
non ricarica se cambiano solo le chiavi away_message_* (vedi __init__).
"""
from __future__ import annotations

import filecmp
import os
import re
import shutil
import stat

from homeassistant.exceptions import Unauthorized

from . import runtime as R
from .const import DOMAIN

MEDIA_SUBDIR = ("citofono", "messaggi")
AUDIO_EXT = (".mp3", ".wav", ".m4a")
NONE_OPTION = "none"  # stato stabile del select file; l'etichetta è la traduzione


def messages_dir(hass) -> str | None:
    dirs = list((getattr(hass.config, "media_dirs", None) or {}).values())
    return os.path.join(dirs[0], *MEDIA_SUBDIR) if dirs else None


def ensure_dir(path: str | None) -> None:
    """Crea la cartella dei messaggi se manca (eseguire in executor)."""
    if path:
        os.makedirs(path, exist_ok=True)


def list_files(path: str | None) -> list[str]:
    """I file audio della cartella (eseguire in executor)."""
    if not path:
        return []
    return sorted(f for f in os.listdir(path) if f.lower().endswith(AUDIO_EXT))


UPLOAD_MAX = 5 * 1024 * 1024
_PLAIN_NAME = re.compile(r"\w[\w .()-]*")  # niente nomi nascosti, separatori o caratteri strani


def save_upload(src, folder: str | None) -> str:
    """Copia un file caricato dalle opzioni nella cartella messaggi e dà il percorso
    (eseguire in executor). ValueError col codice d'errore del form se non va.

    Del nome conta solo l'ultimo pezzo: niente ../ fuori dalla cartella. Un file
    diverso con lo stesso nome non si sovrascrive: si aggiunge -2, -3..."""
    name = os.path.basename(str(src).replace("\\", "/"))
    # 200 byte: c'è posto per il suffisso -n entro il limite di 255 dei filesystem.
    if not _PLAIN_NAME.fullmatch(name) or not name.lower().endswith(AUDIO_EXT) or len(name.encode()) > 200:
        raise ValueError("upload_bad_type")
    size = os.path.getsize(src)
    if size > UPLOAD_MAX:
        raise ValueError("upload_too_big")
    if not size:
        raise ValueError("upload_bad_type")  # un file vuoto non è un messaggio
    if not folder:
        raise ValueError("upload_failed")
    ensure_dir(folder)
    stem, ext = os.path.splitext(name)
    dest, n = os.path.join(folder, name), 1
    while True:
        # O_EXCL: solo un file nuovo. Un symlink (anche rotto) o un file comparso dopo
        # un controllo non viene mai scritto attraverso: si passa al nome dopo.
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
        except FileExistsError:
            if stat.S_ISREG(os.lstat(dest).st_mode) and filecmp.cmp(src, dest, shallow=False):
                return dest  # lo stesso file, già lì
            n += 1
            dest = os.path.join(folder, f"{stem}-{n}{ext}")
            continue
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
        return dest


def apply_options(data: dict, options) -> bool:
    """Opzioni salvate: se rispetto a data["applied"] (l'ultima config applicata) cambiano
    solo le chiavi away_message_*, le applica in memoria e dà True (niente reload)."""
    old, new = data.get("applied", {}), dict(options)
    changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    if not changed or not changed <= set(R.AWAY_KEYS):
        return False
    for k in changed:
        R.set_away_option(k, new.get(k))
    data["applied"] = new
    data["hub"].notify()
    return True


def set_away(hass, entry, key: str, value) -> None:
    R.set_away_option(key, value)
    hass.config_entries.async_update_entry(entry, options={**entry.options, key: value})
    hass.data[DOMAIN][entry.entry_id]["hub"].notify()  # rinfresca la disponibilità dello switch


async def require_admin(hass, context) -> None:
    """Away message entities change what visitors hear: administrators only. A call with no
    user (automations, system) stays allowed, like the admin-only services."""
    user_id = getattr(context, "user_id", None)
    if user_id is None:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None or not user.is_admin:
        raise Unauthorized()
