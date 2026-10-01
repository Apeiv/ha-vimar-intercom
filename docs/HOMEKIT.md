# HomeKit: the integration's own video doorbell

The integration can publish the intercom to Apple Home by itself, as a video doorbell (HomeKit
category 18). It uses HAP-python, the library Home Assistant's HomeKit bridge is built on, and it
changes nothing in Home Assistant. It is off by default.


🇮🇹 *[Italiano](HOMEKIT.it.md)* · [← README](../README.md)

## Quick start

The integration can publish the intercom to the Apple Home app as its own video doorbell: ring
notification, live video, two-way audio, and the gate as a lock in the same view. It is off by
default. Home Assistant's HomeKit bridge cannot do this: its doorbell shows Talk, but never listens on
the port it gives the phone, so the visitor never hears you. The rest of this page has the details.

1. Settings → Devices & services → Vimar Intercom → **Configure** → **HomeKit** → turn on **Publish
   to HomeKit**.
2. The same HomeKit page (administrators only) now shows a QR code and an 8-digit code; a notification
   reminds you that pairing is waiting. In the Home app choose **Add Accessory** and scan it; iOS
   warns that the accessory is not certified, choose **Add Anyway**.
3. In the accessory settings, **Show as Separate Tiles** puts the gate in the live view.

| Option | Description |
|---|---|
| **Smoother video (re-encode)** | On (default): a keyframe every second, so a packet lost on the cloud relay is a brief smear instead of a freeze of up to 3 s; opening takes about 0.5 s longer. Off: the panel's own stream, faster to open |
| **During a ring** | *Answer when you talk* (default): opening the notification previews the street without answering, and your first word answers. *Answer when the view opens*: opening answers at once and the Tab stops ringing |

Closing the last HomeKit view ends a call that HomeKit placed or answered. Everyone who can use the
doorbell in the Home app (whoever paired it, and those the home is shared with) sees the video, talks
and opens the gate: `allowed_users` does not apply to HomeKit. Do not also pair the same
intercom through a Scrypted HomeKit bridge: the Home app would show two doorbells and every ring would
notify twice.


## Why not Home Assistant's HomeKit bridge

The bridge cannot carry the talk direction. Its doorbell camera advertises a speaker, and the Home
app shows the Talk button, but when the phone sends `SetupEndpoints` the bridge answers with the
phone's own ports as if they were the accessory's. Nothing on the Home Assistant side listens there:
Home Assistant's audio proxy only opens that port to send. The voice of whoever answers goes to a
socket nobody reads. On a test call the visitor could be heard clearly, and could not hear anything.

This cannot be fixed from outside Home Assistant, and it applies to every camera or doorbell exposed
through the bridge, because the bridge's doorbell is the same class as its camera.

## What the accessory contains

One accessory, so the live view shows video, Talk and the gate together:

| Service | Purpose |
|---|---|
| `CameraRTPStreamManagement` ×3 | Up to three views at once (iPhone, Mac, Watch) |
| `Microphone` | Voice from the street to the phone |
| `Speaker` | Voice from the phone to the street |
| `Doorbell` | The ring notification with the snapshot |
| `StatelessProgrammableSwitch` (optional) | The ring as a button for Home app automations, pressed at every ring. Off by default |
| `LockMechanism` "Cancello" (gate) | Opens the door through the same panel as the integration's lock. Reports unlocked, then returns to unknown after 5 s: the intercom only pulses the strike and cannot know whether the gate is shut, so it never reports locked |

The HAP server listens on port 21099, a fixed port: the phone pairs with it. The pairing state is
in `.storage/vimar_intercom.<entry>.homekit.state` and the setup code in `.homekit.pin`, both mode
0600. Deleting the integration entry deletes both files.

## Pairing and options

Settings → Devices & services → Vimar Intercom → Configure → HomeKit:

- Publish to HomeKit turns the accessory on or off. Until it is paired, this same HomeKit page shows
  the setup code and a QR code, and a notification says that pairing is waiting and where to find
  them. In the Home app choose Add Accessory and scan the QR, or choose More options, pick the
  intercom and type the code. iOS warns that the accessory is not certified; choose Add Anyway.
- The code and the QR are only on the options page, which only administrators can open. The
  notification does not carry them: Home Assistant shows notifications to every user, and pairing
  gives live video, Talk and the gate, which the `allowed_users` option keeps from other users. The
  QR image is served to administrators only, behind a random token, and stops working once the
  accessory is paired or turned off. HAP-python normally prints the code, with a QR, to standard
  output, which ends up in the container log; the integration turns that off.
- A new setup code is picked when the last Home app removes the accessory, after 10 wrong codes in a
  row (someone may be guessing it; a warning goes to the log), and when HomeKit is turned off before
  it was paired. The old code, which may have been in a screenshot or with a previous owner, no
  longer pairs. HAP-python itself has no limit on pairing attempts.
