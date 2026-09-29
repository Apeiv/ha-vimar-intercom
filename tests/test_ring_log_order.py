"""Le modifiche al registro squilli si applicano nell'ordine in cui sono chieste (il pool
dell'executor non lo garantisce): «answered» non deve passare prima dell'inserimento."""
import asyncio
import time

from custom_components.vimar_intercom import hub as hub_mod
from custom_components.vimar_intercom import ring_log


def test_le_modifiche_al_registro_partono_in_ordine(monkeypatch):
    applicate = []

    def lento_il_primo(folder, change):
        if not applicate:
            time.sleep(0.2)  # il primo job in coda arriva al lock dopo il secondo, se non è serializzato
        change(applicate)

    monkeypatch.setattr(ring_log, "update_ring_log", lento_il_primo)
    monkeypatch.setattr(hub_mod.R, "SNAPSHOT_DIR", "x")

    async def s():
        h = hub_mod.VimarIntercomHub()
        t1 = asyncio.create_task(h._ring_log(lambda r: r.append("inserita")))
        t2 = asyncio.create_task(h._ring_log(lambda r: r.append("answered")))
        await asyncio.gather(t1, t2)

    asyncio.run(s())
    assert applicate == ["inserita", "answered"]
