# External video tools: Scrypted, go2rtc, Frigate

How to feed the intercom video to tools running next to Home Assistant, without them ever
calling the panel on their own.

## The one rule: use the passive URL

The integration has no always-on RTSP camera. Video exists only while the doorbell rings
(preview) or a call is up (about 10 s for a "view" on a Tab 5S Up). It is served as MPEG-TS
(H.264 + PCMU) on `http://<ha>:8123/api/vimar_intercom/av`, LAN only, no token.

- **`/api/vimar_intercom/av`** (plain): opening it while idle **places a call** to the panel
  ("Vedi esterno"). It is meant for Home Assistant's own camera stream and the card. A tool that
  reconnects in a loop would keep calling a shared building panel: **never point Scrypted, go2rtc
  or Frigate at this URL**.
- **`/api/vimar_intercom/av?autocall=0`** (passive, also `?mode=passive`): never calls. While a
  ring or a call is active it attaches to the same stream everybody else gets (panel video and
  audio, no re-encoding); otherwise it answers **503 at once**, so a reconnect loop is cheap and
  harmless. It does not count as a viewer either: a call started for Home Assistant's camera
  still ends when that viewer leaves.
- **`/api/vimar_intercom/av?autocall=0&idle=image`** (passive, continuous): same rule, never
  calls, but the stream **never ends**. While idle it shows a dark standby frame ("Standby",
  intercom icon); when the doorbell rings or a call is up it switches to the panel's live video
  on the same connection, and back to standby afterwards. One H.264 stream with constant
  parameters (640x480, 10 fps, keyframe every second, baseline, video only), re-encoded by a
  single ffmpeg shared by all clients (started with the first, stopped with the last; measured
  0.8 % of one core while idle and 1.4 % while live on an i7-12700H, so expect a few percent on
  a small home server). **This is the one to
  use for Frigate, Scrypted and go2rtc**: no reconnect loops, no "camera offline" logs, and
  Echo Show opens the view instantly. Needs an ffmpeg with `libx264` (Home Assistant OS has it).

Everything below uses the continuous passive URL. `<ha>` is the LAN IP of Home Assistant
(`127.0.0.1` when the tool runs on the same host with host networking).

## Doorbell trigger

The ring is the `event` entity `event.vimar_intercom_doorbell` (event type `ring`). Nothing else
is needed on the HA side: an automation on its state change forwards the ring wherever you like
(examples below). Test it with the service `vimar_intercom.simulate_ring`.

## Scrypted (Alexa chime + Echo Show live view)

1. **Camera**: add a device with the *FFmpeg Camera* plugin. Stream URL
   `http://<ha>:8123/api/vimar_intercom/av?autocall=0&idle=image`. Leave the stream "as is"
   (H.264 baseline, no audio track). Snapshots come from the stream: the standby frame while
   idle, the visitor during a ring. Prebuffer/rebroadcast can stay on: the stream is continuous
   and cheap.

   Alternative, if you already run go2rtc: restream it there and add the go2rtc RTSP URL to
   Scrypted instead (see the go2rtc section).

2. **Doorbell button**: install the *Dummy Switch* and *Webhook* plugins. On the camera, enable
   the *Doorbell Button* extension (from Dummy Switch): Scrypted now treats the camera as a
   doorbell. On the button device, enable *Webhook* and copy the `turnOn` URL.

3. **Home Assistant → Scrypted**: a `rest_command` on the ring:

   ```yaml
   rest_command:
     scrypted_doorbell:
       url: "http://<scrypted>:11080/endpoint/@scrypted/webhook/public/<id>/<token>/turnOn"
       method: get

   automation:
     - alias: "Intercom - Ring → Scrypted doorbell"
       trigger:
         - platform: state
           entity_id: event.vimar_intercom_doorbell
       action:
         - action: rest_command.scrypted_doorbell
   ```

4. **Alexa**: with the Scrypted *Alexa* plugin, sync the camera. A doorbell camera gives the
   "Someone is at the front door" chime on the Echos and a live view on Echo Show ("Alexa, show
   the intercom"). Alexa ignores `DoorbellPress` events closer than about **30 s** apart, and the
   live view only works while the ring or call is still up (the preview lasts as long as the
   ring; a viewed call about 10 s), so ask early.

## go2rtc (restream for Scrypted, Frigate, WebRTC)

```yaml
streams:
  vimar: "ffmpeg:http://<ha>:8123/api/vimar_intercom/av?autocall=0&idle=image#video=copy"
```

The stream is `rtsp://<go2rtc>:8554/vimar` (and WebRTC/MSE from the go2rtc UI), always up.
With the plain passive URL (`?autocall=0`, panel audio included, `#video=copy#audio=copy`) it is
empty while idle: go2rtc's producer starts on demand and retries, each retry one cheap 503.

## Frigate

Frigate's bundled go2rtc does the restream; the Frigate camera reads it. The stream is always up
(standby frame while idle), but the standby frame is not worth recording or detecting on:

- **no continuous recording** (hours of standby) and **no detection** (a static frame, then a
  10 s burst of video: not enough for Frigate's tracker to be useful, and it would run the
  detector on the standby frame all day);
- **record on the doorbell event** instead: an HA automation on the ring creates a manual event
  through Frigate's API, and Frigate keeps the recording around it.

```yaml
# frigate.yml
go2rtc:
  streams:
    vimar: "ffmpeg:http://<ha>:8123/api/vimar_intercom/av?autocall=0&idle=image#video=copy"

cameras:
  vimar:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/vimar
          input_args: preset-rtsp-restream
          roles: [record]
    detect:
      enabled: false
    record:
      enabled: true
      retain:
        days: 0          # only the events below are kept
```

```yaml
# Home Assistant: ring → manual Frigate event (recorded ~30 s around it)
rest_command:
  frigate_doorbell:
    url: "http://<frigate>:5000/api/events/vimar/doorbell/create"
    method: post
    content_type: application/json
    payload: '{"duration": 30, "include_recording": true}'

automation:
  - alias: "Intercom - Ring → Frigate event"
    trigger:
      - platform: state
        entity_id: event.vimar_intercom_doorbell
    action:
      - action: rest_command.frigate_doorbell
```

Check the `record` retention keys against your Frigate version (they moved between 0.13 and
0.15); the idea is the same: keep events, not continuous footage.

## Troubleshooting

- **403**: the request did not come from the LAN, or it passed through a proxy
  (`X-Forwarded-For`). Talk to Home Assistant directly, by LAN IP or `127.0.0.1`.
- **503 always, even during a ring**: the integration is not registered (check the
  `binary_sensor` / status), or the tool is hitting the plain URL from outside the cooldown and
  the panel refused (486). Use `?autocall=0`.
- **503 with `idle=image`**: ffmpeg could not start the encoder; the log says why. Usually an
  ffmpeg without `libx264` (containers with a minimal build).
- **The panel gets called when nothing rings**: the tool uses the plain `/av`. Switch to the
  passive URL.
