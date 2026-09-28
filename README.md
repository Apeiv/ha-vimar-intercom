# Vimar Intercom — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

🇮🇹 *[Leggi questa pagina in italiano](README.it.md)*

Brings the **Vimar Elvox** video intercom (2 Fili Plus / IP / 2FV2) into Home Assistant: get the
doorbell ring, open the door or gate, view the camera **on demand**, control **voicemail** and
**do not disturb**, and drive your plant's **actuators** (F1/F2, stair lights, relays) as buttons.

> **How it actually works.** This integration does **not** use RTSP. It implements a **custom SIP
> stack in Python/asyncio** that emulates the official **Vimar VIEW** app ("TOGA"): same `User-Agent`,
> same identity headers (`Mobile-IMEI`, `MyName`) and the proprietary **`Panda`** header. It talks
> either to the **Flexisip instance running on the Tab** (local UDP :5060) or to the **Vimar cloud over
> TLS** (SRV `_sips._tcp`). Video arrives **on demand** from the SIP call (H.264 RTP, served to Home
> Assistant's stream component as MPEG-TS on `/api/vimar_intercom/av`), not from an always-on RTSP
> stream. While the doorbell rings, the integration requests a video preview (SIP early media), so
> the camera and snapshots show the visitor before anyone answers.

---

## Compatibility

Developed on the **Elvox Tab 7S 2F+ WiFi (art. 40507)**. Other Vimar 2F / 2FV2 / IP Tabs should work
too — the configuration comes from the pairing QR code — and the table below is what has actually been
reported so far.

| Model | Art. | Plant | Firmware | Connection | Status |
|---|---|---|---|---|---|
| Elvox Tab 7S 2F+ WiFi | 40507 | 2F | — | local UDP | Development platform: ring, call, answer/hang up, door open, on-demand video, actuators |
| Elvox Tab 5S UP 2 Wire WiFi | 40515 | 2FV2 | 2.1.0203 | cloud TLS | Working, reported by @CPietro — see the notes below |
| Elvox Tab 5S UP 2 Wire WiFi | 40515 | — | — | cloud TLS | Cloud registration working after the 1.0.1 fix, reported by @gtarraran992 ([#1](../../issues/1)) |

**What differs between plants.** Both Tab 5S reports, plus the development plant, point at the same
practical conclusion: *what matters is the address you send to, and how much the Tab tells you back*.

- **Send state commands to the SGA.** On the development plant (40507 / 2F), `VOICEMAIL;ON|OFF` and
  `DND;ON|OFF` work when they are sent to the SGA — `55001` there, taken from the phonebook's
  `SYSTEM.MAGIC_APT_INTERCOM`. Sent anywhere else (the Tab's own address, the apartment group, the old
  `60001` default) they return a bare 200 OK and do nothing, or a 404. If your switches appear dead,
  the SGA/PICG options are the first thing to check — see the options table below.
- **`GET_INIT_STATUS` replies, but not with the same amount of detail.** On the 40507 the reply is
  short: `rubrica_ver`, `vm_ver`, `vm_level`, `dnd`, `voicemail`. On the 40515 / 2FV2 plant it is the
  full payload — `dnd`, `voicemail`, `rubrica_ver`, `vm_ver`, `vm_level`, `vm_timeout`,
  `vm_timeout_values`, `apt_names`, `GID`, `media_enc` and a `token`. Which is why the integration
  parses what it finds and ignores what it doesn't, instead of assuming a fixed set.
- **Media encryption is a per-plant value**, not a global default: the 40515 reports
  `media_enc: "srtp"`, while the development plant refused SRTP outright and runs plain RTP.
- Still open on the 40515: **voicemail switches on but not off**, under investigation by the reporter.

Got it running on a different model, or on the same one with different results? Please open a
[hardware compatibility report](../../issues/new?template=compatibility_report.yml) — reports where
everything just worked are as useful as the ones where something broke.

---

## Requirements

- Home Assistant **2024.7** or later, Python 3.12+ (what HA 2024.7 ships).
- ffmpeg on the Home Assistant host (declared in the manifest) for the camera.
- The plant's **pairing QR code** (from the VIEW app) **or** the SIP parameters entered by hand
  (id, password, domain, cloud proxy).
- Python requirements: only `pycryptodome` and `requests` — no external SIP library, the stack is custom.

---

## Installation

### Through HACS
1. HACS → Integrations → ⋮ menu → *Custom repositories* → add this repo with category *Integration*.
2. Install **Vimar Intercom**.
3. Restart Home Assistant.

### Manual
Copy `custom_components/vimar_intercom/` into your Home Assistant `config/custom_components/` folder
and restart.

> **If you reinstall or update by hand**: `__init__.py` and `sip_client.py` carry local logging
> patches (not present upstream — see the *Logging* section below). If you overwrite those files with
> a copy from somewhere else, reapply the patches: outside HACS nothing preserves them for you.

---

## Configuration

Settings → Devices & services → Add integration → **Vimar Intercom**.

- **QR** (recommended): paste the text of the Vimar pairing QR code. The integration decodes it
  (`qr_decoder.py`: Base64(AESkey|AES-CBC|IV) → `KEY=VALUE` pairs) and fills in id, password, domain,
  cloud/local proxy, GID, MAC and plant type.
- **Manual**: enter `sip_user`, `sip_password`, `sip_domain` and `cloud_proxy` yourself.

**Found on the network** (since 1.0.12, [#6](../../issues/6)): the Tab announces itself over mDNS
(`_eipvdes._tcp`, the same service the VIEW app looks for), and Home Assistant shows it under
*Discovered*. The QR or the credentials are still needed (the announcement carries no secret), but
the intercom's address and the local SIP domain come from the Tab itself. This matters on plants
whose QR says `domain=127.0.0.1`, such as a 40515: the domain the Tab announces (its own address)
is used for local registration instead of the cloud domain. An intercom that is already set up
is recognised by its MAC, and if the DHCP gives it a new address the integration follows it
(local mode only). Where mDNS is filtered, nothing changes: add it by hand as before.

### Options (after adding the integration)

Settings → Vimar Intercom → **Configure**:

| Option | Description |
|---|---|
| **Intercom IP** (`local_proxy`) | IP address of the Flexisip instance on the Tab |
| **Use local SIP UDP** (`use_local_udp`) | ON = local UDP; OFF = cloud TLS |
| **Local UDP port** (`local_udp_port`) | default 5060 |
| **Actuators (JSON)** (`actuators`) | JSON list of `{name, msg, target, icon}`; creates dynamic buttons. Empty = no buttons |
| **SGA** (`sga_target`) | Recipient of `VOICEMAIL;`/`DND;`, and of the door command when `door_target` is empty. Empty = default `55001` |
| **PICG** (`picg_target`) | Recipient of `GET_INIT_STATUS`. On the development plant it matches the SGA; on others it does not (60001 on a 40515). Empty = default `55001` |
| **Video entrance panel** (`camera_target`) | Panel called by the camera, *Call* and *Call Video (outdoor)*: the `PHONEBOOK` row with `TYPE='PE'`. **Not the SGA.** Empty = default `55100` |
| **Internal panel** (`internal_panel_target`) | Target of *Call Home (indoor)*. The phonebook does not say which one it is: set it by hand. Empty = default `55002` |
| **Entrance panel that opens the door** (`door_target`) | Recipient of the door command (lock, *Open Door*, `open_door` without `target`, actuators with target `AUTO`): the `GID_PE` of the door actuator in the phonebook. **Not always the SGA**: on a 2FV2 the SGA is `61000` and the door is opened by panel `55001`. Empty = the saved door actuator's panel, otherwise the SGA |
| **Ring snapshot folder** (`snapshot_dir`) | Where the visitor's photo is saved on every ring (`squillo_YYYYMMDD_HHMMSS_mmm.jpg` + `ultimo_squillo.jpg`), e.g. `/config/media/citofono`. Must be writable by HA. Empty = off |
| **Seconds after the ring** (`snapshot_delay`) | Wait before the photo (preview start + exposure). Default 3 (Tab 5S Up 40515) |
| **Away message** (`away_message_file`, `away_message_delay`) | Audio file (mp3, wav…) played to the visitor if nobody answers within N seconds (0 = off, max 60); then the integration hangs up |
| **Media encryption (SRTP)** (`media_enc`) | **Automatic** (default since 1.0.11): follows the `media_enc` the plant declares in its `GET_INIT_STATUS` reply (`"srtp"` on a cloud 40515); plants with the short reply (the 40507) stay on plain RTP. **On** / **Off** force it. Entries saved as "on" by 1.0.10 or earlier stay on; "off" becomes automatic. Try **On** if the camera stays black or the call fails with `488` |

Example, Tab 5S Up 40515 (Due Fili Plus, cloud): SGA `61000`, PICG `60001`, video and door panel
`55001`. These values come from the VIEW app's phonebook, not from the defaults.

The actuator list and the SGA/PICG values come from your plant's **phonebook** (`rubrica.db`): in the
options menu pick **"Download the phonebook from the intercom"** (LAN), **"Download the phonebook from
the Vimar cloud"** (plants that send the long `GET_INIT_STATUS` reply) or **"Import actuators from
rubrica.db"**, upload the file (you can get it through the VIEW app or with root access, see
`docs/RUBRICA.md`) and confirm — actuators, SGA, PICG, the video
entrance panel and the panel that opens the door are then set automatically. You can also enter the values by hand in the "Settings" step, which is handy if you
already know your plant's SGA or want to tweak the imported actuator list.

---

## Entities

| Entity | Platform | Description |
|---|---|---|
| Intercom | `camera` | **On-demand** video (stream): opening it places the SIP call (not on a reconnection within 5 s of the last viewer leaving, for 60 s after the end of a call: `/av` answers 503; `/av` also answers 503 at once when the call it was waiting for is refused or ends, unless the iOS app's WebSocket is connected: then it waits up to 25 s for the app's call); during a ring it shows the preview without answering. Snapshots are instant during a call or ring (clean keyframes), none otherwise, so thumbnails never ring the panel |
| Doorbell | `event` | `event` entity (device class DOORBELL), event type `ring`, fired on ring (incoming INVITE) |
| Lock | `lock` | Opens the door (`OPEN_2F` → `door_target`); auto-relocks after 5 s (there is no physical feedback) |
| Call | `button` | SIP call to the default outdoor unit |
| Call Video (outdoor) / Call Home (indoor) | `button` | Call to `camera_target` / `internal_panel_target` |
| Answer / Hang up | `button` | Answer (200 OK) / end the call (BYE) |
| Open Door | `button` | `OPEN_2F` to `door_target` |
| *Dynamic actuators* | `button` | One per entry in `options["actuators"]` (F1/F2, stair lights, relays…); sends `MSG` with `Panda: command` |
| Voicemail | `switch` | `VOICEMAIL;ON/OFF` (Panda: blue) to the SGA; state read from the Tab's announcements and from `GET_INIT_STATUS`, asked after every command. The commanded value is shown for 10 s at most: with no confirmation the state becomes *unknown* ([#9](../../issues/9)) |
| Do Not Disturb | `switch` | `DND;ON/OFF` (Panda: blue) to the SGA; same rules as Voicemail |
| Voicemail delay | `select` | Only on plants that send the long `GET_INIT_STATUS` reply: `vm_timeout`, one of the plant's own `vm_timeout_values`, written with `SET_APT_PARAMS` ([#4](../../issues/4)). It does not appear on plants with the short reply |
| Intercom SIP | `binary_sensor` | SIP registration active (connectivity) |
| Intercom In Call | `binary_sensor` | A call is up |
| Intercom Ringing | `binary_sensor` | ON while an outdoor unit is calling (attribute: caller) |
| Intercom Outgoing Call | `binary_sensor` | ON while Home Assistant is calling |
| Intercom State | `sensor` (enum) | offline / idle / ringing / in_call / calling (plus network attributes, and on plants with the long reply the apartment `GID`, `apt_names` and the declared `media_enc`) |
| Intercom Last Caller | `sensor` | Outdoor unit or monitor of the last ring |
| Intercom Last Ring | `sensor` (timestamp) | Time of the last ring |
| Intercom Rings | `sensor` (counter) | Rings since startup |
| Intercom Calls | `sensor` (counter) | Connected calls |
| Intercom Last Call Duration | `sensor` (s) | Duration of the last call |
| Intercom Last Door Open | `sensor` (timestamp) | Last door opening (attributes: unit, outcome, counter) |
| Intercom Last Command | `sensor` | Outcome of the last `send_command` |
| Intercom Last Received Message | `sensor` | Last SIP MESSAGE from the intercom |

---

## Intercom card (two-way audio)

![The intercom card: at rest with the ring history, while the doorbell rings, and in a call](docs/images/intercom-card.png)

*The card at rest with the ring history, during a ring (video preview before answering) and in a call. The camera picture is a demo scene.*

The integration ships a dashboard card and loads it itself, so there is nothing to add under
Resources. Pick **Citofono Vimar** in the card picker (camera, name, layout and history have a
visual editor; the rest stays in YAML) or add it by hand:

```yaml
type: custom:vimar-intercom-card
# optional, these are the defaults:
name: Citofono
camera: camera.vimar_intercom_intercom
status: sensor.vimar_intercom_intercom_stato
lock: lock.vimar_intercom_serratura
last_ring: sensor.vimar_intercom_intercom_ultimo_squillo
anchor: citofono   # "" = off
history: 8         # 0 = off
layout: overlay    # or "sotto"
```

Opening the card never calls the panel. The live video starts only while the doorbell rings or
during a call. Without video the card is one row: the photo of the last ring, name, state, time
of the last ring and the three buttons. With video the card shows a 4:3 pane; `layout` decides
where the buttons go while it is live:

- `overlay` (default): the card *is* the video. State top left, history top right, buttons on
  a dark strip at the bottom of the picture.
- `sotto`: the video sits above the row, the buttons stay in the row under it; nothing covers
  the picture.

After a hang-up the last picture stays 1.5 s with the buttons off, so a second tap does not land
on whatever moves in below when the card shrinks. **Vedi esterno** calls the video panel; the
view lasts as long as the panel allows (about 10 s on the Tab 5S Up 40515), then the video ends
and the card closes. To look again, press **Vedi esterno** again, as on the in-home monitor.
The buttons change with the state:

| State | Buttons |
|---|---|
| idle | Vedi esterno, Parla, Apri |
| ringing | —, Rispondi (answers and opens the microphone), Apri |
| calling | Annulla, Microfono (only to turn it off), Apri (the pill counts the seconds; after 20 s it says the panel is not answering) |
| in call | Riaggancia, Microfono (on/off), Apri |

Each button keeps its place: call on the left, voice in the middle, door on the right, so a tap
never lands on a button that just changed. A failed call, answer or door opening shows its error
in place of the state line for a few seconds. **Apri** needs two taps within 3 s. There is no
reject button during the ring: the ring stops on its own, and a stray tap would send the visitor
away.

Audio goes over `/api/vimar_intercom/audio_ws`, the same channel the iOS app uses: 8 kHz PCM
both ways, with the browser's echo cancellation. The browser only allows the microphone over
**HTTPS** (or on localhost); over plain HTTP the Parla button is off (its tooltip says why) and
the rest still works. Labels are in Italian.

**Video.** Over HTTPS, on browsers with WebCodecs (Chrome, Edge, Firefox, Safari and iOS from
16.4), the card decodes the panel's H.264 from the same WebSocket and paints it on a canvas:
the first frame shows about 0.1 s after the panel sends it, and HA's stream is not opened.
Anywhere else (plain HTTP, older Safari) it falls back to HA's own camera stream, which takes
2-4 s to start.

**Deep link.** When the page URL ends with `#citofono` (option `anchor`) the card scrolls itself
into view, e.g. `/lovelace/camera#citofono` as the tap action of a ring notification.

**Last rings.** With a snapshot folder (`snapshot_dir`) set, the photo of the last ring on the
row is the history button (during a call the button is on the video): the latest rings
(option `history`, default 8) with photo, time and outcome: *Risposto* (answered from HA),
*Messaggio di assenza* (away message), *Nessuna risposta* (not answered from HA; a ring answered
on the panel counts here too). Tap a photo to see it large. The integration keeps the list in
`squillo.json` next to the photos (last 200 rings). Without the folder there is no history.

---

## Services (`services.yaml`)

| Service | Description | Fields |
|---|---|---|
| `vimar_intercom.send_command` | Arbitrary SIP MESSAGE (for testing). Admins and automations only | `body`, `target`, `header_name`, `header_value` |
| `vimar_intercom.call` | SIP call to an outdoor unit or monitor | `target` |
| `vimar_intercom.answer` | Answers the incoming call | — |
| `vimar_intercom.hangup` | Ends the active call | — |
| `vimar_intercom.open_door` | Door open command (`OPEN_2F`; only `OPEN` / `OPEN_*` commands); without `target` it goes to `door_target` | `target`, `command` |
| `vimar_intercom.fetch_local` | HTTP Digest GET against the Tab's local interface (home mode). Admins and automations only | `path`, `save_as`, `host`, `scheme` |
| `vimar_intercom.simulate_ring` | Test ring (admin): fires the doorbell event and your automations, without the panel | — |

Example (Developer tools → Actions):

```yaml
action: vimar_intercom.send_command
data:
  body: OPEN_2F
  target: "55001"
  header_name: Panda
  header_value: command
```

---

## Events

The doorbell is exposed as an **`event` entity** (`event.<...>_doorbell`, event type `ring`), not as a
bus event. In automations, use a state trigger on the `event` entity (or on the ringing binary sensor).

On top of that, from the `MESSAGE`s the Tab sends, the integration fires these events on the Home
Assistant bus: `vimar_intercom_missed_call`, `vimar_intercom_videomessage`,
`vimar_intercom_fuoriporta`, `vimar_intercom_call_info`, `vimar_intercom_phonebook_changed`.
They are read-only — nothing is sent back. Example trigger:

```yaml
automation:
  - alias: "Intercom - Missed call"
    trigger:
      - platform: event
        event_type: vimar_intercom_missed_call
    action:
      - service: notify.mobile_app_phone
        data: { message: "Missed call at the intercom" }
```

---

## Example automations

`packages/vimar_intercom.yaml` (copy it into `config/packages/`) contains a
"ring → notification" automation driven by the state change of the `event` entity, plus a safety
hang-up that closes a call left open for two minutes:

```yaml
automation:
  - alias: "Intercom - Ring → notification"
    trigger:
      - platform: state
        entity_id: event.vimar_intercom_doorbell
    action:
      - action: notify.mobile_app_YOUR_PHONE
        data:
          title: "🔔 Someone at the door"
          message: "Ring at {{ now().strftime('%H:%M:%S') }}."
          data:
            image: "/api/camera_proxy/camera.vimar_intercom_intercom"
```

`camera.snapshot` works during a call or a ring. For a photo of every visitor you don't need an
automation: set **Ring snapshot folder** in the options. Test your automations with
`vimar_intercom.simulate_ring`. Don't point a `camera: platform: ffmpeg` at `/api/vimar_intercom/av`:
that hangs Home Assistant until the ffmpeg probe times out.

`docs/lovelace_example.yaml` has a basic Lovelace card with the answer / open door / hang up buttons.
The video pane shows live video while a call or a ring is up. For voice, use the intercom card below.

## Behaviour changes in 1.0.9

- The camera never calls the panel by itself on a reconnection: for 60 s after any call end,
  failed auto-call or watched ring preview, a reopen of `/av` within 5 s of the last viewer
  leaving (go2rtc, HA's stream worker) gets 503 instead of a new call. Opening the camera
  later (dashboard, HomeKit) calls as before.
- A ring answers `183 Session Progress` with our SDP (early media) instead of `180 Ringing`,
  so the panel streams video before anyone answers. A second INVITE while busy gets `486`, not
  `603`; a forked second branch of the same ring gets `482`.
- The H.264 answer mirrors the panel's offer (payload type, `packetization-mode`,
  `profile-level-id`); our own offer proposes both modes (96 mode 1, 97 mode 0).
- The `/api/vimar_intercom/video` MJPEG view is gone; `/av` and `/audio_ws` are unchanged.
- Local UDP mode: SIP is accepted from the intercom's address and from the hosts of the current
  dialog (Contact/Via of the ring, Contact of the call), nothing else.

## Known limitations

- **Two-way audio only in the intercom card**: HA's own camera player has no microphone, so
  answering from a button, a notification or Alexa picks up the call silently. Talk from
  `custom:vimar-intercom-card`, over HTTPS.
- **Ring preview** needs the plant to send early media (verified on a Tab 5S Up 40515 over the
  cloud). If it doesn't, the preview and the ring photo stay empty until someone answers.
- **Cloud-only plants** (e.g. Tab 5S Up 40515): the Tab answers `503 You're not allowed` to any SIP
  request on the LAN, so local UDP mode can't work there; use cloud TLS. Its local HTTP interface
  (:80) may accept the TCP connection and then stay silent, so there is no local phonebook to read
  over the LAN. Camera, actuators, door opening and the state commands still work over SIP.
- **Voicemail / DND**: these are commanded through the **SGA** (`SYSTEM.MAGIC_APT_INTERCOM` in the
  phonebook, `55001` on the plant used for development). Sent to any other address they are silently
  ignored, so getting the SGA right is what makes them work — set it in the options or let the
  `rubrica.db` import fill it in.
- **Cloud phonebook**: needs a `token`. Plants that answer `GET_INIT_STATUS` with the long form hand it
  over directly, and the phonebook can then be downloaded with a single authenticated request — see
  `docs/RUBRICA.md` §0-bis, verified on a 40515. Since 1.0.12 the options menu does it:
  **"Download the phonebook from the Vimar cloud"** ([#5](../../issues/5)); the token is read from the
  plant each time and never stored. Plants that answer with the short form (including the development
  one) don't carry a token: there use the download from the intercom on the LAN, or the manual extraction.
- **By-me actuators** (e.g. stair lights on By-me home automation): these may not respond over SIP even
  when they are listed in the phonebook.
- **Lock**: no physical state feedback (optimistic auto-relock after 5 s).
- **A ring during one of our own calls**: while Home Assistant is calling the panel or is in a
  call, an incoming INVITE gets `486 Busy Here` and fires no doorbell event: on the field it can't
  yet be told apart from the PBX echoing our own call. A ring right after the panel's BYE is a
  normal ring.
- **Phonebook**: on cloud-only plants it has to be extracted once (see `docs/RUBRICA.md`); the
  automatic import over the cloud depends on a token provisioned by the account.

---

## Logging

The component keeps an internal circular buffer (`_debug_log`, in `__init__.py`) for its own
diagnostics, and raises its logger to `DEBUG` to fill it. By default that would propagate every
`DEBUG` line to the Home Assistant log too, overriding the level set in `logger:` in
`configuration.yaml` (Python loggers propagate to the root).

The patch: the `custom_components.vimar_intercom` logger stays at `DEBUG` for the internal buffer, but
with `propagate = False`; a dedicated handler forwards only `WARNING` and above to the HA log. Result:
internal diagnostics intact, HA log clean.

**"Stale response 407" on the SIP keepalive**: the periodic OPTIONS (`_send_options_ping` in
`sip_client.py`) did not register its own Call-ID among the expected responses, so the proxy's reply
(typically a `407`) was logged as `WARNING "Stale response ..."` even though it is the normal outcome
of the keepalive. `_dispatch_message` now recognises Call-IDs prefixed with `ping-` and logs them at
`DEBUG` instead. With this fix and the one above, **no** `logger:` filter in `configuration.yaml` is
needed any more to silence these messages.

**Known limitation**: the trade-off cuts both ways. Because the component keeps its own logger at
`DEBUG` and forwards only `WARNING` and above, setting
`logger: logs: custom_components.vimar_intercom: debug` in `configuration.yaml` will *not* put this
component's `DEBUG` lines in the Home Assistant log — read them from
`/api/vimar_intercom/debug` instead. Making the forwarded level configurable is on the list.

If you update `__init__.py` or `sip_client.py` from an external source (not HACS, not versioned for
this component), check that both patches are still in place — see the note under
*Installation → Manual*.

---

## Security

- SIP credentials (password / `ha1`) are stored **encrypted** in the Home Assistant config entry, never
  in plain text in the repo.
- Internal HTTP endpoint: `/av` is **LAN-only** (`_is_local_request`); the `/audio_ws`
  WebSocket requires Home Assistant authentication, and its debug actions (`command`, `probe`,
  `scan`, `register`, `reconnect`) are admin-only. The QR payload is never logged at INFO level.
- Ring history for the card: `GET /api/vimar_intercom/rings` (list, `?limit=` up to 50) and
  `GET /api/vimar_intercom/rings/<name>` (the photo) require Home Assistant authentication (the
  card loads photos through signed paths). The second serves only `squillo_YYYYMMDD_HHMMSS_mmm.jpg`
  files inside `snapshot_dir`, nothing else; the folder is never exposed under `/local`.
- In local UDP mode, SIP packets from any host other than the intercom are dropped, so another
  device on the LAN can't fake a ring.
- No mandatory cloud dependency when running in local UDP mode.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: never guess SIP commands or tokens (a wrong
actuator token can physically open a door), keep credentials out of the repo and out of your logs, run
`pytest` before opening a PR, and say which hardware you tested on.

---

## Disclaimer

This project is **not affiliated with or endorsed by Vimar S.p.A.**. "Vimar", "Elvox" and "VIEW" are
trademarks of their respective owners. You supply your own credentials for your own plant.

## License

**MIT** — © [Noise Heroes](https://github.com/noiseheroes-lab) (upstream project) and the fork's
contributors.
