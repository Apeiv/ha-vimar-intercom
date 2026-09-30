"""Messaggio di assenza da testo: la voce la fa il TTS di Home Assistant.

Il file audio (AWAY_MESSAGE_FILE) ha la precedenza. Senza file, il testo
AWAY_MESSAGE_TEXT viene letto dal motore AWAY_MESSAGE_TTS (un'entità tts.*;
vuoto = il predefinito di HA) nella lingua di HA e decodificato in PCM 8 kHz da
ffmpeg come un file, con lo stesso tetto di 30 s. Il PCM resta in cache per
(testo, motore, lingua): allo squillo non si aspetta la rete. Si genera
all'avvio di HA (salvare le opzioni ricarica l'entry) e, se manca ancora, allo
squillo. Se il TTS fallisce (nessun motore, rete) si lascia squillare.

API pubblica di HA (2024.7 e successive): tts.generate_media_source_id e
tts.async_get_media_source_audio.
"""
from __future__ import annotations

import logging

from homeassistant.components import tts
from homeassistant.helpers.start import async_at_started

from . import media_handler as media
from . import runtime as R

_LOGGER = logging.getLogger(__name__)
_hass = None
_cache: tuple[tuple[str, str, str], bytes] | None = None  # ((testo, motore, lingua), pcm)


def setup(hass) -> None:
    global _hass
    _hass = hass
    if R.AWAY_MESSAGE_TEXT and not R.AWAY_MESSAGE_FILE:
        async_at_started(hass, _prefetch)  # a HA avviato: i motori TTS sono su


async def _prefetch(_) -> None:
    await load_pcm()


async def load_pcm() -> bytes | None:
    """PCM 8 kHz del testo (dalla cache se testo, motore e lingua non sono cambiati);
    None se non c'è testo o il TTS fallisce."""
    global _cache
    if not R.AWAY_MESSAGE_TEXT or _hass is None:
        return None
    key = (R.AWAY_MESSAGE_TEXT, R.AWAY_MESSAGE_TTS, str(_hass.config.language or ""))
    if _cache and _cache[0] == key:
        return _cache[1]
    text, engine, language = key
    try:
        audio = await _synth(text, engine or None, language)
    except Exception as e:  # nessun motore, lingua non supportata, rete: si lascia squillare
        _LOGGER.warning("Messaggio di assenza: sintesi vocale fallita (%s: %s)", type(e).__name__, e)
        return None
    pcm = await media.load_pcm(audio)
    if pcm:
        _cache = (key, pcm)
    return pcm


async def _synth(text: str, engine: str | None, language: str) -> bytes:
    """Audio (mp3, wav...) del testo dal TTS di HA. La lingua di HA è «it», ma i
    motori la scrivono ognuno a modo suo (google «it», piper «it_IT», cloud «it-IT»)
    e HA la confronta alla lettera: si provano le varianti, poi la predefinita del motore."""
    base = language.replace("-", "_").split("_")[0]
    for lang in (language, base, f"{base}_{base.upper()}", f"{base}-{base.upper()}", None):
        try:
            media_id = tts.generate_media_source_id(_hass, text, engine=engine, language=lang)
            break
        except Exception:
            if lang is None:
                raise
    _ext, audio = await tts.async_get_media_source_audio(_hass, media_id)
    return audio