- Who can use the doorbell in the Home app is decided by Apple Home, not by Home Assistant: whoever
  pairs it, and everyone the home is shared with, sees the video, talks and opens the gate.
  `allowed_users` does not apply to them.
- Smoother video (re-encode), on by default. See below.
- During a ring. **Answer when you talk** (the default): opening the notification shows and plays the
  street without answering, so the Tab and the VIEW app keep ringing and can still answer; your first
  word (Talk) answers the call. **Answer when the view opens**: opening the notification answers at
  once and the Tab stops ringing.
- **Ring button for Home automations**, off by default. Adds a programmable button pressed at
  every ring, to use as a trigger in Home app automations. Turning it on or off keeps the other
  services' identifiers, so the gate and the camera stay as they are in the Home app; an
  automation that used the button stops firing while it is off.

Do not also pair the same intercom through a Scrypted HomeKit bridge: the Home app would show two
doorbells and every ring would notify twice.

In the Home app, turn on Show as Separate Tiles for the accessory. The gate then gets its own tile and
appears in the camera's live view. Without it, the gate stays inside the camera tile and the live view
cannot reach it.

Saving the options reloads the integration for a few seconds. The pairing survives.

## How audio and video travel

Street to phone. `media_handler` hands every decrypted audio packet of the call to the view's tap
(`homekit_media.AudioTap`), which forwards it to a local port described by a small SDP file. The
view's ffmpeg reads that, encodes Opus and sends it in the clear to `AudioBridge`, which encrypts it (SRTP) and sends it from the socket announced to the phone.
Anything from the phone passes a replay filter first (the last 128 packet indices, as libsrtp
keeps), so a captured packet sent again is not played to the street twice.
When the panel sends no audio for 0.2 s, the tap sends silence instead. During the ring preview the
panel sends video only, and without an audio stream coming in the phone sends nothing when Talk is
pressed, so Talk could never answer. The tap keeps the panel's own timestamps (a packet lost upstream
leaves its gap), stamps the silence by the wall clock, and after a relay stall drops the late burst
that falls inside the silence already sent, so a stall does not add to the phone's delay.
ffmpeg stamps Opus at 48 kHz whatever the real rate (RFC 7587); HomeKit wants the negotiated rate, so
the bridge rescales the timestamps.

Phone to street. The phone's voice arrives on the same socket (symmetric RTP). The bridge decrypts it
with the key the phone provided, restamps it at 48 kHz and hands it to a second ffmpeg that decodes it
to 8 kHz PCM. The PCM goes to the panel through `media.send_audio`, the same path as the intercom
card's microphone, paced at one 20 ms frame per tick. This second
ffmpeg starts with the first voice packet: an ffmpeg reading RTP exits after ten seconds without
input, and someone who opened the view and waited before pressing Talk was never heard. If it exits
during a long silence, it restarts on the next voice packet, and the packets that arrive while it
starts are kept. If it fails to start it is tried at most once a second, and after five failures in a
row not again for that view.

Every ffmpeg input is a local SDP file with a random name (mode 0600), read with
`-localaddr 127.0.0.1`: without it ffmpeg 8.1 binds the RTCP port of an SDP input on every address.

Video, re-encoded (the default). ffmpeg decodes the panel's video, concealing lost packets, and
re-encodes it with a keyframe every second. One encoder per call, shared by all views, started at the
ring when early media is available. `media_handler`'s video protocol is one per hub and serves every
call, so the end of a call (or a new ring) is what retires the encoder: the next visitor never gets
the previous one's picture, and no encoder starts once the call has no video.

