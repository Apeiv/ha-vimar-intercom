# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versioning: [semver](https://semver.org/).
Newest entries on top. **Entries are written in English from 1.0.1 onwards**; earlier ones are in
Italian and are kept as they were written.

## [1.0.13] - 2026-09-28

### Added

- **`vimar_intercom.find_sga`: find the PICG without the phonebook**
  ([#14](https://github.com/lollox80/ha-vimar-intercom/issues/14), PR #18 brought up to date). It is
  for cloud-only systems where neither the intercom's local HTTP API nor the cloud phonebook (1.0.12)
  is available.
  - The action probes a small range of addresses one at a time, with a pause between probes:
    default `55000`–`55010`, at most 50.
  - It stops at the first reply. With `GET_NICKS` (the default) the intercom declares its own PICG in
    `GET_NICKS_REPLY`.
  - Each probe is reported with the three-outcome rule: `absent` (404), `exists` (accepted, no reply)
    or `replied`.
  - A late `GET_NICKS_REPLY` still counts, because it names the PICG by content.
  - **`probe: get_init_status`** is the fallback for the 40515 in cloud mode where `GET_NICKS` got
    `Timeout`. There the PICG is the address whose probe triggered the reply, and a late reply is
    reported as two candidates. Sent to the real SGA, it makes the VIEW app show "Configurazione
    appartamento modificata".
  - `sip_timeout` (default 8 s) bounds each probe; `do_system_message` gained a `timeout` argument.
  - Nothing is written unless `apply` / `apply_sga` is set.
  - **Admin only**, like `send_command`, since it sends MESSAGEs to a range of addresses and can
    change the configuration.
- `GET_NICKS_REPLY` is parsed wherever it arrives: the declared nicknames and PICG are kept in the
  hub's stats.

## [1.0.12] - 2026-09-28

### Added

- **The intercom is found on the network** ([#6](https://github.com/lollox80/ha-vimar-intercom/issues/6)).
  The Tab announces `_eipvdes._tcp` over mDNS, the service the VIEW app looks for, and Home Assistant
  now offers it under *Discovered*. The QR or the credentials are still asked for, since the TXT record
  carries no secret, but the address and the local SIP domain come from the record:
  - `proxy` fills the intercom address;
  - `domain` becomes the local SIP domain. On a 40515 whose QR says `domain=127.0.0.1` this is the
    Tab's own address, instead of the cloud domain that local registration refused with `503`. This
    is not yet verified on such a plant;
  - on the 40507 it is the cloud domain, as before.
  Only `mac`, `proxy` and `domain` are relied on (the 40507 record has four keys, the 40515 eleven);
  `dev` and `fver` are shown when present. An intercom already set up is recognised by its MAC in
  any notation (or by its address, for manual entries without MAC) and not offered again; when its
  address changes, the entry follows it and reloads (local mode only). No `ZeroconfServiceInfo`
  import, which exists only from HA 2024.12: the blocker of the old PR #7.
- **Phonebook from the Vimar cloud** ([#5](https://github.com/lollox80/ha-vimar-intercom/issues/5)):
  options → *Download the phonebook from the Vimar cloud*. It uses the recipe @CPietro verified on a
  40515 / 2FV2:
  - `GET https://<cproxy>/phonebook/domains/<cdomain without .cproxy>/<rubrica_ver>`;
  - HTTP Digest with the **full** `cdomain` as user and the `token` as password;
  - `User-Agent: TOGA/2.4.0`.
  The token and `rubrica_ver` come from the plant's long `GET_INIT_STATUS` reply; if the token is
  missing the integration asks once and waits a few seconds. The file is the VIEW app's own
  `rubrica.db` and goes through the usual import confirmation; the GID offered is the one the plant
  declares. Plants with the short reply (no token, like the 40507) get a message pointing to the LAN
  download or the file import.

### Security

- The token is never stored: it stays in memory from the last reply. It is masked in the
  *Last received message* sensor, which any user can read. Errors never include it.

## [1.0.11] - 2026-09-28

Closes [#9](https://github.com/lollox80/ha-vimar-intercom/issues/9) and
[#4](https://github.com/lollox80/ha-vimar-intercom/issues/4), with the long `GET_INIT_STATUS_REPLY`
that @CPietro published from a Tab 5S Up 40515 / 2FV2.

### Changed

- **Voicemail and Do Not Disturb no longer show a guess as a fact** (#9). After a command that
  the plant accepted, the switch shows the commanded value for at most 10 s, and meanwhile
  asks the plant for its state (`GET_INIT_STATUS`). The Tab's announcement (`VOICEMAIL;`/`DND;`)
  or the reply replaces it at once, even when it says nothing changed. With neither, the state
  becomes *unknown* instead of keeping the commanded value forever. After a restart only a state
  the Tab had confirmed is restored, not the last command. A failed command now raises an error.
- **`media_enc` has three values: Automatic (new default), On, Off** (#4). Automatic follows the
  `media_enc` the plant declares in the long reply (`"srtp"` → SRTP); a plant with the short reply
  (40507 / 2F) stays on plain RTP. Entries saved as on by earlier versions stay on; off becomes
  Automatic, which on plants that do not declare it is the same thing.

### Added

- **Voicemail delay** `select` (#4): `vm_timeout`, with the values the plant itself declares in
  `vm_timeout_values`, written with `SET_APT_PARAMS` (`Panda: set`) and changed only when the reply
  says `ERR_NONE` (no reply or no `ERRCODE` is a failure, as in the VIEW app). The entity is
  created when the plant first sends the list, so it does not appear at all on plants with the
  short reply. `APT_PARAMS_CHANGED` from the Tab or the app updates it.
- The state sensor shows the apartment `GID`, `apt_names`, the declared `media_enc` and whether
  SRTP is in use, on plants that send them.

## [1.0.10] - 2026-09-28

Small follow-up to 1.0.9, from the first days of use on the 2F (Tab 7S 40507).

### Changed

- **"Rispondi" (Answer) is available only while the doorbell rings.** At rest the button is greyed
  out instead of accepting the press and logging `Answer failed: Nessuna chiamata in arrivo`. To
  look at the entrance panel, open the camera: answering is for a ring.
- The intercom card finds its entities by itself. Entity ids change from one installation to
  another (the device's area, renames), so the defaults `sensor.vimar_intercom_intercom_stato` and
  `lock.vimar_intercom_serratura` often did not exist and the card showed "offline". An entity
  written in the card config still wins when it exists; otherwise the card takes the
  integration's camera from the entity registry, and the state, last ring and lock from the
  camera's new `card_entities` attribute.

### Fixed

- The *Open door*, *Call*, *Answer* and actuator **buttons now raise an error when they fail**
  (the lock already did in 1.0.9): `button.press` used to "succeed" with the door still closed and
  only a line in the log ([#23](https://github.com/lollox80/ha-vimar-intercom/issues/23)).
- *Last opening*, *Last ring*, *Last caller*, *Last call duration* and *Last missed call* keep
  their value (and attributes) across a Home Assistant restart, until the next event replaces it.
  They used to go back to "Unknown". The counters since start-up are not restored.

### Added

- DEBUG log of the outgoing voice level every 2 s (`Voce verso la targa: picco N/32767`): the
  `tx=` packet count says packets leave, not that they carry a voice, so a muted or wrong
  microphone can now be told apart from a panel that does not play it.

### Changed

- `/av` audio is AAC-LC (48 kHz mono, 32 kb/s) instead of the panel's raw G.711. In MPEG-TS
  PCMU ends up as private data (`bin_data`), so HA's stream worker (HLS, `camera.record`) and
  HomeKit had no audio. Transcoding and low-latency mux flags as in #21 by @m4r1k; 48 kHz
  instead of 24 because the muxer holds the first video packet until the first AAC frame
  (measured +10 ms vs +60 ms). Video is still copied.

### Removed

- The iOS push code, as in #21 by @m4r1k: `push_sender.py` (APNs VoIP over aiohttp, which
  speaks HTTP/1.1 while APNs needs HTTP/2, so it could never deliver), the
  `/api/vimar_intercom/push_token` view that stored device tokens inside the integration's
  folder, its call on ring and the `APNS_*` constants. With `APNS_KEY_ID` empty none of it ever
  ran; the "APNs push not configured" warning at every start is gone. The Vimar-cloud FCM
  parameters (`PN_*`, `connectProfiles`) are unchanged.

## [1.0.9] - 2026-09-28

Builds on 1.0.8. Field-tested on a Tab 5S Up 40515 (cloud TLS + SRTP).

### Added

- Video preview while the doorbell rings (early media): the ring gets `183 Session Progress` with
  our SDP instead of a bare `180 Ringing`, so the panel streams video before anyone answers, as
  the VIEW app's preview does. Snapshots and `/av` work during the ring; answering reuses the same
  SDP and SRTP keys. Not done during one of our own calls. A UDP retransmission of the same
  INVITE gets the same response instead of counting as a new ring; a ring whose CANCEL never
  arrives ends after 90 s; placing a call while it rings is refused ("Squillo in corso").
- Away message: options `away_message_file` (mp3, wav...) and `away_message_delay` (seconds,
  0 = off, max 60). If that ring is still ringing after the delay (nobody answered from the panel,
  a phone or HA), the file is decoded first (max 30 s; an unreadable file leaves it ringing), then
  the integration answers, plays it at real time (8 kHz, 20 ms RTP packets, SRTP included) and
  hangs up only its own call. `answer` during the message takes the call over.
- Ring snapshot: options `snapshot_dir` (folder, must be writable by HA; empty = off) and
  `snapshot_delay` (seconds after the ring, default 3 for the Tab 5S Up 40515). On every ring the
  integration saves the visitor's photo from the preview as `squillo_YYYYMMDD_HHMMSS_mmm.jpg` and
  `ultimo_squillo.jpg`.
- Dashboard card `custom:vimar-intercom-card`, loaded by the integration (no Resources entry):
  live video only during a ring or call (opening the card never calls the panel), **Vedi
  esterno** when idle, buttons that follow the call state, **Apri** with two taps, and two-way
  audio over `/api/vimar_intercom/audio_ws` (signed path, 8 kHz PCM, browser echo cancellation).
  The microphone needs HTTPS; over HTTP the card still answers (video only). Microphone and
  WebSocket close when the call ends or never starts.
- Card: one row without video (photo of the last ring, name, state, last ring, buttons); the 4:3
  video pane appears on ringing / calling / in call and goes away when idle (1.5 s after a
  hang-up, buttons off, so a second tap does not land on the card below). The buttons keep their
  slots. Option `layout`: `overlay` (default, the card is the video, buttons on a dark strip at
  the bottom) or `sotto` (video above the row, buttons under it). Visual editor (camera, name,
  layout, history); the card is "Citofono Vimar" in the card picker, with a preview. Errors
  replace the state line for a few seconds instead of a permanent line under the buttons.
- Card: low-latency video. Over HTTPS, with WebCodecs (Chrome, Edge, Firefox, Safari/iOS 16.4+),
  the card decodes the panel's H.264 NALs from `audio_ws` and paints them on a canvas: first
  frame about 0.1 s after the panel sends it (HA's stream took 2-4 s, past half of the ~10 s the
  40515 allows), without opening `/av`. A ring joined mid-GOP gets the current GOP replayed, so
  the first frame does not wait for the next IDR. No WebCodecs, or an unsupported codec: HA's
  stream as before. A decoder error or a dropped WebSocket does not fall back (HA's stream is
  blank off-LAN, no TURN): the decoder restarts at the next IDR, the WebSocket reopens and gets
  the current GOP again. Frames are dropped down to the next IDR when the decoder falls behind.
  RTP the panel still sends after our BYE is discarded until the next call (it used to reach the
  card and the replayed GOP); a call placed during the 1.5 s hold gets a fresh player; a card
  HA detaches and re-attaches during a call (view switch) shows the video again at once.
- Card option `anchor` (default `citofono`, empty = off): with `#citofono` in the URL (e.g. a
  notification opening `/lovelace/camera#citofono`) the card scrolls itself into view, also on a
  later hash change.
- Ring history: with `snapshot_dir` set, every ring is logged in `squillo.json` next to the photos
  (time, photo, caller, outcome; last 200), and the outcome becomes `answered` on `answer` or
  `away` when the away message picks up. New authenticated endpoints `GET /api/vimar_intercom/rings`
  (newest first, `?limit=` max 50) and `GET /api/vimar_intercom/rings/<name>` (only
  `squillo_YYYYMMDD_HHMMSS_mmm.jpg` inside `snapshot_dir`). The card shows the latest rings (option
  `history`, default 8, 0 = off) with photo, time and outcome; a tap opens the photo large. The
  ring photo is now named after the ring time, not the moment it was taken.
- "Vedi esterno" lasts as long as the panel allows (about 10 s on the Tab 5S Up 40515): when the
  panel hangs up the video ends and the card closes. To look again, press "Vedi esterno" again, as
  on the in-home monitor.
- Admin service `vimar_intercom.simulate_ring`: fires the doorbell event (and the automations
  on it) without the entrance panel. No SIP, push, WebSocket broadcast or statistics.

### Fixed

- Stills (`camera.snapshot`, notifications) come from a per-call frame grabber instead of the
  stream: one ffmpeg decodes the H.264 NALs from the first SPS of the call (or ring preview) and
  keeps the latest JPEG, so a snapshot is instant instead of waiting for the next IDR (>8 s at
  night on the 40515). They also work during the ring. No image and no call outside a call.
- `/av` clients share one ffmpeg: go2rtc (WebRTC) and HA's stream worker open it together, and
  each new client used to kill the previous one's ffmpeg. A slow client is dropped instead of
  corrupting the MPEG-TS for everyone. Two simultaneous opens place one call, not two.
- First decodable frame in about 1 s instead of the next in-band IDR (~8 s on the 40515, past
  the panel's hang-up): SPS/PPS are cached even with no WebSocket client and written to the AV
  SDP (`sprop-parameter-sets`), and a keyframe is requested when `/av` attaches.
- Local UDP mode: in-dialog requests (BYE, re-INVITE, INFO) from the panel's own address are
  accepted when the plant has no Record-Route: the source filter also allows the hosts of the
  current dialog (Contact/Via of the ring, Contact of the call). Before, HA stayed "in call" and
  the panel kept retransmitting its BYE.
- Auth retries are capped (at most 2 per INVITE or INFO, and only on `stale=true` or a new
  nonce): a retransmitted 407 no longer produces an INVITE storm. The proxy challenge is cached,
  so the keyframe INFOs are sent with `Proxy-Authorization` from the first one (no 407 for each
  of the 8 in the initial burst), and an authenticated resend waits for its own answer.
- Incoming INVITEs are keyed on (Call-ID, Via branch): the second branch of a forked ring (the
  cloud relay sends two INVITEs a few ms apart) gets `482 Loop Detected` on its own Via, and a
  CANCEL is matched on the branch too. The relay's CANCEL of its duplicate branch, sent ~70 ms
  after our 200 OK, no longer ends the call just answered. The 200 OK is not retransmitted and a
  missing ACK never turns into a BYE: over the cloud relay the ACK of our 200 never arrives.
- Frame grabber: the first IDR is kept as the photo until a later one replaces it (with an IDR
  every ~3 s, or >8 s at night, a ~10 s view could end with no photo); a re-INVITE with a new
  SDP restarts the grabber without clearing the photo already taken.
- The GOP cached for the `/av` replay is filled after the reorder filter: an old keyframe
  resent by the 40515 no longer resets it to "old IDR fragment + new P frames".
- `away_message_file` must be inside a folder HA may read (`is_allowed_path`, like
  `snapshot_dir`) and is passed to ffmpeg as `file:<path>`.
- `/av`: ffmpeg is spawned off the event loop and its stderr reader task is kept referenced.
- Card: the video WebSocket reopens with a backoff (1, 2, 4... s, max 10) and stops when the
  player is closed; the microphone is released if the card left the page while the browser asked
  for permission; a failed photo tap shows its error on the card.
- A single RTP packet lost on the cloud path (`FU-A seq gap ... gap=1, continuing` in the log)
  produced a NAL with a hole and smeared video on the card until the next IDR (~3 s). A gap
  inside an FU-A now drops that NAL, P slices are dropped for the WebSocket clients, the frame
  grabber and the GOP replay until the next IDR (the canvas freezes on the last good frame
  instead), and a keyframe is requested at once (at most one per second). `/av` still gets the
  raw RTP: ffmpeg does its own concealment.
- `/av` waits up to 25 s for the call (cloud call setup sometimes takes ~15 s), and a client
  that leaves early no longer leaves the viewer count, and so the auto-hangup, stuck.
- Card: each button keeps its place (call | voice | door), so "Riaggancia" no longer slides under
  the finger that meant "Parla". Failed services (`{ok: false}`) and a failed door opening (the
  lock now raises an error) show on the card; a double tap on Parla/Rispondi places one call; the
  microphone is released on failure; "Audio interrotto" when the audio channel drops; iOS/Safari
  audio works (AudioContext created in the tap); the second tap of Apri after the 3 s re-arm works.
- Speaking into the card's microphone takes the call: the away message no longer hangs it up and
  an auto-call is no longer hung up when the video closes. An INVITE during a call no longer turns
  the state into "ringing", and `answer` during a call is refused instead of breaking it.
- SIP: hanging up while the panel rings sends CANCEL (before, the call went up anyway on the late
  200 OK); a late or orphan 200 OK gets ACK + BYE, a retransmitted 200 OK of the current call gets
  its ACK again, the ACK of a 4xx/6xx uses the INVITE's branch, and a 45 s INVITE timeout sends
  CANCEL. re-INVITE and UPDATE inside the call get 200 OK instead of a new ring. A second INVITE
  while busy gets `486 Busy Here` (a 603 made the PBX cancel the call on the other phones too); a
  ring that times out gets `480`. Responses to other methods no longer reach the INVITE
  transaction. A retransmitted 407 is only ACKed.
- TLS: the reader no longer deadlocks on reconnect (the new REGISTER waited for responses only the
  reader itself could read: ~2.5 min deaf after every server close); it uses the same dispatcher
  as UDP; a negative Content-Length no longer freezes the event loop.
- Media: outgoing voice leaves in 20 ms packets from a pacer (the browser sent 43 ms bursts against
  our `a=ptime:20`); no voice during the ring preview or when the panel says `sendonly`; never
  plain RTP inside an SRTP session. Video: the reorder buffer no longer stalls on old or restarted
  sequence numbers, SRTP keeps rollover state per SSRC, RTP header extensions are skipped, and
  video reaches ffmpeg as PT 96 whatever the panel's payload type (black `/av` before).
- HTTP views follow the active config entry (after removing and re-adding the integration they
  answered 503 until HA restarted); WebSocket broadcasts no longer fail when a client connects
  mid-send. `fetch_local` has its service strings.
- A retransmitted INVITE after we answered gets the 200 OK again instead of silence (a lost 200
  over UDP used to end the call), and the 90 s ring timer is cancelled when the ring ends.
- Hanging up during a ring no longer stops its preview, so a later answer has audio and video. A
  malformed SDP in the ring no longer silences the doorbell (the preview is skipped instead). A
  second INVITE or a failed 200 OK no longer leaves the preview running.
- The ring photo folder can't be under `/config/www` (served on `/local` without login); disk
  errors are logged, and a reload during the delay cancels the photo.
- While the doorbell is ringing, opening the stream neither calls the panel (603) nor answers:
  a dashboard or wall tablet with the camera open would otherwise steal the ring from the
  in-home panel. Answer explicitly with the `answer` service.
- The `open_door` service default follows the lock's door target.
- A BYE for another dialog no longer ends the active call (481 instead of 200).
- Answering clears the pending ring before the first await, so two answers (timer + user) can't
  send two 200 OKs.
- `send_command` defaults to the configured SGA instead of a hardcoded 55001; a WebSocket panel
  switch can no longer leave SIP broadcasts muted if cancelled.
- A doorbell ring right after the panel ended a view call got `603 Decline`, which the PBX
  propagated, cancelling the ring on the Tab too. Only a call we are placing or holding now
  suppresses a ring.
- `/av` answers 503 as soon as nothing is coming (call refused, cancelled or ended) instead of
  spinning for 25 s and counting as a viewer. With the iOS app's WebSocket connected it still waits
  (up to 25 s) for the call the app places. The call end reaches HA without the 3 s wait for
  ffmpeg to quit.
- BYE is retransmitted over UDP like the other requests (a lost BYE left the panel busy, 486 on
  the next view), waits only for its own response (not the 200 of a keyframe INFO), and the call
  is closed locally even when the BYE can't be sent (TLS down): before, it stayed "in call" with
  the media on and every ring got 486. Crossed BYEs no longer tear down what came next. Declining
  a ring with the connection down ends the ring anyway.
- The INVITE is retransmitted over UDP (Timer A): a lost first INVITE made "Vedi esterno" wait
  45 s. Closing the camera while the call is still connecting cancels it after the usual delay
  (it stayed up, unwatched, until the 5-minute cap).
- An incoming call without SDP in the INVITE (late offer) takes the panel's answer from the ACK;
  before, the call was up with no audio or video.
- `/av` keeps one continuous RTP stream towards ffmpeg when the panel restarts its stream (new
  SSRC or sequence numbers): ffmpeg dropped every packet as "too late" and the video froze.
- One reconnection at a time: reader, keepalive and the WebSocket `reconnect` action each opened a
  TLS connection and closed the other's. Unloading the integration stops a pending reconnection.
- The cloud unreachable at startup (HA up before the router after a power cut) makes HA retry the
  setup by itself (`ConfigEntryNotReady`) instead of leaving the entry failed until a manual reload.
- Card: if the ring ends while the browser asks for the microphone after "Rispondi", the card
  says the ring is over instead of calling the panel. When the panel refuses a call the
  card closes the video pane and says so ("La targa non ha accettato, riprova.") instead of
  staying on "Collegamento…".
- Hanging up while the panel ends the call at the same moment (crossed BYEs) returns at once
  instead of waiting 5 s for a response that no longer comes.
- A malformed `squillo.json` (entries that are not objects) no longer breaks the ring list or the
  next ring's entry; the log is rewritten through a fresh temporary file (a symlink planted in the
  folder is not followed).
- Local UDP mode: the SDP of a 183/200 to our INVITE is held to the same LAN-only rule as a ring
  (a 200 pointing the media outside the LAN is ACKed and closed with BYE), and a late ACK no longer
  restarts the media of a call that has ended in the meantime.

### Removed

- The MJPEG path: the camera's stream override and the `/api/vimar_intercom/video` view. Both
  looped on `hub.video_frame` (always `None`) and never wrote a frame; the view also needed no
  login and placed a call to the panel. Also the unused `FFMPEG_VIDEO_PORT`, `do_door` (no
  callers), the UDP re-register every 3400 s (the 120 s keepalive already re-registers) and the
  constants `SEGRETERIA_TARGET`/`DND_TARGET`.

### Changed

- The camera no longer calls the panel by itself on a reconnection: go2rtc and HA's stream
  worker reopen `/av` as soon as a stream ends, and each reopen placed a new call (486 from the
  still-busy panel, then another). Now, for 60 s after any call ends (answered, cancelled or
  refused), after a failed auto-call or after a watched ring preview, a reopen of `/av` within
  5 s of the last viewer leaving places no call and gets 503 at once. Opening the camera later
  (dashboard, HomeKit) calls as before; `/av` still waits for the video when a call is up or
  being placed.
- H.264: the answer to a ring mirrors the panel's offer (payload type, `packetization-mode`,
  `profile-level-id`), and our own offer proposes both modes (96 mode 1, 97 mode 0). The 2F
  panel (baresip, Tab 7S 40507) offers and accepts only mode 0: answering mode 1 left it with no
  common format and no video at all. Video is still re-stamped to PT 96 towards ffmpeg.
- `packages/vimar_intercom.yaml`: the ring notification has "Rispondi" (answers and opens the
  camera) and "Apri" (device authentication on iOS only, and only within 3 minutes of the ring).
  The visitor's photo comes from the `snapshot_dir` option instead of an automation.

### Security

- Local UDP mode drops SIP packets that don't come from the intercom. Before, any host on the LAN
  could send an INVITE; with the ring preview that would also have started media towards it,
  saved its picture as the visitor's photo and played it the away message.
- `/audio_ws` debug actions (`command`, `probe`, `scan`, `register`, `reconnect`) are admin-only,
  and the received actions are logged at DEBUG instead of INFO.
- `send_command` (arbitrary SIP MESSAGE) and `fetch_local` (Digest with the SIP password towards a
  LAN host) are admin-only; automations still work. They bypassed the `/audio_ws` restriction.
- Every SIP id used for calls, the door, buttons and actuators must be numeric (`sip_uri`):
  `x@other.domain` no longer changes the request URI. `open_door` accepts only `OPEN` and `OPEN_*`
  commands (the `MSG` of the door actuator, or `OPEN_2F`).
- In local UDP mode, media from a ring goes only to LAN addresses: a spoofed INVITE could point
  the preview, the away message and the card's microphone at an outside address.

### Requirements

- Home Assistant **2024.7** or later (`hacs.json`): the card's static path needs
  `async_register_static_paths`.

## [1.0.8] - 2026-09-26

Two field reports from a **Tab 5S Up 40515** (2-wire, Wi-Fi, cloud TLS) by @Apeiv, in
[#3](https://github.com/lollox80/ha-vimar-intercom/issues/3) and
[#8](https://github.com/lollox80/ha-vimar-intercom/issues/8). The camera fix is his, as he
proposed and tested it.

### Fixed

- **The camera was always black** ([#8](https://github.com/lollox80/ha-vimar-intercom/issues/8)).
  Three causes, all fixed:
  - the camera forced MJPEG and did not declare `CameraEntityFeature.STREAM`, so Home Assistant
    never used `stream_source()`, while the MJPEG path read `hub.video_frame`, which is always
    `None`. The camera now declares `STREAM` and goes through HA's stream worker (HLS/WebRTC);
  - the ffmpeg behind `/api/vimar_intercom/av` could never start: ffmpeg opens RTP **and** RTCP
    (RTP + 1) for each `m=` line, and with ports 19201/19202 the video RTCP landed on the audio
    port (`bind failed: Address already in use`). The ports are now 19210/19212;
  - the reader of ffmpeg's stderr stopped as soon as the process had exited, which is exactly
    when stderr says why. It now reads to EOF, and a startup failure logs ffmpeg's last lines.
- **Calls went to the SGA** ([#3](https://github.com/lollox80/ha-vimar-intercom/issues/3)).
  *Call* and *Call Video (outdoor)* invited the SGA, which takes the state commands but does not
  necessarily accept a call (`488` on the development plant; `488`/`408` on the 40515, where the
  SGA is `61000`). They now call the video entrance panel, like the camera.
- **The door did not open on a 2FV2** (reported by @Apeiv on
  [#20](https://github.com/lollox80/ha-vimar-intercom/pull/20)). `OPEN_2F` went to the SGA; on his
  Tab 5S Up 40515 the SGA is `61000`, which answers `200` and does nothing. The command has to go
  to the entrance panel that owns the relay (`55001` there), which is where the VIEW app sends it:
  the `GID_PE` of the door actuator in the phonebook. The lock, the *Open Door* button, the
  `open_door` action without `target` and the actuators with target `AUTO` now use that panel. On
  plants where the SGA and the door panel are the same address (the development plant: `55001`)
  nothing changes. `VOICEMAIL;`, `DND;` and `GET_INIT_STATUS` still go to the SGA/PICG.
- The `open_door` action filled `target` with the SGA when it was omitted, and ignored `command`
  when `target` was missing. Both fixed.

### Added

- **`camera_target` option**: the video entrance panel called by the camera, *Call* and *Call Video
  (outdoor)*. Default `55100`. The phonebook import fills it in with the app's own rule:
  `PHONEBOOK.AUTO` of your apartment row, otherwise the first `PE`/`PE_EXT` row.
- **`internal_panel_target` option**: the target of *Call Home (indoor)*. Default `55002`. The
  phonebook does not say which one it is, so the import leaves it alone.
- **`door_target` option**: the entrance panel that receives the door command. Empty by default.
  The phonebook import (from file or downloaded from the intercom) fills it in with the `GID_PE`
  of the first door actuator and shows it in the summary. When it is empty: the door actuator
  already saved in the entry (entries that imported the phonebook with 1.0.7 have the actuators
  but not the option), otherwise the SGA, as before.

### Changed

- Thumbnails and `camera.snapshot` come from the stream **only during a call**. Outside a call
  there is no picture on purpose: producing one would mean calling the panel at every thumbnail
  refresh. A snapshot on the ring alone still writes nothing.
- **Opening the live view places the call.** A dashboard card with `camera_view: live` now calls
  the panel every time the dashboard is shown: use `camera_view: auto` (the example card in
  `docs/lovelace_example.yaml` does).
- `media_enc` (SRTP) description: the 40515 only accepts the call with SRTP **on**, the opposite of
  the development plant. It is per plant.


## [1.0.7] - 2026-09-21

Stability release from a full debug pass against the decompiled VIEW app, the SIP logs of
the reference plant and the MITM capture. Every fix below has a regression test.
It also ships the local phonebook download from #16.

### Added

- **The phonebook can now be downloaded from the intercom itself.** Options → *Download the
  phonebook from the intercom* fetches `rubrica.db` over the intercom's own local HTTP API and
  imports the actuators, with no file to extract by hand: no rooted phone, no WSA, no adb, no
  cloud token. Authentication is HTTP Digest with **the SIP credentials the integration already
  has** in the config entry — no Vimar account is involved, and nothing leaves the local network.
  The existing *Import actuators from rubrica.db* entry stays: it is the only route for systems
  that are reachable only through the cloud.
- **The intercom now tells us its own PICG.** The same step asks
  `get_info.php?action=nickname`, whose reply carries the `PICG` role and its extension, and
  offers it as `picg_target` in the confirmation screen. On systems where the intercom is
  reachable on the local network this answers
  [#10](https://github.com/lollox80/ha-vimar-intercom/issues/10): the address does not have to be
  guessed. Cloud-only systems still need the scan proposed in
  [#14](https://github.com/lollox80/ha-vimar-intercom/issues/14). `sga_target` still comes from the phonebook's
  `SYSTEM.MAGIC_APT_INTERCOM` — two distinct values from two distinct sources, as `runtime.py`
  always documented.
- New pure module `rest_client.py` (`requests`, already a requirement; no Home Assistant imports)
  with `get_status()`, `get_nicknames()`, `download_db()`, `probe()` and the matching parsers.
  `parse_status()` reads exactly the body of a `GET_INIT_STATUS_REPLY;`, so the SIP and HTTP
  routes share one parser. 38 new tests in `tests/test_rest_client.py`.

Three quirks of the intercom's HTTP server are handled in `rest_client.py`, and are worth knowing
before touching it: its Digest `nonce` arrives as the `repr()` of a Python `bytes` object
(`nonce="b'…'"`) and must be echoed verbatim; responses carry the illegal header
`Content-Encoding: none`, so the body is read with `decode_content=False`; and **an unknown
resource is answered with 401, never 404** — a failed download and wrong credentials are
indistinguishable, which is why `RestAuthError` says both.

### SIP transport

- **Requests over UDP were sent once and never retransmitted.** A single lost datagram
  turned into "REGISTER: no useful final response (0 responses)" and two minutes of
  "Not registered" — the pattern that showed up in the reference plant's log every one to
  three hours. `REGISTER` and `MESSAGE` now retransmit per RFC 3261 (Timer E: 0.5 s,
  doubling up to 4 s) until any response arrives, and stop on a provisional one. TLS is
  unchanged. The response queue is also created *before* sending, so an immediate answer
  is no longer discarded as stale.
- **The keyframe request could block Home Assistant's event loop for ~3 s.** When an
  unrelated response sat in the dialog's queue, the wait loop re-queued it and picked it
  up again without ever yielding (Python 3.12+). Unrelated responses are now set aside
  and put back after the loop.
- **`Content-Length` counted characters, not bytes.** Any body with an accented letter
  (a nickname, a room name) was sent with a wrong length. It is now the UTF-8 byte count.
- The `REGISTER` failure warning now lists the response codes, the target and the
  transport, so a log line says what actually happened.

### Incoming messages

- **`NEW_PHONEBOOK;<version>;<gid>` was read with the two fields swapped.** The
  "Phonebook version" sensor showed the GID, and the next `GET_INIT_STATUS_REPLY`
  "changed" it back, firing a second, spurious `phonebook_changed`. Order confirmed in the
  app's receiver.
- **`CALL_INFO` turned every `0` into `None`.** `MEDIA_TYPE 0` means audio, `REASON 0`
  means declined, `VIDEO_SRC 0` means the source can't be switched: all three were lost.

### Voicemail and Do Not Disturb switches

- **A failed command flipped the switch anyway** (timeout, 404, "Not registered"), and
  with no announcement from the intercom to correct it, the wrong state survived a
  restart. Only a successful send now moves the state, and only while the real state is
  unknown.
- While the intercom has never announced its state, the switches report
  `assumed_state`, so Home Assistant shows on/off buttons instead of presenting a guess
  as fact (#9).

### Logging

- **`logger.set_level` and `logger:` had no effect on Home Assistant's log.** The
  component's logger was pinned to `DEBUG` and forwarded only `WARNING` and above. The
  forwarding threshold now follows the level you set; with nothing set it is still
  `WARNING`. The internal buffer at `/api/vimar_intercom/debug` still gets everything.
- **Lines forwarded to Home Assistant's log were not redacted** — only the internal buffer
  was. Both now go through the same filter.
- **The redaction filter missed the forms the token actually takes:** `VALUE` before
  `PARAM` (as the 40507 sends it), JSON objects (`"token": "…"`), dict reprs, and an
  `Authorization` header inside a message logged with `%r`.
- Logging setup moved to a pure module, `log_buffer.py`, so it is tested without Home
  Assistant.

### Services

- **`send_command` with an empty `header_value` sent `Panda: None`.** The header is now
  added only when both name and value are present, and a CR/LF in either is rejected.

## [1.0.6] - 2026-09-19

The last of the audit findings: the registration state machine, and four ways an
unvalidated value reached a SIP message or the filesystem.

### Registration: three bugs that all reported success

- **Once the SIP registration was lost, it never came back.** The keepalive loop ran inside
  `if sip.registered:`, so the moment that flag went false the loop spun forever doing
  nothing. In local UDP — the default — there was no other recovery path: the intercom
  stayed disconnected until Home Assistant was restarted. The loop now calls
  `reconnect()` when it finds itself unregistered.
- **A failed `REGISTER` left `registered` at `True`.** Three exit paths returned `False`
  without touching the flag, so Home Assistant kept reporting the intercom as reachable
  while it wasn't — sometimes for an hour. Every negative exit now clears it, and says why
  in the log.
- **After a recovery, the plant state was never re-read.** Voicemail, DND and the phonebook
  version can change while Home Assistant is disconnected. Regaining the registration now
  clears `_init_status_sent`, so `GET_INIT_STATUS` is asked again instead of carrying on
  with values from before the outage.

### The bind fallback advertised a port nobody was listening on

`connect()` falls back to an ephemeral UDP port when the configured one is busy, but
`_my_port()` kept returning the configured value — and that value goes into `Via`, into the
`REGISTER` `Contact` and into the in-dialog contacts. Registration still succeeded, because
responses come back to the source port, so everything looked fine: the binary sensor was
green and "open door" worked. But the incoming `INVITE` was routed to a port where nothing
was listening, so **the doorbell never rang again, with no error anywhere**. `_my_port()`
now reads the port from the socket, and the fallback logs a warning instead of happening
silently.

### Input validation

New pure module `validate.py`, applied at the boundaries:

- **`/api/vimar_intercom/video?target=…`** went straight into the request line of an
  `INVITE`. An arbitrary value called an arbitrary address; a `CRLF` split the SIP message
  in two and injected headers. Now digits only, and `hub.sip_uri()` refuses newlines as a
  second net for the authenticated paths.
- **`fetch_local`'s `save_as`** went into `hass.config.path()` as given: a `../` wrote
  anywhere under the Home Assistant user. Now it is a filename, and the file is created in
  `/config/vimar_intercom/`.
- **`fetch_local`'s `host`** was arbitrary, and that request carries the SIP password in
  Digest — anyone able to call the service could have it sent to a server of their choosing.
  Now only literal private or loopback addresses; DNS names are refused on purpose, because
  resolving them here means trusting a resolution that can change between the check and the
  request. Redirects are no longer followed, and the scheme is restricted to http/https.
- **`_is_local_request` trusted `request.remote`.** Behind a reverse proxy or Remote UI that
  is the proxy's address, which made all of Internet "local". A declared proxy hop is now
  reason enough to refuse: the legitimate consumers of these endpoints talk to Home
  Assistant directly and never declare one.

### Cloud TLS is verified when it can be

`_create_ssl_context()` fell back to `CERT_NONE` whenever `vimar_rootca.pem` was missing —
which is every installation, since that file is not in the repo — silently. `connect()` now
tries with verification first (the bundled CA if present, the system trust store otherwise)
and only falls back to an unverified handshake if the certificate is what stopped it,
saying so in the log. Plants that worked before keep working; the ones whose certificate can
be verified now get an authenticated connection without configuring anything.

New tests in `tests/test_validate.py`, `tests/test_sip_transport.py` and
`tests/test_hub_keepalive.py`; 11 of them fail on 1.0.5. `_keepalive_loop` was split so the
per-tick logic is testable. Full suite: 164 passing.

## [1.0.5] - 2026-09-19

- **The lock and the "Apri Porta" button ignored the configured SGA.** `sga_target` has
  been configurable since 1.0.0 — from the options flow or imported from `rubrica.db` —
  and `const.py` claimed that every platform read it from `runtime`. That was not true:
  `lock.py` passed the literal `"55001"` and did not even import `runtime`, and `button.py`
  did the same for the door and call buttons.

  On the plant this was developed against, `SYSTEM.MAGIC_APT_INTERCOM` happens to be
  `55001`, so nothing looked wrong. On a plant where it differs, the switches and the
  actuators imported from the phonebook followed the configured address while **the lock
  entity — the one exposed to Apple Home — and the "Apri Porta" button kept sending
  `OPEN_2F` to 55001**. As far as we know that returns a bare `200 OK` with no effect,
  which `hub.async_door` counts as success: the lock showed *unlocked* for five seconds
  while the door stayed shut. Failing while reporting success is the worst of the options.

  Both now pass no target at all, so `hub.async_door` resolves it from
  `runtime.DOOR_ESTERNO` like every other path. The "Chiama Video (esterno)" button uses
  `runtime.SGA_TARGET`.

- The internal panel address used by "Chiama Casa (interno)" moved to
  `const.INTERNAL_PANEL_TARGET`. It is **not** configurable: there is no config entry field
  and no `rubrica.db` key to derive it from, and guessing it (SGA+1) is exactly the kind of
  assumption this project does not make. On a different plant that button will call an
  address that does not exist and the call will fail — no side effect, unlike the door.

`tests/test_no_hardcoded_plant_values.py` now fails if a plant address reappears as a
literal in `lock.py`, `button.py` or `switch.py`, and `tests/test_hub_stats.py` covers the
targetless `async_door()` path the two entities now rely on.

## [1.0.4] - 2026-09-19

Security fix. Anyone who paired with a QR code should update.

- **The SIP password was readable over HTTP by any logged-in Home Assistant user.**
  Three pieces, each harmless on its own. `qr_decoder` logged the decrypted pairing
  payload at DEBUG — and that payload contains `PWD=<your SIP password>`. The internal
  ring buffer raises the `vimar_intercom` logger to DEBUG unconditionally, so that line
  was captured whether or not you had configured `logger:`. And `/api/vimar_intercom/debug`
  serves that buffer to any authenticated user, guest accounts included.

  All three are now closed: the payload is never logged (only how many fields were found
  and their names, which is what actually helps diagnose a wrong QR), the debug endpoint
  requires an **administrator**, and every line entering the buffer passes through a new
  `log_redact` module that masks credential-shaped values — `pwd`/`password`/`ha1`/`token`
  assignments, `Authorization` and `Proxy-Authorization` headers, Digest `response=`
  fields, and the `{"PARAM":"token","VALUE":"…"}` form carried by
  `GET_INIT_STATUS_REPLY`. The masking is a safety net, not the rule: credentials must not
  be logged in the first place.

  If your Home Assistant has non-administrator users, or you have ever shared a debug
  dump, treat the SIP password as exposed and re-pair from the intercom panel to rotate it.

New tests in `tests/test_log_redact.py`, plus two in `tests/test_qr_decoder.py` that fail
if the password ever reaches a log record again.

## [1.0.3] - 2026-09-18

Three bugs in `hub.py`, all found by a code audit rather than in the field, and all of the
kind that fails without looking like a failure.

- **Video auto-start never worked.** Opening the camera stream is supposed to place a SIP
  call to the video entry panel. The URI was built from `sip.C.SIP_DOMAIN` — but `sip_client`
  imports `const as C`, and `SIP_DOMAIN` does not exist there: the active domain lives in
  `runtime`, because it differs between local UDP and cloud mode. Every auto-call therefore
  raised `AttributeError`, which the surrounding `except` turned into a single
  `Auto-call error:` line, so the stream simply showed nothing. `CAMERA_TARGET` was reached
  through the same wrong module. Both now read from where the value actually lives.
- **After a call ended, the doorbell stopped ringing.** `_auto_called` marks a call we
  placed ourselves, so that the INVITE the plant echoes back is not announced as a doorbell
  ring. It was cleared on six paths but not when the call ended — and the watchdog that
  would have cleared it requires `sip.in_call`, which is already `False` by then. From that
  point on every genuine ring matched the "we started this" test and was answered with
  `603 Decline`, until Home Assistant was restarted. It is now cleared on `call_ended`.
- **A failed `GET_INIT_STATUS` was never retried.** The flag marking the request as sent was
  set regardless of the outcome, which made the retry branch in the keepalive
  (`if not self._init_status_sent`) unreachable. One transient failure left `rubrica_ver`,
  `vm_ver`, `vm_level` and the real voicemail/DND states at `None` until a restart. The flag
  now follows the result of the send.

New regression tests in `tests/test_hub_autocall.py`, including a guard that fails if a
`SIP_DOMAIN` constant reappears in `const.py`.

Note for plants that answer `200 OK` to `GET_INIT_STATUS` but never send the reply
([#1](https://github.com/lollox80/ha-vimar-intercom/issues/1)): this release does not change
that behaviour — the send succeeds, so the retry is not triggered. That case is tracked
separately.

## [1.0.2] - 2026-09-17

- **Per-installation device identity.** `const.py` shipped `DEVICE_IMEI = "351234567890123"` (and `DEVICE_UUID = DEVICE_IMEI`), a constant every installation sent in the SIP `Mobile-IMEI` header and in the `+sip.instance` contact parameter. The Vimar cloud ties a registration — and its push routing — to the device identity, so two plants presenting the same one compete for the same registration. The identity is now generated once per installation by `runtime.new_device_identity()` (15-digit IMEI, random UUIDv4) and stored in the config entry; existing entries are migrated silently on the next start, and `runtime.configure()` falls back to a fresh ephemeral identity when none is stored, so no code path can reuse a shared value. `const.py`, `runtime.py`, `__init__.py`, `sip_client.py`. New tests in `tests/test_device_identity.py`, including a regression guard that fails if a hardcoded identity reappears in `const.py`.
- **The cloud SNI and `Route` header come from the config entry.** `const.SIP_SNI` and `const.SIP_ROUTE` were fixed to `ipvdes.vimar.cloud` even though the pairing QR carries the plant's own `cproxy` (already stored as `runtime.SIP_PROXY`). On an installation whose `cproxy` differs, the TLS handshake presented the wrong server name and the `Route` header pointed at the wrong proxy. Both now use `runtime.SIP_PROXY`, and the two constants are gone. `const.py`, `sip_client.py`.
- **Options flow keeps what you typed.** All three option steps rebuilt their form from the saved entry data, so a validation error on one field silently discarded every other edit made alongside it — the rubrica GID included. The redisplayed form now starts from the submitted values; the saved ones remain the fallback, and the change-detection used to decide whether to re-run the live SIP test still compares against what is actually stored. `config_flow.py`.
- Door statistics no longer record a hardcoded `"55001"` as the target when none is given: `SGA_TARGET` has been configurable since 1.0.0, and `hub.py` now reports the configured one. New guards in `tests/test_no_hardcoded_plant_values.py` fail if any of these plant-specific values reappear as constants.
- **`do_call()` no longer strands the `calling` flag.** The INVITE transaction cleaned up on each `return` path but had no `finally`: if `parse_sdp()` or `media.setup_media()` raised after the 200 OK, `pending_responses` kept the queue and `calling` stayed `True` forever — and the guard at the top of `do_call()` then refused every later call until Home Assistant was restarted. The response loop is now wrapped in `try`/`finally` that always pops the queue and clears `calling`. `sip_client.py`.

## [1.0.1] - 2026-09-17

- **Fixed cloud SIP registration on Tab 5S UP** ([#1](https://github.com/lollox80/ha-vimar-intercom/issues/1), reported by @gtarraran992): the pairing QR code carries two distinct SIP domains — `domain` (the intercom's local domain) and `cdomain` (the cloud one) — but the decoder only kept the first, and on some Tab 5S UP units that field is `127.0.0.1`. The result was `sip:<user>@127.0.0.1` URIs and cloud registration failing every time. `qr_decoder.extract_sip_credentials()` now keeps both domains (`local_domain`/`cloud_domain`) and picks the local one as the pairing-time default only when it is actually routable — loopback, `0.0.0.0` and `localhost` fall back to `cdomain` — while `runtime.configure()` selects the active domain based on `use_local_udp`, recomputing HA1 when the active mode's domain differs from the saved one. Config entries created by earlier versions have neither of the new keys and keep using `sip_domain` as before. `qr_decoder.py`, `runtime.py`. New tests in `tests/test_qr_domain_selection.py`.
- Added `CONTRIBUTING.md` and issue templates (bug report, hardware compatibility report).

## [1.0.0] - 2026-09-07

**Prima release pubblica** su `github.com/lollox80/ha-vimar-intercom` (HACS custom repository).
Le versioni precedenti (2.0.0–3.1.4, più sotto) sono state sviluppo privato su un singolo impianto,
mai distribuite pubblicamente: restano nel changelog come storico/riferimento "beta" pre-1.0.

- **`sga_target`/`picg_target` configurabili** (gap principale pre-pubblicazione): erano hardcoded a `"55001"` in giro per il codice, un valore verificato solo su questo impianto — impianti diversi possono avere un SGA/PICG diverso. Ora sono in `runtime.SGA_TARGET`/`runtime.PICG_TARGET`, popolati da `runtime.configure()` da `options["sga_target"]`/`["picg_target"]` con fallback al default storico in `const.py`. Due nuovi campi testo nello step "Impostazioni" dell'options flow (validati: solo cifre); l'importer rubrica.db, alla conferma, li imposta entrambi automaticamente dal valore rilevato (`SYSTEM.MAGIC_APT_INTERCOM`) invece di limitarsi a segnalarlo. Aggiornati anche gli usi hardcoded residui: `switch.py` (target segreteria/DND), `button.py` (sentinella target "AUTO"), `hub.py` (target di `GET_INIT_STATUS`), e i due servizi HA `send_command`/`open_door` in `__init__.py` (default dinamico invece di `"55001"` fisso). `CAMERA_TARGET` (targa video, `"55100"`) resta fuori scope. `runtime.py`, `hub.py`, `switch.py`, `button.py`, `__init__.py`, `config_flow.py`, `strings.json`, `translations/{it,en}.json`. Nuovi test in `tests/test_runtime.py`; `runtime` aggiunto a `PURE` in `tests/test_smoke.py`.
- **Importer `rubrica.db` nell'options flow**: nuovo menu nelle opzioni dell'integrazione con due voci, "Impostazioni di rete e attuatori" (il form esistente, ora sullo step `settings`) e "Importa attuatori da rubrica.db". Quest'ultimo fa caricare il file `rubrica.db` (selettore file nativo HA, `homeassistant.components.file_upload`), lo legge in sola lettura, estrae attuatori + parametri `SYSTEM` (stessa logica read-only di `tools/parse_rubrica.py`, isolata nel nuovo modulo puro `rubrica_import.py`) e mostra un riepilogo di conferma (numero/nome attuatori trovati, SGA rilevato, con avviso se diverso da quello attualmente in uso) prima di sostituire la lista attuatori salvata — senza più copiare a mano l'output JSON del tool. Nuova dipendenza manifest: `file_upload`. `config_flow.py`, `rubrica_import.py` (nuovo), `strings.json`, `translations/{it,en}.json`, `manifest.json`. Test dedicati in `tests/test_rubrica_import.py` (schema completo, filtro per GID, fallback senza `ACTUATOR_RULES`/`ICON_LIST`, file mancante/non-SQLite, sola lettura); aggiunto a `PURE` in `tests/test_smoke.py`.
- Fix logging: il buffer di debug interno (`_debug_log`) alzava il logger `custom_components.vimar_intercom` a DEBUG, e per propagazione ai logger Python questo scavalcava il livello WARNING impostato in `logger:` di HA. Ora il logger ha `propagate = False` e un handler dedicato inoltra al log HA solo WARNING+. `__init__.py`, `README.md`.
- Fix log "Stale response 407" sul keepalive SIP: `_send_options_ping` non registrava il proprio Call-ID tra le risposte attese, quindi la risposta del proxy al keepalive OPTIONS finiva loggata come WARNING anche se normale. `_dispatch_message` ora declassa a DEBUG le risposte con Call-ID `ping-*`. Con questa fix + quella sopra non serve più alcun filtro `logger:` in `configuration.yaml`. `sip_client.py`, `README.md`.

---

## Storico pre-1.0.0 — sviluppo privato, versioni "beta" mai pubblicate

> Numerazione interna usata durante lo sviluppo su un solo impianto privato, prima della release
> pubblica. Tenuta per riferimento/tracciabilità, non corrisponde a versioni mai distribuite.

## [3.1.4] - 2026-08-20

- Fix pipeline camera: forward RTP video->ffmpeg, conflitto porte AV risolto (lock + terminazione pulita, no piu 'Address in use'), SDP scritto in executor (no blocking I/O), keyframe INFO al peer reale (non 55001) con gestione 407. Probe: comando call ora riceve e conta i pacchetti RTP (audio/video) con NAT punch

## [3.1.3] - 2026-08-20

- Media in RTP chiaro di default (media_enc off): l'impianto non accetta SRTP; build_sdp offre RTP/AVP senza crypto, media_handler passa RTP non cifrato. Opzione media_enc (SRTP) in options flow. Comando sip_probe call per test chiamata/autoaccensione da terminale

## [3.1.2] - 2026-08-20

- Camera on-demand (autoaccensione): chiama la targa video 55100 (const.CAMERA_TARGET dalla rubrica) invece del PICG 55001 che dava 488 Not Acceptable Here

## [3.1.1] - 2026-08-20

- Fix riaggancia per chiamate in arrivo: il BYE ora punta al chiamante (era R.INTERCOM); card Lovelace citofono base in docs/lovelace_example.yaml (video + rispondi/apri/riaggancia)

## [3.1.0] - 2026-08-20

### Added
- **Stato iniziale via `GET_INIT_STATUS`**: dopo il REGISTER (avvio e reconnect) l'hub invia `GET_INIT_STATUS` (Panda: blue) al PICG (`const.PICG_TARGET = SGA_TARGET = 55001`). La risposta `GET_INIT_STATUS_REPLY;[{PARAM,VALUE}]` è parsata in modo generico e robusto (fallback a regex se il body arriva troncato) e popola `voicemail`, `dnd`, `vm_level`, `rubrica_ver`, `vm_ver` (e `init_status` grezzo per token/altri param). `hub.py`, `const.py`.
- Sensori: `sensor` **Spazio Segreteria** (`vm_level`), **Versione Rubrica** (`rubrica_ver`, attr `vm_ver`), **Ultima Chiamata Persa** (`missed_call_count`, `sip_id`, `ts`). `sensor.py`.
- `binary_sensor` **Nuovo Videomessaggio** (ON su `VM;VIDEO_MESSAGE_CHANGE;NEW`). `binary_sensor.py`.
- Entità `event` **Fuoriporta** (event_type `fuoriporta`) su `FP;{...}`. `event.py`.
- Eventi bus HA in ingresso: `vimar_intercom_missed_call` `{sip_id, ts, name}`, `vimar_intercom_videomessage` `{change, extra, full}`, `vimar_intercom_fuoriporta` `{sip_id, msg}`, `vimar_intercom_call_info` `{sip_id, reason, media_type, video_src}`, `vimar_intercom_phonebook_changed` `{gid, rubrica_ver}` (emesso al cambio di `rubrica_ver` e su `NEW_PHONEBOOK;<gid>;<ver>`). Costanti `EVENT_*` in `const.py`, fire in `__init__.py` via callback dell'hub. Parsing solo in lettura (difensivo JSON/`;`, nessun comando in uscita).
- Test parser `GET_INIT_STATUS_REPLY` (completo, reale, troncato) ed eventi `MISSED_CALL`/`VM;VIDEO_MESSAGE_CHANGE`/`FP`/`CALL_INFO`/`NEW_PHONEBOOK`/`phonebook_changed`. `tests/test_hub_stats.py`.
- Stato iniziale via GET_INIT_STATUS->55001 (segreteria/DND all'avvio, sensori vm_level e rubrica_ver); eventi in arrivo: chiamata persa, videomessaggio, fuoriporta, call_info, phonebook_changed; fix troncamento body MESSAGE (200->4096) per non perdere dnd/voicemail

## [3.0.1] - 2026-08-19

- Fix form opzioni: rimosse graffe ICU nella descrizione attuatori (INVALID_ARGUMENT_TYPE); il test SIP live nelle opzioni ora si esegue solo se cambiano i parametri SIP (non blocca il salvataggio dei soli attuatori)

## [3.0.0] - 2026-08-19

 — in preparazione 3.0.0 (2026-08-19)
### Added
- Attuatori dinamici come bottoni: letti da `options["actuators"]` (lista JSON `{name, msg, target, icon}` prodotta da `tools/parse_rubrica.py`), inviati con `Panda: command`. `button.py` (`VimarActuatorButton`), `runtime.py` (`R.ACTUATORS`), `config_flow.py` (campo/validazione options), `strings.json`+traduzioni.
- `tools/parse_rubrica.py`: legge `rubrica.db` (SQLite, read-only) e ne estrae attuatori JSON per HA + SGA (`SYSTEM.MAGIC_APT_INTERCOM`) + parametri `SYSTEM`.
- `tools/sip_probe.py`: opzione `--cloud` (TLS via SRV `_sips._tcp`, SNI `ipvdes.vimar.cloud`, porta 5070), `VIMAR_SIP_HA1` (usa l'`ha1` senza password in chiaro), `--imei`/`--myname` per test di impersonazione.
- `docs/RUBRICA.md`: schema completo di `rubrica.db` (23 tabelle), metodo di estrazione via root (Samsung A32) e flusso `rubrica.db` → opzioni HA.
### Fixed
- **SGA reale = 55001** (`const.py`): `VOICEMAIL;ON/OFF` e `DND;ON/OFF` (Panda: blue) verso `SYSTEM.MAGIC_APT_INTERCOM = 55001` **accendono/spengono davvero** segreteria e non disturbare sul Tab (verificato sul campo). I precedenti target 55002/61000/60002/101 davano 200 senza effetto.
### Changed
- `README.md` e `ARCHITECTURE.md` riscritti in modo veritiero: stack SIP custom che emula VIEW (header `Panda`, UDP locale/TLS cloud), camera on-demand, **nessun RTSP**. Rimossi i riferimenti errati all'evento bus `vimar_intercom_campanello` (il campanello è un'entità `event`). Aggiornati `docs/STATE.md`, `docs/PROTOCOL.md`, `docs/ROADMAP.md`.
- SGA 55001 (segreteria/DND funzionanti), attuatori dinamici da rubrica, parse_rubrica, sip_probe --cloud/--ha1, docs veritieri

## [2.9.4] - 2026-08-19
### Fixed
- SRTP migrato ad AES-CTR di pycryptodome: rimossa la dipendenza non dichiarata da cryptography (srtp.py la importava senza che fosse nei requirements del manifest). Aggiunti test con i vettori ufficiali RFC 3711 B.3 che verificano keystream e KDF byte per byte.

## [2.9.3] - 2026-08-18
### Changed
- `const.py`: `ACTUATORS = []` — rimossi i token ipotizzati OPEN_F1/OPEN_2 (aprivano la porta). ADR-4.
- Commenti/documentazione dei comandi Segreteria/DND (Panda: blue, SGA).

## [2.9.2] - 2026-08-18
### Fixed
- Modalità cloud: risoluzione SRV `_sips._tcp.ipvdes.vimar.cloud` → `flexiprod{1,2,3}.ipvdes2.vimarsso.cloud:7042`, TLS con SNI. ADR-5.

## [2.9.0] - 2026-08-17
### Added
- Servizio `vimar_intercom.fetch_local` (GET HTTP Digest verso il proxy locale) per tentare il download di rubrica/mailbox.

## [2.8.0] - 2026-08-17
### Security
- Hardening endpoint HTTP: `/audio_ws`, `/push_token`, `/debug` autenticati; `/video`, `/av` solo da LAN (`_is_local_request`). ADR-3.

## [2.2.0 – 2.7.0] - 2026-08-16/17
### Added
- Switch Segreteria e Non disturbare (stato letto dagli annunci `VOICEMAIL;`/`DND;` da 55002).
- Rilevamento modello da header SIP (`model_detect.py`), options flow UDP locale / cloud, statistiche estese.

## [2.1.0] - 2026-08-16
### Added
- Sensori estesi (stato, ultimo chiamante, contatori, durata, ultima apertura, ultimo comando, ultimo messaggio).
- Servizi `send_command`, `call`, `answer`, `hangup`, `open_door`.

## [2.0.0] - 2026-08-15
- Fork di noiseheroes-lab/ha-custom-components `vimar_intercom`; adattamento a Tab 7S 2F+ WiFi (40507), UDP locale.
