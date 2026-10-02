"""Foto e clip degli squilli e registro squillo.json accanto (per la card). Solo I/O su
file, da chiamare nell'executor."""

import contextlib
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable

# Registro degli squilli accanto alle foto: vecchi prima, al massimo RING_LOG_MAX.
RING_LOG = "squillo.json"
RING_LOG_MAX = 200
RING_FILE = re.compile(r"squillo_\d{8}_\d{6}(_\d{3})?\.(jpg|mp4)")  # foto o clip di uno squillo
LAST_PHOTO = "ultimo_squillo.jpg"  # copy of the latest ring photo, kept across restarts
_lock = threading.Lock()  # letture/scritture dal pool dell'executor


def write_photo(folder: str, name: str, jpeg: bytes) -> int:
    """Foto con data e ora, più ultimo_squillo.jpg sempre aggiornata. Restituisce la
    versione della foto (mtime in ms): cambia quando la foto migliore sostituisce la
    prima sullo stesso nome, e la card non tiene quella vecchia in cache."""
    os.makedirs(folder, exist_ok=True)
    for n in (name, LAST_PHOTO):
        # New file, then rename over the target (as update_ring_log does): a symlink
        # or hard link planted at that name is replaced, never written through.
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(jpeg)
            os.chmod(tmp, 0o644)  # mkstemp makes 0600; photos were readable by others before
            os.replace(tmp, os.path.join(folder, n))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
    return int(os.path.getmtime(os.path.join(folder, name)) * 1000)


def read_last_photo(path: str | None, folder: str | None) -> bytes | None:
    """The last ring photo: `path` (the hub's last_photo_path), else LAST_PHOTO in
    `folder`, which survives a restart. None when neither can be read."""
    for candidate in (path, os.path.join(folder, LAST_PHOTO) if folder else None):
        if not candidate:
            continue
        try:
            # Only a regular file with one name, never a symlink or hard link someone left
            # in the folder (it would serve any file Home Assistant can read as the photo).
            if not stat.S_ISREG(os.lstat(candidate).st_mode):
                continue
            fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                if _linked(os.fstat(fh.fileno())):
                    continue
                return fh.read()
        except OSError:
            continue
    return None


def _linked(st: os.stat_result) -> bool:
    """Not a regular file, or a file with other names: a hard link planted in the folder.
    Our photos and clips are always renamed into place from a new file, so they have one."""
    return not stat.S_ISREG(st.st_mode) or st.st_nlink > 1  # some shares report 0


def update_ring_log(folder: str, change: Callable[[list], None]) -> None:
    """Legge il registro, applica change(lista) e lo riscrive. Le voci che escono dal
    registro (oltre RING_LOG_MAX) si portano via foto e clip: la cartella non cresce
    per sempre. Mai ultimo_squillo.jpg, mai file fuori dalla cartella (ring_photo_path)."""
    with _lock:
        rings = read_ring_log(folder)
        change(rings)
        dropped, rings = rings[:-RING_LOG_MAX], rings[-RING_LOG_MAX:]
        os.makedirs(folder, exist_ok=True)
        # File temporaneo nuovo (mkstemp, O_EXCL): un symlink «squillo.json.tmp» messo
        # nella cartella non viene seguito. os.replace: mai un registro scritto a metà.
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rings, fh)
            os.replace(tmp, os.path.join(folder, RING_LOG))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        for r in dropped:
            for k in ("photo", "clip"):
                if path := _ring_path(folder, str(r.get(k) or "")):
                    with contextlib.suppress(OSError):
                        os.unlink(path)


def read_ring_log(folder: str) -> list[dict]:
    """Registro degli squilli; solo le voci che sono oggetti (un file rotto non ferma nulla)."""
    try:
        with open(os.path.join(folder, RING_LOG), encoding="utf-8") as fh:
            rings = json.load(fh)
    except (OSError, ValueError):
        return []
    return [r for r in rings if isinstance(r, dict)] if isinstance(rings, list) else []


def _ring_path(folder: str, name: str) -> str | None:
    """Percorso di `name` (un nome di foto/clip di squillo) dentro `folder`. Non si
    risolve il file: un symlink resta un symlink, si toglie lui e non il suo bersaglio."""
    if not folder or not RING_FILE.fullmatch(name):
        return None
    return os.path.join(os.path.realpath(folder), name)


def ring_photo_path(folder: str, name: str) -> str | None:
    """Percorso del file `name` se è davvero una foto (jpg) o un clip (mp4) di uno
    squillo dentro `folder`: nient'altro (nemmeno un .mp4.part in scrittura, né un hard
    link a un altro file)."""
    path = _ring_path(folder, name)
    try:
        return None if path is None or _linked(os.lstat(path)) else path
    except OSError:
        return None


def recent_rings(folder: str, limit: int) -> list[dict]:
    """Ultimi squilli, dal più recente; foto e clip solo se ci sono davvero. photo_v:
    versione della foto (vedi write_photo), per l'URL che la card mette in cache."""
    rings = read_ring_log(folder)[::-1][:limit]
    for r in rings:
        for k in ("photo", "clip"):
            if not r.get(k):
                continue
            path = ring_photo_path(folder, str(r[k]))
            if not path:
                r[k] = None
            elif k == "photo":
                r["photo_v"] = int(os.path.getmtime(path) * 1000)
    return rings