Video, direct. No ffmpeg: the panel's H.264 packets go to the phone as they are, restamped for it
(negotiated SSRC and payload type, continuous sequence numbers) and encrypted. They come from a tap on
`media_handler`'s video protocol, behind its late-packet filter, each packet once. The group already in
memory (`media_handler`'s own keyframe cache, with SPS/PPS added when the panel sent them earlier)
goes first, from the keyframe on, with its timestamps squeezed into a few ticks so the phone
shows it at once instead of treating it as backlog. Then the live stream follows. If the panel
stream restarts with a new SSRC, the phone's stream carries on from where it was; packets of the old
stream still in flight are dropped for a second instead of being followed back. An RTCP sender
report goes out with the first packet and then every two seconds. This is how the VIEW app works: the panel's own stream, decoded by
the phone.

Early media. Since 1.0.9 the integration answers a ring with `183 Session Progress` and an SDP, so
the panel sends video and audio while the call keeps ringing. A view opened during a ring shows that
preview straight away, and with the default answer mode the call is answered only when you talk.

## Measurements (26 September 2026)

On an Elvox Tab 7S Up 40517 (Due Fili Plus EVO), over the cloud relay.

| | At home (Wi-Fi) | Away (5G) |
|---|---|---|
| Video, direct | under 2 s | about 3 s |
| Video, re-encoded | about 0.5 s more | about 0.5 s more |

Before this work opening took about 5 s. The VIEW app takes 2.5 s. The extra second away from home is
Apple's path (home hub, then iCloud relay): on our side the video starts after 1.5 to 1.6 s in both
cases.

Every view logs its own timeline at INFO, measured from the moment the phone asks for the stream: one
line at "stream started", and one when the first audio packet leaves for the phone. The second is the
number to compare with the VIEW app.

## Things to know

- If the log says "HomeKit video doorbell not started" with `Address in use`, something else holds
  port 21099. Home Assistant's own HomeKit bridges start at 21063 and count up, so it takes many of
  them to reach it. Only the doorbell is lost; the rest of the integration keeps working. Free the
  port (`ss -ulpnt | grep 21099` shows who has it) and reload the integration.

- The cloud relay loses packets, two to four in a hundred, before they reach Home Assistant. The phone
  counts exactly as many missing packets as we do, and Wi-Fi loses none. In direct mode a lost packet
  freezes the picture until the panel's next keyframe, up to 3 s, on a panel that ignores keyframe
  requests, as the 40517 does. The 40515 honours them and sends a keyframe about 0.25 s after a
  request. That is why re-encoding is the default.
- When the panel hangs up, the view stays open on the last frame. The integration ends the session
  within a millisecond (RTCP BYE, streaming available again), but HomeKit gives an accessory no way to
  close the Home app's viewer. Home Assistant's cameras behave the same. A view still opening when
  the call or the ring ends is closed too, and the shared encoder of that call is stopped.
- Away from home, the home hub decides the packet time. A hub asks for 60 ms audio packets, and a
  60 ms Opus packet does not fit in Home Assistant's default 188-byte RTP packet, so the integration
  uses 1316.
- Closing the last HomeKit view ends a call that HomeKit placed or answered: the Home app has no
  hang-up button. A ring that was only previewed is left ringing, and a call someone else is watching
  (a camera card, `/av`) stays up. If you press Talk and close the view before the panel confirms the
  answer, the call is hung up as soon as it does. The hang-up runs on its own, so a closing view
  never waits for the cloud to confirm it.
- For a call without video (the indoor monitor calling), the view shows a black picture with the audio.

## The trap that still applies: controllers cache what you can do

HAP-python computes the accessory's fingerprint with `include_value=False`:

```python
# pyhap/accessory_driver.py
return hashlib.sha512(util.to_sorted_hap_json(
    self.get_accessories(include_value=False))).hexdigest()
```

Characteristic values do not count, and the list of resolutions or audio codecs is a value, not
structure. You can change everything the doorbell says it supports and the configuration number `c#`
does not move, so phones never re-read the accessory and keep the copy they took at pairing. The
symptom: the phone connects, downloads the snapshot and never asks for a stream, with no error
anywhere.

Adding or removing a service changes the structure and therefore `c#`. For a values-only change,
increment `config_version` by hand in the state file while Home Assistant is stopped. The pairing
survives.

## Reading a session

Everything is in the integration's own log, `/api/vimar_intercom/debug` (administrators only; Home
Assistant's log only gets warnings unless you raise the level). For each view:

```
HomeKit: stream started to 192.168.1.20 (PID 4242, video re-encoded) in 690 ms (stream_opened 1 ms, call 2 ms, transcoder 3 ms, keyframe 4 ms, video begin 4 ms, first packet 5 ms, ffmpeg 690 ms)
HomeKit: ring answered (Talk)
HomeKit: audio bridge closed. To phone 1409 packets, from phone 220 (0 failed to decrypt, 0 replayed), 219 voice frames to the intercom
HomeKit: direct video closed. 1188 packets to the phone (6 from the initial group), 0 backlog packets dropped | lost BEFORE us (panel/relay): 15, arrived late: 0 | the phone asked for 25: 0 we had and resent, 25 we never had | RR: cumulative loss 15, jitter max 12 ms | RTCP from the phone {'RR': 66, 'TMMBR': 55, 'PLI': 25}
HomeKit: last view closed, hanging up
```

- `in N ms (...)`: where the opening time went, each stage in ms from the moment the phone asked.
- `from phone 0` while someone pressed Talk: the voice is not arriving.
- `lost BEFORE us` and the `PLI` count: relay losses, and the phone asking for a keyframe.
- An address that differs from the iPhone's is the home hub: the view is from away.

## History: the Home Assistant bridge

Until 26 September 2026 the intercom went through Home Assistant's HomeKit bridge in accessory mode.
Two traps cost days there: the `c#` cache above (changed resolutions the phones never saw, and a
stream that never started), and the doorbell sensor Home Assistant links by itself from the same
device unless you point it at one that does not exist. If you used the bridge for the intercom, remove
its `homekit:` entry before turning this accessory on.
