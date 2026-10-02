const TOKEN = 'PASTE_YOUR_TOKEN_HERE';          // HA long-lived access token (Profile > Security)
const HA = 'http://homeassistant.local:8123';   // Home Assistant on the LAN, plain http
const AV_KEY = '';                              // optional for now: the /av key (integration options)

// Vimar intercom on Alexa (Scrypted, Scripts plugin). Setup: docs/SCRYPTED-ALEXA.md
// Video: Home Assistant's continuous passive stream (standby picture at rest, the panel live
// during a ring or a call). It never calls the panel by itself.
// Talk: Alexa calls startIntercom as soon as an Echo opens the camera, also by itself on the
// doorbell announcement. So:
//  - ringing: only watch (the Vimar app keeps ringing on the phones); answer, like the card's
//    "Answer", only when someone speaks to the Echo;
//  - not ringing: call the panel (live view);
//  - already in a call (card, app): join it.
// On close (stopIntercom) only a call the Echo placed or answered is hung up.
// It never opens the door: the only actions it sends are call, answer and hangup.
const URL = HA + '/api/vimar_intercom/av?autocall=0&idle=image' + (AV_KEY ? '&auth=' + AV_KEY : '');
const FRAME = 320;        // 20 ms of PCM16LE mono at 8 kHz, the audio_ws format
const VOICE_RMS = 800;    // raise it if household noise answers a ring
const VOICE_FRAMES = 10;  // 200 ms of voice in a row while ringing = "Answer"
const { spawn } = require('child_process');
// Node 22 (current Scrypted) has a built-in WebSocket that accepts headers; otherwise 'ws'.
const WS = (globalThis as any).WebSocket ?? require('ws');

const mso = () => ({ id: 'channel0', name: 'Stream 1', container: 'mpegts', video: { codec: 'h264', width: 640, height: 480 } });

function rms(pcm: Buffer) {
  let s = 0;
  for (let i = 0; i < pcm.length; i += 2) s += pcm.readInt16LE(i) ** 2;
  return Math.sqrt(s / (pcm.length / 2));
}

export default class VimarIntercomAlexa extends ScryptedDeviceBase implements VideoCamera, Intercom {
  ffmpeg?: any;
  ws?: any;
  step = '';    // '' waiting for the state, 'ring', 'answer', 'call', 'join'
  ours = false; // the Echo placed (or answered) the call: hang up on close

  constructor(nativeId: ScryptedNativeId) {
    super(nativeId);
    setTimeout(() => systemManager.getDeviceById(this.id).setType(ScryptedDeviceType.Doorbell), 1000);
  }

  async getVideoStreamOptions() { return [mso()]; }

  async getVideoStream(options?: RequestMediaStreamOptions) {
    // No -an: Rebroadcast turns the street audio (AAC) into Opus, see the docs.
    return mediaManager.createFFmpegMediaObject({
      url: undefined, mediaStreamOptions: mso(),
      inputArguments: ['-fflags', '+genpts', '-analyzeduration', '3000000', '-probesize', '5000000', '-fpsprobesize', '0', '-f', 'mpegts', '-i', URL],
    });
  }

  async startIntercom(media: MediaObject) {
    this.ffmpeg?.kill(); // a second Echo: the mic changes, the call stays
    if (!TOKEN || TOKEN === 'PASTE_YOUR_TOKEN_HERE') throw new Error('Vimar intercom: set TOKEN at the top of the script');
    const ws = this.ws ??= this.connect();
    const input = await mediaManager.convertMediaObjectToJSON<FFmpegInput>(media, ScryptedMimeTypes.FFmpegInput);
    const ff = this.ffmpeg = spawn(await mediaManager.getFFmpegPath(),
      [...(input.inputArguments ?? []), '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', 'pipe:1'],
      { stdio: ['ignore', 'pipe', 'ignore'] });
    let pending = Buffer.alloc(0);
    let loud = 0;
    ff.stdout.on('data', (chunk: Buffer) => {
      pending = Buffer.concat([pending, chunk]);
      while (pending.length >= FRAME) {
        const pcm = pending.subarray(0, FRAME);
        pending = pending.subarray(FRAME);
        if (ws.readyState !== 1) continue;
        if (this.step === 'ring') {
          // Ringing and the Echo is just open: the mic does not reach the panel, but if
          // someone speaks we answer (the doorbell announcement alone never answers).
          loud = rms(pcm) >= VOICE_RMS ? loud + 1 : 0;
          if (loud >= VOICE_FRAMES) {
            // ours right away: if the Echo closes before the answer completes, HA handles the
            // answer before the hangup (same WS, in order) and the answered call is closed.
            this.step = 'answer'; this.ours = true;
            ws.send(JSON.stringify({ action: 'answer' }));
          }
          continue;
        }
        // HA forwards it to the panel only during a call, and drops it before
        ws.send(Buffer.concat([Buffer.from([0x02]), pcm]));
      }
    });
  }

  connect() {
    const ws = new WS(HA.replace(/^http/, 'ws') + '/api/vimar_intercom/audio_ws', { headers: { Authorization: `Bearer ${TOKEN}` } });
    ws.binaryType = 'arraybuffer';
    this.step = '';
    this.ours = false;
    ws.onmessage = (e: any) => {
      if (typeof e.data !== 'string') return; // 0x01 audio, 0x03 video: the Echo gets them from the stream
      const m = JSON.parse(e.data);
      if (!this.step && m.type === 'state') {
        if (m.in_call) this.step = 'join';
        // no ringing field (older integration): treat it as ringing, never call over a ring
        else if (m.ringing !== false) this.step = 'ring';
        else { this.step = 'call'; this.ours = true; ws.send(JSON.stringify({ action: 'call' })); }
      } else if (m.type === 'error' && (this.step === 'call' || this.step === 'answer')) {
        // call not started, or the ring was taken by someone else: just watch
        this.step = 'join';
        this.ours = false;
      }
    };
    ws.onerror = () => console.error('audio_ws: error (valid token? HA reachable? IP banned?)');
    ws.onclose = () => { if (this.ws === ws) this.ws = undefined; };
    return ws;
  }

  async stopIntercom() {
    // Alexa also calls it when opening, before startIntercom: no session, nothing to do.
    this.ffmpeg?.kill();
    this.ffmpeg = undefined;
    const ws = this.ws;
    this.ws = undefined;
    if (!ws) return;
    if (this.ours && ws.readyState === 1) ws.send(JSON.stringify({ action: 'hangup' }));
    this.ours = false;
    ws.close();
  }
}
