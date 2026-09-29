"""Webhook squillo (opzionale): GET fire-and-forget a inizio e fine squillo.

Serve ad accendere/spegnere un interruttore fittizio (es. Scrypted "Dummy
Switch" collegato a un Custom Doorbell Button) o un qualsiasi altro automatismo
esterno. Non deve MAI bloccare o interrompere lo squillo: timeout breve,
eccezioni sempre catturate, solo un warning nei log. L'URL porta spesso un
token segreto in query string: nei log si vede solo schema+host, mai intero.

Stesso pattern di away_tts.py (hass in una globale di modulo, popolata da
setup() in __init__.py): l'hub non tiene hass, per coerenza con ring/state
callback (vedi hub.py).
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)
_hass = None

TIMEOUT_S = 5


def setup(hass) -> None:
    global _hass
    _hass = hass


def _safe(url: str) -> str:
    """Solo schema+host(+porta) per i log: l'URL può contenere un token segreto
    o user:password@, che netloc si porterebbe dietro."""
    try:
        p = urlsplit(url)
        if not (p.scheme and p.hostname):
            return "(url non valido)"
        return f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "")
    except ValueError:
        return "(url non valido)"


async def fire(url: str) -> None:
    """GET fire-and-forget: nessuna eccezione esce da qui."""
    if not url or _hass is None:
        return
    try:
        session = async_get_clientsession(_hass)
        async with asyncio.timeout(TIMEOUT_S):
            async with session.get(url):
                pass  # fire-and-forget: non ci serve la risposta, solo che sia partita
    except Exception as e:
        # Solo il tipo: il testo dell'eccezione (es. URL non valido) stampa l'URL intero.
        _LOGGER.warning("Webhook squillo fallito (%s): %s", _safe(url), type(e).__name__)
