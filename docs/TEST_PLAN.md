# Field test plan

The full round to say the integration works, not that it seems to. Where a test has an expected
number, it was measured on an Elvox Tab 7S Up (40517) over the cloud relay.

How to report results: say which test you ran, roughly when, and what you saw. The rest is already
recorded in the integration's own log (`/api/vimar_intercom/debug?lines=N`, administrators only),
with the SIP trace of every call and the media counters when it ends.

At home means the iPhone on Wi-Fi, streaming straight to the Home Assistant host. Away means the
iPhone on mobile data, going through the home hub (Apple TV or HomePod) and Apple's relay: about one
second slower is normal.

The sections on HomeKit apply only with the optional HomeKit doorbell turned on.

---

## 1. HomeKit: pairing and options

| # | Test | Expected |
|---|---|---|
| 1.1 | In the Home app the intercom accessory is there, with the doorbell/camera and the gate in the same room | Everything responds; no "No Response" |
| 1.2 | Options → HomeKit: turn Smoother video off and on | It saves, the integration reloads in a few seconds, and the accessory stays paired |
| 1.3 | Options → HomeKit: switch "During a ring" between its two choices | It saves; the next ring behaves as chosen (answer on Talk, or answer when the view opens) |
| 1.4 | Save that page, then open Settings and save without changes | The HomeKit choices stay as they were |
| 1.5 | (Optional, costs a new pairing) Turn Publish to HomeKit off, then on | Off: the accessory disappears. On: it comes back paired; if not, the QR notification appears |

## 2. Opening the camera from the Home app (nobody rang)

Half of these with Smoother video off, half with it on.

| # | Test | Expected |
|---|---|---|
| 2.1 | Open cold (no call for at least a minute), at home | Picture in under 2 s (re-encoded: about half a second more) |
| 2.2 | The same away, on mobile data | Picture in about 3 s; the view does not die after a second or two |
| 2.3 | Keep it open 30 s while cars pass | Direct: smooth, but can freeze up to 3 s when the relay loses packets. Re-encoded: always moving, at most a brief smear. Never a grey blob |
| 2.4 | Close and reopen at once, five times | Always the same; no opening stays black |
| 2.5 | Open on two devices at once (iPhone and Mac) | Both see it |
| 2.6 | Close all views | The call ends by itself within a few seconds (the street panel light goes off) |
| 2.7 | Restart Home Assistant, then open at once | No slower than usual: the video parameters are saved on disk |

## 3. Someone rings (needs someone at the street)

| # | Test | Expected |
|---|---|---|
| 3.1 | Ring the bell | One HomeKit notification (plus the Vimar app's, if still enabled) |
| 3.2 | Open the notification | Picture faster than opening by hand: the ring's early media is already flowing |
| 3.3 | From the view: Talk, and listen | Voice both ways, clean |
| 3.4 | From the view: open the gate | Asks for confirmation, opens, notifies the unlock once; after a few seconds the state returns to unknown, with no "locked" notification; the call stays up |
| 3.5 | Ring and answer from the indoor monitor | The indoor monitor answers and opens as always; Home Assistant does not steal the call |
| 3.6 | Ring and do not answer | The ring times out cleanly; no call left hanging; street panel light off |
| 3.7 | Ring twice a few seconds apart | The second ring behaves like the first |
| 3.8 | Answer from the iPhone and let the street panel hang up | Audio stops and the view stays on the last frame until you close it. That is a HomeKit limit: the Home app does not let an accessory close its viewer. Nothing may stay hanging |
| 3.9 | An entrance panel without a camera rings (if the plant has one) | Ring and voice work; the SDP answer has no video line |

## 4. Talking

| # | Test | Expected |
|---|---|---|
| 4.1 | Open the view, wait 15 s, then press Talk | The street hears you from the first word |
| 4.2 | Talk, stay silent 20 s, talk again | The second time is heard too |
| 4.3 | The same away, on mobile data | Voice both ways |
| 4.4 | The intercom card in Home Assistant: listen, then talk | Voice both ways |

## 5. The indoor monitor calls Home Assistant (from its display)

| # | Test | Expected |
|---|---|---|
| 5.1 | Call Home Assistant from the display and answer | Ring, then voice both ways |
| 5.2 | Look at the video during that call | A black placeholder: that call has no video |
| 5.3 | Hang up on the monitor | The call ends at once in Home Assistant too |

## 6. Preview image

| # | Test | Expected |
|---|---|---|
| 6.1 | After a call, look at the tile in the Home app | The last frame of the call |
| 6.2 | Restart Home Assistant and look again | The placeholder until a new call, then the last frame again. Never an empty rectangle |
| 6.3 | The camera entity in Home Assistant | Real frames during a ring or a call; never "unavailable" between calls |
| 6.4 | The camera's stream in Home Assistant while Home Assistant is behind a reverse proxy | The stream opens (the stream source is a loopback URL on Home Assistant's own port) |

## 7. Voicemail and Do not disturb

| # | Test | Expected |
|---|---|---|
| 7.1 | Turn voicemail on and off from Home Assistant | The switch moves at once; the monitor shows the change |
| 7.2 | Turn Do not disturb on from Home Assistant | LED blinking, as from the Vimar app |
| 7.3 | Change the state on the monitor | Home Assistant follows |
| 7.4 | Change the state from the Vimar app | Home Assistant follows |

## 8. Living with the Vimar app

| # | Test | Expected |
|---|---|---|
| 8.1 | Use the Vimar app normally with Home Assistant running | No kick-outs, no lost registration |
| 8.2 | Ring and answer from the Vimar app | Works as always; Home Assistant does not interfere |
| 8.3 | The devices sensor after a while | The devices that talked show up |

## 9. Failures and restarts

| # | Test | Expected |
|---|---|---|
| 9.1 | Restart Home Assistant | Registers again by itself; the accessory is reachable again in Home |
| 9.2 | Restart Home Assistant during a call | No call left hanging on the intercom |
| 9.3 | Cut the host's network for about a minute | Reconnects and registers without help; a failed renewal starts the reconnection at once, not at the next keepalive |
| 9.4 | Restart the intercom | Registration comes back when it does |

## 10. Time only (leave it running)

| # | Test | Expected |
|---|---|---|
| 10.1 | At least 90 minutes | Registration renewed on every keepalive; registration never lost; no warning about a short Expires |
| 10.2 | One night | No reconnections, or reconnections followed by a successful registration |
| 10.3 | Home Assistant's log at the end of the day | No `vimar_intercom` warnings for normal ffmpeg shutdowns, cancelled calls or relay duplicates; a real ffmpeg failure does show as a warning |

---

## A note on privacy

The preview is a photo of whoever was at the door. It is kept in memory until the next call and is
gone after a restart; it is not written to disk unless a snapshot folder is set in the options.
