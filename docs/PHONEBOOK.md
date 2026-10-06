# Getting the phonebook

🇮🇹 *[Italiano](PHONEBOOK.it.md)* · [← README](../README.md) · [Configuration](CONFIGURATION.md) · [Troubleshooting](TROUBLESHOOTING.md)

The phonebook (`rubrica.db`) tells the integration which actuators your plant has (door, F1/F2,
stair lights, relays), which panel opens the door, which panel the camera calls, and the **SGA** and
**PICG**: the addresses voicemail, do-not-disturb and the status request must go to. Without it the
integration falls back to defaults that are right on some plants and wrong on others
([#10](https://github.com/ha-vimar/ha-vimar-intercom/issues/10),
[#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14)).

There is no automatic chain: the three sources are three entries of the options menu
(Settings → Devices & services → Vimar Intercom → **Configure**) and you pick one. Try them in this
order and stop at the first that works.

## Which way for your plant

```text
Can Home Assistant reach the intercom on the local network (its IP, port 80)?
├─ yes → 1. "Download the phonebook from the intercom"
│        ├─ works → done
│        └─ "not reachable over HTTP" / "rejected the SIP credentials" → go to 2
└─ no (cloud-only plant, e.g. Tab 5S Up 40515) → go to 2

2. "Download the phonebook from the Vimar cloud"
   ├─ works → done
   └─ "did not send the phonebook token" (short GET_INIT_STATUS reply, as on the 40507)
      or "the Vimar cloud rejected the token" → go to 3

3. "Import actuators from rubrica.db" with a file you extracted yourself
   ├─ you have the file → upload it, confirm → done
   └─ no way to get the file → go to 4

4. By hand: "Network and actuator settings" (actuators JSON, SGA, PICG, panels),
   plus the find_sga service to find the PICG/SGA
```

## 1. From the intercom (local network)

**Configure → Download the phonebook from the intercom.** Home Assistant asks the intercom's local web
interface for the phonebook, authenticated with the SIP credentials it already has from the QR: no
token, no Vimar account, no phone. In the same request it reads the PICG the intercom declares.
Verified on a Tab 7S (40507).

- Needs the intercom's address in *Network and actuator settings*.
- "Not reachable over HTTP": the Tab does not answer on port 80. The 40515s reported so far refuse
  the connection ([#5](https://github.com/ha-vimar/ha-vimar-intercom/issues/5)): use 2 or 3.
- "Rejected the SIP credentials, or does not expose this API": the intercom answers `401` in both
  cases, so it does not necessarily mean wrong credentials.

## 2. From the Vimar cloud (token)

**Configure → Download the phonebook from the Vimar cloud** (since 1.0.12). Plants that answer the
status request (`GET_INIT_STATUS`) with the long reply include a `token` in it; with that token the
phonebook is one authenticated download from the Vimar cloud. The token is read from the plant each
time and never stored. Verified on a Tab 5S Up 40515 (2FV2) ([#5](https://github.com/ha-vimar/ha-vimar-intercom/issues/5)).

- "Did not send the phonebook token": the plant answers with the short reply (the 40507 does). There
  is no token to use: go back to 1, or 3.
- How long a token stays valid is not known yet.

## 3. From a `rubrica.db` file

**Configure → Import actuators from rubrica.db.** Upload the SQLite file and confirm: the actuators
of your apartment's GID replace the current list, and SGA, PICG, the video entrance panel and the
panel that opens the door are filled in. A file that says nothing about the PICG leaves the
configured one as it is. The file is only read, not kept.

How to get the file from the VIEW app (a rooted phone, or the app in Windows Subsystem for Android)
is in [RUBRICA.md](RUBRICA.md) §3 and §3-bis (in Italian).

## 4. By hand

When none of the above works, enter the values in **Configure → Network and actuator settings**:

- **Actuators (JSON)**: the list `tools/parse_rubrica.py` prints from a phonebook. Empty = no
  actuator buttons; the door still opens with the generic command to the panel that opens the door
  ([#58](https://github.com/ha-vimar/ha-vimar-intercom/issues/58)).
- **SGA**, **PICG**, **Video entrance panel**, **Entrance panel that opens the door**: see
  [Configuration](CONFIGURATION.md). Empty SGA/PICG means `55001`, which is right on the development
  plant and wrong on others.
- Don't know the SGA/PICG? The **`vimar_intercom.find_sga`** service (admins) asks a range of
  addresses with `GET_NICKS` and reports the one that answers as PICG; with *Apply as PICG* it writes it
  into the options ([#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14)).

## After the import

Voicemail and do-not-disturb that answer `200 OK` but change nothing, or a door command that is
accepted but opens nothing, almost always mean a wrong SGA or door panel: see
[Troubleshooting](TROUBLESHOOTING.md).
