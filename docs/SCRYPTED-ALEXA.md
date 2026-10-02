# Echo Show as an intercom (Scrypted)

🇮🇹 *[Italiano](SCRYPTED-ALEXA.it.md)* · [← README](../README.md)

A Scrypted script that turns an Amazon Echo Show into a monitor for the Vimar panel: live video,
street audio on the Echo, and the Echo's microphone to the panel. Tested on Home Assistant with a
Tab 5S Up (40515), Scrypted and an Echo Show.

## What you get

- **No ring**: open the camera on the Echo ("Alexa, show the intercom") and it calls the panel:
  live view and two-way talk. Closing the Echo hangs up.
- **During a ring**: the doorbell announcement opens the camera on the Echo by itself. The Echo
  only **watches**, silent: it does not answer and the ring goes on on the other phones. It answers
  only when someone speaks to the Echo (about 200 ms of voice in a row).
- **Already in a call** (card, app): the Echo joins it and does not hang it up on close.
- The script **never opens the door**: the only actions it sends are `call`, `answer` and `hangup`.

## Requirements

- The integration at version **1.0.19 or later** (passive stream fixes, see the
  [pull request](https://github.com/lollox80/ha-vimar-intercom/pulls?q=is%3Apr+passive)).
  <!-- placeholder: update with the real release and PR number -->
- Scrypted with the **Scripts**, **Rebroadcast** and **Alexa** plugins.
- The ring sent to Scrypted as a doorbell press: the *Doorbell Button* (Dummy Switch) and the
  integration's ring webhooks, as in [External systems, steps 2 and 3](EXTERNAL.md#scrypted-alexa-chime--echo-show-live-view).
  Without it everything works except the Echo opening by itself on a ring.
- A Home Assistant **long-lived access token**: your profile → *Security* → *Long-lived access
  tokens* → *Create token*. If the integration's *Users allowed to see rings and live media* option is set, the token's user
  must be in it.

## Setup

1. **Script device.** In Scrypted: *Scripts* plugin → *Create Script*, name it (e.g. "Intercom").
2. **Code.** Paste the whole [`scrypted/vimar-intercom-alexa.ts`](scrypted/vimar-intercom-alexa.ts),
   replacing the template.
3. **Constants**, in the first three lines:
   - `TOKEN`: the long-lived token;
   - `HA`: Home Assistant on the LAN, plain `http` (e.g. `http://192.168.1.10:8123`);
   - `AV_KEY`: the [/av key](EXTERNAL.md#the-av-key), optional for now, leave `''` to skip it.
4. **Save, then Run.** Both. A previous version keeps running until you press *Run*: this is the
   most common reason why a change "does nothing".
5. **Audio for the Echo.** On the new device: *Rebroadcast* (prebuffer) settings → *FFmpeg Output
   Prefix*:
   ```
   -vcodec copy -acodec libopus -ar 48000 -ac 1 -b:a 32k
   ```
   The stream carries AAC in MPEG-TS, which cannot be copied into RTSP/WebRTC: without this the
   Echo gets video and no sound.
6. **Doorbell button.** Enable the *Doorbell Button* extension on this device and point the
   integration's webhooks to it (see Requirements).
7. **Alexa.** In the device's extensions, enable *Alexa*. In the Alexa app: *Devices* → *+* →
   *Add device* → discover; the intercom shows up as a doorbell camera. In its settings, turn on
   **Doorbell Press Announcements** (and the notification if you want it on the phone).
8. Optional, not measured: in the Alexa plugin settings, turning off *Use TURN Servers* can make
   the view start faster on a LAN-only setup.

Test: say "Alexa, show the intercom" with nobody at the door. The panel should light up within a
few seconds and you should hear the street.

## How it works

- **Video**: `getVideoStream` reads the continuous passive stream
  `/api/vimar_intercom/av?autocall=0&idle=image` ([details](EXTERNAL.md#the-one-rule-use-the-passive-url)):
  standby picture at rest, the panel live during a ring or a call, and it never calls by itself.
  So the Echo always has a picture, and opening it costs nothing.
- **Talk**: Alexa calls `startIntercom` when the Echo opens the camera and `stopIntercom` when it
  closes. The script opens `/api/vimar_intercom/audio_ws` with `Authorization: Bearer <TOKEN>` and
  reads the first `state` message:
  - `in_call`: join;
  - `ringing` true: watch, and send `{"action": "answer"}` only after ~200 ms of voice;
  - otherwise: `{"action": "call"}`.

  Integrations without the `ringing` field (before 1.0.19) are treated as ringing, so the Echo
  never places a call over a ring; on those versions opening the Echo with no ring only shows the
  standby picture.
- **Mic**: the Echo's audio, converted by ffmpeg to PCM16LE 8 kHz mono, sent as `0x02` + one
  20 ms frame (320 bytes). Home Assistant forwards it to the panel only during a call.
- **Hang up**: on `stopIntercom` it sends `{"action": "hangup"}` only for a call the Echo placed
  or answered.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| After a call the Echo stays frozen on the last outdoor frame | Fixed in the integration (1.0.19 or later). |
| Video choppy or seconds late | Fixed in the same release (stream options order, decoder fps). Update. |
| Video but no sound on the Echo | *FFmpeg Output Prefix* missing or mistyped (setup step 5). |
| Echo opens but no call, or old behavior | An old version of the script is still running: *Save* and *Run*. In the Scrypted log of the device, an ffmpeg command with `-an` means the old script. |
| `audio_ws: error` in the log, 403 Forbidden from HA | Wrong or revoked token, or the Scrypted IP is banned after failed logins: remove it from `ip_bans.yaml` in the HA config folder and restart HA. |
| Connection closed with "Not allowed" | The token's user is not in the integration's *Users allowed to see rings and live media*. |
| No token field in the device settings | Expected: the token is the `TOKEN` constant at the top of the script. |
| Household noise answers a ring | Raise `VOICE_RMS` (default 800) in the script, then *Save* and *Run*. |

## Removing an old camera

If an older camera for the intercom exists (FFmpeg Camera, a previous script), remove it **from
the Alexa app first**, then delete it in Scrypted. The other way round, Alexa keeps a dead device
that can still get the doorbell announcement.
