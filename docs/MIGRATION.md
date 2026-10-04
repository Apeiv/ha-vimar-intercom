# Coming from noiseheroes-lab/ha-vimar-intercom

🇮🇹 *[Italiano](MIGRATION.it.md)* · [← README](../README.md)

The original [noiseheroes-lab/ha-vimar-intercom](https://github.com/noiseheroes-lab/ha-vimar-intercom)
is archived and points here. This page is for moving an existing install over.

## Same domain, so one at a time

Both integrations use the domain `vimar_intercom` and install into the same folder,
`custom_components/vimar_intercom`. They can't run side by side: installing this one over the old
folder replaces it. The old config entry doesn't carry over either. It is version 2, this
integration's is version 1 and has no migration for it, so left in place it stops at
*Migration error*. The way across is: remove the old one, install this one, add it again.

## Before you start

Note where you use the old integration: automations, scripts and dashboards. Entity ids, services
and events are different here (see [What changes](#what-changes)), so those will need a touch-up
afterwards.

## Step by step

1. **Delete the old entry while the old code is still installed.** Settings → Devices & services →
   Vimar Intercom → ⋮ → *Delete*. Doing it in this order lets the old integration remove its own
   stored data (the plant phonebook and the call log under `.storage/`).
2. **Remove the old code.** In HACS open the old *Vimar Intercom* (the one from `noiseheroes-lab`),
   ⋮ → *Remove*, then in HACS → ⋮ → *Custom repositories* delete
   `https://github.com/noiseheroes-lab/ha-vimar-intercom`. Installed by hand? Delete
   `config/custom_components/vimar_intercom`.
3. **Restart Home Assistant.**
4. **Install this integration** as in the [README](../README.md#installation) and restart again.
5. **Add it:** Settings → Devices & services → *Add integration* → *Vimar Intercom*. You can reuse
   the same pairing slot on the indoor unit: it is the same QR code. This integration takes the
   QR's **text**, not a photo, so read the code with any QR scanner app and paste what it shows.
6. **Get the phonebook:** *Configure* → download it from the intercom, from the Vimar cloud or from
   a `rubrica.db` file. The old integration fetched it on its own; here it is one step in the
   options, and it sets the door, the actuators and where commands go.
7. Reload the browser tab or the app so the new card is picked up, then fix the automations and
   dashboards from your list.

## What changes

- **Entities:** Home Assistant sees them as new (their unique ids start with the new entry's id),
  with a new device. Names, areas and icons you set on the old ones don't carry over, and neither
  does their history. Check the new ids in [Entities](ENTITIES.md).
- **Services:** the old `play_video_message`, `mark_video_message_read`, `delete_video_message`,
  `delete_all_video_messages` and `clear_missed_calls` don't exist here. This integration has
  `call`, `answer`, `decline`, `hangup`, `open_door` and a few more, listed in
  [Entities, services and automations](ENTITIES.md#services-servicesyaml).
- **Events:** there is no `vimar_intercom_ring` bus event. The doorbell is an `event` entity: trigger
  on its state, or on the ringing binary sensor. `vimar_intercom_missed_call` exists but with a
  different payload, and video messages fire `vimar_intercom_videomessage` (no underscore between
  the two words). Details in [Events](ENTITIES.md#events).
- **Dashboard card:** the type is still `custom:vimar-intercom-card` and it still loads by itself,
  with nothing to add under Resources. The options are different, though (`title`, `device_id`,
  `show_actuators` and `hidden_entities` are ignored), so the simplest fix is to delete the old card
  and add *Citofono Vimar* again from the card picker. See [The intercom card](CARD.md).

## Leftovers

Followed in the order above, nothing of the old integration stays behind. If the code was swapped
before the entry was deleted, two small files may remain in `config/.storage/`:
`vimar_intercom.<old entry id>.plant` and `vimar_intercom.<old entry id>.call_log`. They are unused
and safe to delete with Home Assistant stopped. The Python package `pyzbar` the old integration
asked for also stays installed; it does no harm.
