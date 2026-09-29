// Vimar Intercom — card citofono: video (anche durante lo squillo), parla/ascolta, apri,
// cronologia degli squilli. Caricata dall'integrazione, non serve aggiungerla alle risorse.
//
//   type: custom:vimar-intercom-card
//   name: Citofono                                 (opzionali: questi sono i default)
//   camera: camera.vimar_intercom_intercom
//   status: sensor.vimar_intercom_intercom_stato
//   lock: lock.vimar_intercom_serratura
//   last_ring: sensor.vimar_intercom_intercom_ultimo_squillo
//
// Le entità non vanno scritte: un entity_id che non esiste (area del dispositivo,
// rinomina) viene sostituito da quello vero. La camera dell'integrazione si trova nel
// registro del frontend; stato, ultimo squillo e serratura dall'attributo
// `card_entities` della camera. Scritte e esistenti, vincono quelle della config.
//   anchor: citofono      (URL con #citofono: la card si porta in vista; se lo stato è già
//                         "in_call" — es. "Rispondi" premuto sulla notifica, che risponde
//                         dall'automazione — anche l'audio riparte da sola; "" = no)
//   history: 8            (ultimi squilli con foto e clip, se c'è la cartella foto; 0 = no)
//   confirm_open: true    (Apri chiede un secondo tocco; false = apre al primo)
//   listen_on_ring: false (si sente il visitatore già allo squillo, senza rispondere:
//                         solo ricezione, il microfono resta spento; anche dall'editor)
//   layout: overlay       (o "sotto"; anche dall'editor visuale)
//
//   layout: overlay   (default) "Video a tutta card": da fermo riga da 72 px (foto dell'ultimo
//                     squillo = tasto cronologia | nome · stato · ultimo / tre pill). In diretta
//                     la card È il video 4:3: stato in alto a sinistra, cronologia in alto a
//                     destra, tasti da 44 px etichettati su uno scrim sfumato in basso. Da fermo
//                     con cronologia: palco 4:3 con la foto grande e il cassetto, sopra la riga.
//   layout: sotto     "Tasti sotto il video": stessa riga; in diretta il video 4:3 si infila
//                     SOPRA la riga e i tasti restano nella riga, niente sopra al video. Da fermo
//                     con cronologia: lista a piena larghezza sotto la riga (3 righe, poi scorre).
//
// Stesso DOM per i due layout: cambia solo il CSS, agganciato all'attributo `layout` sull'host
// (blocchi :host([layout="overlay"]) / :host([layout="sotto"]) in fondo a STYLE).
//
// Regole comuni: aprire la pagina non chiama la targa; posti fissi [vedi|annulla|riaggancia]
// [parla|rispondi|microfono] [apri]; "Apri" in due tocchi; slot 1 spento (non nascosto)
// durante lo squillo; il cassetto si chiude da solo allo squillo; dopo il riaggancio il
// video resta 1,5 s con i tasti spenti; un avviso sostituisce la riga di stato per 4 s.
//
// Audio e video sul WebSocket /api/vimar_intercom/audio_ws (lo stesso dell'app iOS):
//   targa → browser: 0x01 + PCM16LE 8 kHz mono, 0x03 + NAL H.264 (Annex B);
//   browser → targa: 0x02 + PCM16LE.
// Il video dal vivo passa da lì (WebCodecs → canvas: primo fotogramma in ~0,1 s) e, dove
// WebCodecs manca, dallo stream di HA (go2rtc/HLS, 2-4 s). Microfono e WebCodecs
// funzionano solo in HTTPS (o su localhost).

const RATE = 8000;
const NO_ANSWER = "La targa non risponde, riprova.";
const REFUSED = "La targa non ha accettato, riprova.";
const SETUP_TIMEOUT_S = 20;  // oltre, "la targa non risponde" (il cloud a volte ci mette 15 s)
const HOLD_MS = 1500;        // dopo il riaggancio: video fermo, tasti spenti, poi la card si richiude
const SAY_MS = 4000;         // un avviso resta 4 s al posto della riga di stato
const LIVE = ["ringing", "calling", "in_call"];
const OUTCOME = { answered: "Risposto", away: "Messaggio di assenza", missed: "Nessuna risposta" };
const LABEL = {
  idle: "Pronto", ringing: "Suonano alla porta", calling: "Collegamento…",
  in_call: "In chiamata", offline: "Non raggiungibile",
};
const hm = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const dayLabel = (t) => {  // "" oggi, "Ieri", altrimenti "26 set"
  const d = new Date(t), now = new Date(), y = new Date(now);
  y.setDate(now.getDate() - 1);
  return d.toDateString() === now.toDateString() ? "" : d.toDateString() === y.toDateString() ? "Ieri"
    : d.toLocaleDateString([], { day: "numeric", month: "short" });
};
const when = (t) => [dayLabel(t), hm(t)].filter(Boolean).join(" ");  // "18:42", "Ieri 21:30"
const whenFull = (t) => new Date(t).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

const STYLE = `
  :host { display: block; scroll-margin-top: calc(var(--header-height, 56px) + 8px); }
  ha-card { position: relative; overflow: hidden; --st: var(--primary-color); --ink: var(--primary-text-color);
            --dim: var(--secondary-text-color); --fill: color-mix(in srgb, var(--ink) 7%, transparent); }
  [data-state="ringing"] { --st: var(--warning-color); }
  [data-state="calling"] { --st: var(--info-color); }
  [data-state="in_call"] { --st: var(--success-color); }
  [data-state="offline"] { --st: var(--disabled-text-color, var(--dim)); }
  button { all: unset; box-sizing: border-box; position: relative; cursor: pointer; -webkit-tap-highlight-color: transparent; }
  button:disabled { cursor: default; }
  button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  [hidden] { display: none !important; }
  @keyframes blink { 50% { opacity: .25; } }
  @keyframes pulse { 50% { transform: scale(1.5); opacity: .5; } }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes halo { to { box-shadow: 0 0 0 14px transparent; } }

  /* Nome · ● stato · ultimo squillo su una riga; un avviso prende il posto di stato+ultimo per 4 s. */
  .name { font-size: 14px; font-weight: 600; line-height: 18px; color: var(--ink); white-space: nowrap;
          overflow: hidden; text-overflow: ellipsis; }
  .sub { display: flex; align-items: center; gap: 6px; min-width: 0; font-size: 12px; line-height: 16px;
         color: var(--dim); white-space: nowrap; }
  .sub > * { overflow: hidden; text-overflow: ellipsis; }
  .pill { display: inline-flex; align-items: center; gap: 5px; flex: none; color: var(--st); font-weight: 600; }
  .pill::before { content: ""; flex: none; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  [data-state="ringing"] .pill { padding: 0 8px 0 6px; border-radius: 999px; color: var(--text-primary-color); background: var(--st); }
  [data-state="ringing"] .pill::before { animation: pulse 1s ease-in-out infinite; }
  [data-state="calling"] .pill::before, [data-state="in_call"] .pill::before { animation: blink 2s infinite; }
  .last:not(:empty)::before { content: "· "; }
  .last:empty, .err:empty { display: none; }
  .err { color: var(--warning-color); font-weight: 500; }
  .sub:has(.err:not(:empty)) > :not(.err) { display: none; }

  /* Foto dell'ultimo squillo = tasto cronologia (senza foto: campanello, non cliccabile). */
  #photo { flex: none; width: 56px; height: 44px; border-radius: 8px; overflow: hidden; display: grid; place-items: center;
           color: var(--st); background: color-mix(in srgb, var(--st) 14%, transparent); transition: box-shadow .15s; }
  #photo img { display: none; width: 100%; height: 100%; object-fit: cover; }
  #photo img[src] { display: block; }
  #photo img[src] + ha-icon { display: none; }
  #photo ha-icon { --mdc-icon-size: 26px; }
  #photo:focus-visible { outline-offset: 0; }
  [data-drawer="true"] #photo { box-shadow: inset 0 0 0 2px var(--primary-color); }
  [data-state="ringing"] #photo { box-shadow: inset 0 0 0 2px var(--st); }

  /* Scena: video dal vivo o foto dell'ultimo squillo. Niente animazione di altezza né
     overflow nascosto animato: Safari/iOS non dipinge il <video> lì dentro. */
  #video > * { position: absolute; inset: 0; --ha-card-border-radius: 0; --ha-card-box-shadow: none; --ha-card-border-width: 0; }
  #video > canvas { width: 100%; height: 100%; object-fit: cover; }
  #video, .still, .ph { position: absolute; inset: 0; display: none; }
  .live #video { display: block; }
  .still { width: 100%; height: 100%; object-fit: cover; }
  ha-card:not(.live) .still[src] { display: block; }
  ha-card:not(.live) .still:not([src]) + .ph { display: grid; place-items: center; color: rgba(255,255,255,.28); }
  .ph ha-icon { --mdc-icon-size: 48px; }
  .badge { position: absolute; top: 10px; left: 10px; z-index: 2; display: none; align-items: center; gap: 6px;
           padding: 4px 10px 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; line-height: 16px;
           color: #fff; background: rgba(0,0,0,.55); }
  .live .badge { display: inline-flex; }
  .badge::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--st); }
  [data-state="ringing"] .badge::before { animation: pulse 1s ease-in-out infinite; }
  [data-state="calling"] .badge::before { animation: blink 1.2s infinite; }
  .hold .row button { pointer-events: none; opacity: .5; }

  /* Cronologia: righe 52 px (thumb 56×42, ora / esito), separatore quando cambia giorno. */
  .hist { flex: 1; overflow-y: auto; overscroll-behavior: contain; -webkit-overflow-scrolling: touch; padding: 4px 6px; }
  .day { padding: 8px 8px 2px; font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--dim); }
  .ring { display: grid; grid-template-columns: 56px 1fr; gap: 10px; align-items: center; width: 100%; height: 52px;
          padding: 0 6px; border-radius: 10px; text-align: left; transition: background .15s; }
  .ring:not(:disabled):hover, .ring:not(:disabled):active { background: var(--fill); }
  .ring:focus-visible { outline-offset: -2px; }
  .th { position: relative; display: grid; place-items: center; width: 56px; height: 42px; border-radius: 6px; overflow: hidden;
        background: var(--fill); color: var(--dim); }
  .th img { width: 100%; height: 100%; object-fit: cover; }
  .th ha-icon { --mdc-icon-size: 20px; }
  /* Squillo con clip: tasto play sulla miniatura (anche senza foto). */
  .th .play { position: absolute; inset: 0; display: grid; place-items: center; color: #fff; background: rgba(0,0,0,.3); }
  .th .play ha-icon { --mdc-icon-size: 24px; }
  .ring:disabled .th { opacity: .6; }
  .txt { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .at { font-size: 13px; font-weight: 600; line-height: 1.2; }
  .out { display: flex; align-items: center; gap: 5px; font-size: 12px; line-height: 1.2; color: var(--dim);
         white-space: nowrap; overflow: hidden; }
  .out::before { content: ""; flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--oc, var(--dim)); }
  [data-outcome="answered"] { --oc: var(--success-color); }
  [data-outcome="away"] { --oc: var(--info-color); }
  [data-outcome="missed"] { --oc: var(--warning-color); }
  .empty { display: none; flex: 1; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
           padding: 14px; text-align: center; font-size: 13px; color: var(--dim); }
  .empty ha-icon { --mdc-icon-size: 32px; opacity: .6; }
  .hist:empty + .empty { display: flex; }

  .lbl { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  button.busy ha-icon { display: none; }
  button.busy .ic::after { content: ""; width: 12px; height: 12px; border-radius: 50%; border: 2px solid currentColor;
                           border-top-color: transparent; animation: spin 1s linear infinite; }
  dialog.photo { padding: 0; border: 0; background: none; max-width: 95vw; color: #fff; text-align: center; }
  dialog.photo::backdrop { background: rgba(0,0,0,.8); }
  dialog.photo img, dialog.photo video { display: block; max-width: 95vw; max-height: 80vh; border-radius: 14px; }
  dialog.photo video { width: min(95vw, 640px); background: #000; }
  .cap { margin: 12px 0 0; font-size: 14px; font-weight: 500; }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }

  .drawer { position: absolute; top: 0; right: 0; bottom: 0; z-index: 1; width: min(64%, 320px); display: flex; flex-direction: column;
            color: var(--ink); background: color-mix(in srgb, var(--card-background-color, #fff) 94%, transparent);
            box-shadow: -6px 0 24px rgba(0,0,0,.25); transform: translateX(100%); visibility: hidden;
            transition: transform .28s cubic-bezier(.2,.8,.2,1), visibility .28s; }
  [data-drawer="true"] .drawer { transform: none; visibility: visible; }

  #log { position: absolute; top: 8px; right: 8px; z-index: 3; width: 40px; height: 40px; border-radius: 50%; display: none;
         place-items: center; color: #fff; background: rgba(0,0,0,.45); }
  #log ha-icon { --mdc-icon-size: 22px; }
  .live #log { display: grid; }
  [data-drawer="true"] #log { background: var(--primary-color); }

  .row { display: flex; gap: 6px; min-width: 0; }
  /* Se la riga è stretta (anteprima dell'editor, ~330 px) si accorcia solo la pill più lunga
     ("Vedi es…"): "Parla" e "Apri" restano leggibili per intero. */
  #view, #hangup { order: 1; flex: 0 1 auto; } #talk { order: 2; } #open { order: 3; }
  .row button { display: inline-flex; flex: none; align-items: center; gap: 4px; height: 36px; padding: 0 9px; border-radius: 18px; min-width: 0;
                font-size: 12px; font-weight: 600; color: var(--primary-color);
                background: color-mix(in srgb, var(--primary-color) 12%, transparent); transition: transform .1s, background .2s; }
  .row button::before { content: ""; position: absolute; inset: -4px 0; }  /* bersaglio 44 px */
  .ic { display: grid; place-items: center; flex: none; width: 18px; height: 18px; }
  .ic ha-icon { --mdc-icon-size: 18px; }
  .row button:active { transform: scale(.96); }
  button.fill { background: var(--primary-color); color: var(--text-primary-color); }
  button.ok { background: var(--success-color); color: var(--text-primary-color); }
  button.warn { background: var(--warning-color); color: var(--text-primary-color); }
  #hangup { background: var(--error-color); color: var(--text-primary-color); }
  .row button:disabled { opacity: .45; }
  button.answer { box-shadow: 0 0 0 0 color-mix(in srgb, var(--success-color) 55%, transparent); animation: halo 1.4s ease-out infinite; }

  .head { display: grid; grid-template-columns: 56px minmax(0, 1fr); grid-template-rows: 18px 36px; column-gap: 10px; row-gap: 4px;
          align-items: center; padding: 7px 10px 7px 12px; }
  #photo { grid-row: 1 / 3; }
  .ttl { display: flex; align-items: center; gap: 6px; min-width: 0; }
  .name { flex: 0 1 auto; }
  .ttl .sub::before { content: "·"; flex: none; }

  ha-card { display: flex; flex-direction: column; }
  .media { display: none; position: relative; aspect-ratio: 4 / 3; min-height: 0; background: #0b0e12; color: #fff; }
  .live .media, [data-drawer="true"] .media { display: block; }
  .live #photo { display: none; }  /* in diretta la cronologia sta sul video */

  /* ---- layout="overlay": in diretta la testata si stende sul video, resta solo la fila dei tasti sullo scrim. */
  :host([layout="overlay"]) ha-card:not(.live) .ph { right: min(64%, 320px); }
  :host([layout="overlay"]) [data-state="ringing"] #log { display: none; }
  :host([layout="overlay"]) .live .head { position: absolute; inset: 0; z-index: 3; display: block; padding: 0; pointer-events: none; }
  :host([layout="overlay"]) .live .ttl { display: none; }
  :host([layout="overlay"]) .live .row { position: absolute; left: 0; right: 0; bottom: 0; padding: 26px 8px 10px; justify-content: space-evenly;
               gap: 0; pointer-events: auto; background: linear-gradient(to top, rgba(0,0,0,.72), rgba(0,0,0,.4) 60%, transparent); }
  :host([layout="overlay"]) .live .row button { flex-direction: column; height: auto; padding: 0; gap: 4px; border-radius: 0;
               background: none; color: #fff; font-size: 11px; font-weight: 500; }
  /* Il rosso di #hangup sta su un selettore con id: senza questa riga vince sullo sfondo nullo e il tasto è un quadrato. */
  :host([layout="overlay"]) .live #hangup { background: none; }
  :host([layout="overlay"]) .live .row button::before { inset: -6px -12px; }
  :host([layout="overlay"]) .live .row button:active { transform: none; }
  :host([layout="overlay"]) .live .ic { width: 44px; height: 44px; border-radius: 50%; background: rgba(0,0,0,.45); color: #fff; }
  :host([layout="overlay"]) .live .ic ha-icon { --mdc-icon-size: 22px; }
  :host([layout="overlay"]) .live button.fill .ic { background: var(--primary-color); }
  :host([layout="overlay"]) .live button.ok .ic { background: var(--success-color); }
  :host([layout="overlay"]) .live button.warn .ic { background: var(--warning-color); }
  :host([layout="overlay"]) .live #hangup .ic { background: var(--error-color); }
  :host([layout="overlay"]) .live button.answer { animation: none; box-shadow: none; }
  :host([layout="overlay"]) .live button.answer .ic { width: 52px; height: 52px; margin-top: -8px;
               box-shadow: 0 0 0 0 color-mix(in srgb, var(--success-color) 55%, transparent); animation: halo 1.4s ease-out infinite; }
  :host([layout="overlay"]) .live button.busy .ic::after { width: 20px; height: 20px; border-width: 3px; }
  :host([layout="overlay"]) .live .drawer { bottom: 96px; }

  /* ---- layout="sotto": in diretta il video sta sopra la riga, i tasti restano nella riga (a piena larghezza).
     Da fermo la cronologia è una lista sotto la riga, non un palco 4:3. */
  :host([layout="sotto"]) .live .ttl, :host([layout="sotto"]) .live .row { grid-column: 1 / 3; }
  :host([layout="sotto"]) .live .head { padding-top: 8px; }
  :host([layout="sotto"]) ha-card:not(.live)[data-drawer="true"] .media { order: 1; aspect-ratio: auto; background: none; color: var(--ink); }
  :host([layout="sotto"]) ha-card:not(.live)[data-drawer="true"] :is(.still, .ph) { display: none; }
  :host([layout="sotto"]) ha-card:not(.live) .drawer { position: static; width: auto; transform: none; visibility: visible; box-shadow: none;
               background: none; transition: none; border-top: 1px solid var(--divider-color); }
  :host([layout="sotto"]) ha-card:not(.live) .hist { max-height: 176px; }
`;

const btn = (id, icon, label) =>
  `<button id="${id}"><span class="ic"><ha-icon icon="${icon}" aria-hidden="true"></ha-icon></span>` +
  `<span class="lbl">${label}</span></button>`;
const DRAWER = `<aside class="drawer" id="drawer" aria-label="Ultimi squilli"><div class="hist"></div>
  <div class="empty"><ha-icon icon="mdi:bell-off-outline" aria-hidden="true"></ha-icon><span></span></div></aside>`;
const SCENE = `<div id="video"></div><img class="still" alt="">
  <span class="ph"><ha-icon icon="mdi:doorbell-video" aria-hidden="true"></ha-icon></span>`;
const PHOTO = `<button id="photo" aria-label="Cronologia squilli" aria-expanded="false" aria-controls="drawer" disabled>
  <img alt=""><ha-icon icon="mdi:doorbell-video" aria-hidden="true"></ha-icon></button>`;
const LOG = `<button id="log" aria-label="Cronologia squilli" aria-expanded="false" aria-controls="drawer">
  <ha-icon icon="mdi:menu-open" aria-hidden="true"></ha-icon></button>`;
const SUB = `<span class="sub"><span class="pill" role="status" aria-live="polite"></span><span class="last"></span><span class="err" role="alert"></span></span>`;
const ROW = `<div class="row">${btn("view", "mdi:cctv", "Vedi esterno")}${btn("talk", "mdi:microphone", "Parla")}` +
  `${btn("hangup", "mdi:phone-hangup", "Riaggancia")}${btn("open", "mdi:door-open", "Apri")}</div>`;
// Foto dello squillo in grande, o il suo clip (<video>) se c'è.
const PHOTO_DLG = `<dialog class="photo" aria-label="Squillo"><img alt="Foto dello squillo">` +
  `<video controls playsinline preload="metadata" hidden></video><p class="cap"></p></dialog>`;

const TEMPLATE = `<ha-card>
  <div class="media">${SCENE}<span class="badge dyn" aria-hidden="true"></span>${LOG}${DRAWER}</div>
  <div class="head">${PHOTO}<div class="ttl"><span class="name"></span>${SUB}</div>${ROW}</div>
  ${PHOTO_DLG}</ha-card>`;

const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  parts.reduce((o, p) => (out.set(p, o), o + p.length), 0);
  return out;
};

// Video dal vivo a bassa latenza: i NAL H.264 che il server manda sul WebSocket (0x03 +
// Annex B, gli stessi dell'app iOS) decodificati con WebCodecs e disegnati su un
// <canvas>. Niente stream di HA (go2rtc/HLS: 2-4 s di ritardo, e la targa chiude dopo
// ~10 s). Autonomo: prende un canvas qualunque, chi lo crea lo chiude con close().
// Parte dal primo IDR (il server manda SPS→PPS→IDR, e a chi si collega a video in
// corso rimanda il GOP corrente). Chiama onFail (la card rimette lo stream di HA) solo
// se manca VideoDecoder (HTTP non sicuro, Safari vecchio, WebKit di Playwright) o il
// codec non è supportato: lo stream di HA fuori dalla LAN (5G, niente TURN) è bianco,
// quindi a un WebSocket caduto si riapre (il server rimanda il GOP corrente) e a un
// decoder rotto (dati corrotti, riferimento perso: VideoToolbox su iPhone è severo)
// se ne fa uno nuovo al prossimo IDR, ogni ~3 s dalla targa. Dal campo: seconda
// "Vedi esterno" 3 s dopo la prima, card bianca su 5G.
class NalPlayer {
  static ok = () => !!window.VideoDecoder;

  constructor(hass, canvas, onFail) {
    this.canvas = canvas;
    this.frames = 0;
    this.resets = 0;  // decoder rifatti e WebSocket riaperti (nei test)
    this._n = 0;
    this._hass = hass;
    this._fail = (why) => {
      if (this._closed) return;
      console.warn("vimar-intercom-card: video WebCodecs non disponibile:", why);
      this.close();
      onFail(why);
    };
    this._open();
  }

  async _open() {
    try {
      const { path } = await this._hass.callWS({ type: "auth/sign_path", path: "/api/vimar_intercom/audio_ws" });
      if (this._closed) return;
      const ws = (this._ws = new WebSocket(location.origin.replace(/^http/, "ws") + path));
      ws.binaryType = "arraybuffer";
      ws.onopen = () => (this._wait = 0);  // aperto: la prossima caduta riparte da 1 s
      ws.onmessage = (ev) => {
        if (typeof ev.data === "string" || new Uint8Array(ev.data, 0, 1)[0] !== 0x03) return;
        this._nal(new Uint8Array(ev.data, 1));
      };
      ws.onclose = () => this._ws === ws && this._reopen("WebSocket chiuso");
    } catch (e) {
      this._reopen(e.message || e);  // auth/sign_path fallita: connessione con HA in ripristino
    }
  }

  // WebSocket caduto (rete, HA che riparte): si riapre dopo 1, 2, 4… s (massimo 10, così
  // HA fermo non riceve un tentativo al secondo), finché il player non viene chiuso
  // (close(): la card è uscita dal vivo). I P arrivati prima del GOP che il server
  // rimanda non si decodificano (riferimenti persi).
  _reopen(why) {
    if (this._closed) return;
    this._wait = Math.min((this._wait || 0.5) * 2, 10);
    console.warn("vimar-intercom-card: video WebSocket:", why, `— riapro fra ${this._wait} s`);
    this.resets++;
    this._ws = null;
    this._skip = true;
    this._t = setTimeout(() => this._open(), this._wait * 1000);
  }

  // Un NAL in Annex B (00 00 00 01 + NAL). SPS e PPS si tengono; l'IDR coi suoi SPS+PPS
  // è il chunk chiave (configura il decoder la prima volta), i P seguono. Mai un P
  // prima del primo IDR (_dec non c'è) o dopo un buco (_skip): il decoder darebbe errore.
  _nal(nal) {
    const t = nal[4] & 0x1f;
    if (t === 7) this._sps = nal;
    else if (t === 8) this._pps = nal;
    else if (t === 5 && this._sps && this._pps) {
      try {
        if (!this._dec) this._configure();
        this._skip = false;
        this._decode("key", concat(this._sps, this._pps, nal));
      } catch (e) {
        this._broken(e);
      }
    } else if (t === 1 && this._dec && !this._skip) {
      // Il decoder non tiene il passo: via i P fino al prossimo IDR, non si accumula ritardo.
      if (this._dec.decodeQueueSize > 8) this._skip = true;
      else this._decode("delta", nal);
    }
  }

  _configure() {
    const s = this._sps;  // profile_idc, constraint_set, level_idc → "avc1.42C01E"
    const codec = "avc1." + [s[5], s[6], s[7]].map((b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
    this._dec = new VideoDecoder({ output: (f) => this._paint(f), error: (e) => this._broken(e) });
    // Senza `description` il formato è Annex B. Codec non supportato: arriva da `error`.
    this._dec.configure({ codec, optimizeForLatency: true });
  }

  _decode(type, data) {
    try {
      this._dec.decode(new EncodedVideoChunk({ type, timestamp: this._n++ * 66667, data }));
    } catch (e) {
      this._broken(e);
    }
  }

  // Decoder rotto: si butta e se ne fa uno nuovo al prossimo IDR. Solo un codec non
  // supportato manda la card allo stream di HA.
  _broken(e) {
    if (this._closed) return;
    if (e.name === "NotSupportedError") return this._fail(e.message);
    console.warn("vimar-intercom-card: decoder video:", e.message || e, "— riparto dal prossimo IDR");
    this.resets++;
    try { this._dec?.close(); } catch { /* già chiuso dall'errore */ }
    this._dec = null;
  }

  _paint(frame) {
    const c = this.canvas;
    if (c.width !== frame.displayWidth || c.height !== frame.displayHeight) {
      c.width = frame.displayWidth;
      c.height = frame.displayHeight;
    }
    (this._ctx ||= c.getContext("2d")).drawImage(frame, 0, 0);
    frame.close();
    this.frames++;
  }

  close() {
    this._closed = true;
    clearTimeout(this._t);
    const ws = this._ws;
    this._ws = null;
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    try { this._dec?.close(); } catch { /* già chiuso dopo un errore */ }
    this._dec = null;
  }
}

const DEFAULTS = {
  name: "Citofono",
  camera: "camera.vimar_intercom_intercom",
  status: "sensor.vimar_intercom_intercom_stato",
  lock: "lock.vimar_intercom_serratura",
  last_ring: "sensor.vimar_intercom_intercom_ultimo_squillo",
  anchor: "citofono",
  history: 8,
  layout: "overlay",  // o "sotto"
  confirm_open: true,
  listen_on_ring: false,
};

class VimarIntercomCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement(EDITOR_TAG);
  }

  static getStubConfig(_hass, entities = []) {
    const reg = _hass?.entities || {};
    const camera = Object.keys(reg).find((id) => id.startsWith("camera.") && reg[id].platform === "vimar_intercom")
      || entities.find((e) => e.startsWith("camera.vimar_intercom")) || DEFAULTS.camera;
    return { camera, name: DEFAULTS.name, layout: DEFAULTS.layout, history: DEFAULTS.history };
  }

  setConfig(config) {
    this._cfg = { ...DEFAULTS, ...config };
    this.setAttribute("layout", this._cfg.layout === "sotto" ? "sotto" : "overlay");  // il CSS si aggancia qui
    if (this._root) {  // l'editor richiama setConfig sulla card viva: nome, cronologia e layout cambiano subito
      this._applyCfg();
      this._histKey = null;
      if (this._hass) this._render();
    }
  }

  // entity_id da usare per camera / status / lock / last_ring (vedi l'intestazione).
  _ent(key) {
    const hass = this._hass, want = this._cfg[key];
    if (!hass || hass.states[want]) return want;
    if (key === "camera") {
      const reg = hass.entities || {};
      return Object.keys(reg).find((id) => id.startsWith("camera.") && reg[id].platform === "vimar_intercom")
        || want;
    }
    return hass.states[this._ent("camera")]?.attributes?.card_entities?.[key] || want;
  }

  _applyCfg() {
    this._root.querySelector(".name").textContent = this._cfg.name;
    this._log.hidden = !(this._cfg.history > 0);
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) {
      this._build();
      this._toAnchor();
    }
    if (this._video) this._video.hass = hass;
    this._render();
  }

  _render() {
    const hass = this._hass;
    const raw = hass.states[this._ent("status")]?.state;
    const state = LABEL[raw] ? raw : "offline";  // unknown/unavailable: come non raggiungibile
    const on = !!this._ws;
    const was = this._state;
    this._state = state;
    const live = LIVE.includes(state);
    // Riaggancio: il video resta HOLD_MS con i tasti spenti, così il secondo tocco di "Apri"
    // non cade sulla card sotto quando la dashboard rifluisce. Niente animazione di altezza.
    if (was === "in_call" && state === "idle") {
      clearTimeout(this._holdT);
      this._card.classList.add("hold");
      this._holdT = setTimeout(() => { this._holdT = null; this._card.classList.remove("hold"); this._render(); }, HOLD_MS);
    } else if (live && this._holdT) {
      // Chiamata nuova durante l'attesa: player nuovo (WebSocket e decoder), non
      // quello della chiamata prima, che avrebbe in pancia i suoi ultimi fotogrammi.
      clearTimeout(this._holdT);
      this._holdT = null;
      this._card.classList.remove("hold");
      this._live = undefined;
    }
    const show = live || !!this._holdT;
    this._setVideo(show);
    this._card.dataset.state = state;
    this._card.classList.toggle("live", show);
    if (state === "ringing" && was !== "ringing") this._setDrawer(false);  // lo squillo non va coperto
    this._tickSetup();
    if (was === "calling" && state === "idle" && !this._cancelled && this._err.textContent === this._hint) {
      this._err.textContent = REFUSED;
    } else if (state !== "idle" && state !== was && this._err.textContent === REFUSED) {
      this._err.textContent = this._hint;
    }
    if (state !== "calling") this._cancelled = false;
    const lr = hass.states[this._ent("last_ring")], last = lr?.state, a = lr?.attributes || {};
    this._last.textContent = isNaN(Date.parse(last)) ? "" : `ultimo ${when(last)}`;
    // La lista si ricarica anche quando arrivano la foto (subito, poi quella migliore) e il clip.
    const key = `${last}|${this._live}|${a.foto_url}|${a.clip_url}`;
    if (this._cfg.history > 0 && key !== this._histKey) {
      this._histKey = key;
      this._loadHistory();
    }

    // Posti fissi: [vedi / annulla / riaggancia] [parla / rispondi / microfono] [apri].
    // Allo squillo "Vedi esterno" resta al suo posto, spento: lo slot non si svuota.
    this._view.hidden = ["calling", "in_call"].includes(state);
    this._view.disabled = state !== "idle" || this._view.classList.contains("busy");
    this._hangup.hidden = !["calling", "in_call"].includes(state);
    this._label(this._hangup, state === "calling" ? "Annulla" : "Riaggancia");
    const ring = state === "ringing", inCall = state === "in_call" || state === "calling";
    // L'audio automatico è riuscito (_autoAudioTried) ma il gesto vero mancava (iOS):
    // "Microfono" diventa "Audio", ben visibile, finché non si tocca — un microfono
    // spento si legge come "muto", non come invito a toccare.
    const audioHint = inCall && !on && this._audioBlocked;
    this._talk.className = ring || audioHint ? "ok answer" : on ? "fill" : "";
    this._talk.setAttribute("aria-pressed", on);
    this._icon(this._talk, ring ? "mdi:phone" : audioHint ? "mdi:volume-off"
      : on || !inCall ? "mdi:microphone" : "mdi:microphone-off");
    this._label(this._talk, ring ? "Rispondi" : audioHint ? "Audio" : inCall ? "Microfono" : "Parla");
    this._talk.disabled = state === "offline" || (!window.isSecureContext && !ring)
      || (state === "calling" && !on);
    this._open.disabled = state === "offline" || hass.states[this._ent("lock")]?.state === "unavailable";
    if ((state === "idle" || state === "offline") && this._ws) this._stopAudio();

    // Arrivo dall'ancora (link della notifica) già "in_call" (l'automazione ha risposto
    // lei, con vimar_intercom.answer): l'audio si aggancia da sola, un tentativo per
    // chiamata — se l'utente stacca il microfono a mano non si riattacca da sola, e se
    // iOS tiene l'audio sospeso senza un tocco vero si rinuncia in silenzio, ma il tasto
    // diventa "Audio" (sopra): resta un tocco solo, ben visibile. Mai per
    // "ringing"/"calling": aprire la pagina non risponde né chiama da sola.
    if (state === "in_call") {
      if (!this._autoAudioTried && !this._ws && !this._starting && window.isSecureContext
          && this._cfg.anchor && location.hash === `#${this._cfg.anchor}`) {
        this._autoAudioTried = true;
        this._startTalk(true).catch(() => {});
      }
    } else {
      this._autoAudioTried = false;
    }
    // "Audio" (sopra) serve tanto all'aggancio automatico quanto all'ascolto allo squillo
    // (sotto): si azzera solo lasciando gli stati dal vivo, non ad ogni giro di `_render`
    // durante ringing/calling — altrimenti un blocco vero (iOS) sparirebbe e riproverebbe
    // ad ogni aggiornamento di `hass`, anche senza alcun cambio di stato.
    if (!live) this._audioBlocked = false;

    // `listen_on_ring`: si sente il visitatore già a video (ringing/calling/in_call in
    // anteprima), senza rispondere né aprire il microfono — smette da sola a fine
    // squillo/preview o quando parte l'audio vero (_ws, mic compreso: si passa a quello,
    // niente doppio canale). "Rispondi" resta al suo posto durante lo squillo: un tocco
    // solo, già pronto, anche se l'ascolto automatico non parte (iOS senza gesto).
    if (this._cfg.listen_on_ring && live && !this._ws && !this._starting) {
      if (!this._listenWs && !this._listenStarting) this._startListen();
    } else {
      this._stopListen();
    }
  }

  _label(button, text) {
    button.querySelector(".lbl").textContent = text;
  }

  _icon(button, icon) {
    button.querySelector("ha-icon").icon = icon;
  }

  _build() {
    this._root = this.attachShadow({ mode: "open" });
    this._root.innerHTML = `<style>${STYLE}</style>${TEMPLATE}`;
    const $ = (s) => this._root.querySelector(s);
    this._card = $("ha-card");
    this._pill = $(".pill");
    this._last = $(".last");
    this._badge = $(".badge.dyn");   // badge sul video col testo dello stato (dove c'è)
    this._log = $("#log");
    this._photo = $("#photo");
    this._pic = $("#photo img");
    this._still = $(".still");
    this._hist = $(".hist");
    this._empty = $(".empty span");
    this._dlg = $("dialog.photo");
    this._clipEl = $("dialog.photo video");
    // Tocco ovunque (o Esc) chiude, tranne sui controlli del video.
    this._dlg.onclick = (e) => e.target !== this._clipEl && this._dlg.close();
    this._dlg.onclose = () => { this._clipEl.pause(); this._clipEl.removeAttribute("src"); this._clipEl.load(); };
    this._err = $(".err");
    this._hint = "";  // niente avviso permanente: in HTTP il microfono è semplicemente spento
    this._view = $("#view");
    this._talk = $("#talk");
    this._hangup = $("#hangup");
    this._open = $("#open");
    this._videoBox = $("#video");
    this._applyCfg();
    this._view.className = "fill";
    this._open.setAttribute("aria-label", "Apri portone, tocca due volte");
    if (!window.isSecureContext) this._talk.title = "Per parlare serve Home Assistant in HTTPS.";
    for (const b of [this._log, this._photo]) b.onclick = () => this._setDrawer(this._card.dataset.drawer !== "true");
    this._view.onclick = () => this._call("call", this._view).catch(() => {});
    this._talk.onclick = () => (this._ws ? this._stopAudio() : this._starting || this._startTalk());
    this._hangup.onclick = () => this._call("hangup", this._hangup).catch(() => {});
    this._open.onclick = () => this._openDoor();
    // Un avviso vive SAY_MS al posto della riga di stato, poi sparisce (NO_ANSWER resta finché si collega).
    new MutationObserver(() => {
      clearTimeout(this._sayT);
      const t = this._err.textContent;
      if (t && t !== NO_ANSWER) this._sayT = setTimeout(() => { if (this._err.textContent === t) this._err.textContent = ""; }, SAY_MS);
    }).observe(this._err, { childList: true, characterData: true, subtree: true });
  }

  _setDrawer(open) {
    this._card.dataset.drawer = open;
    for (const b of [this._log, this._photo]) b.setAttribute("aria-expanded", open);
    this._icon(this._log, open ? "mdi:menu-close" : "mdi:menu-open");
  }

  // Link diretto (es. dalla notifica): con l'URL .../camera#citofono la card si porta in
  // vista. HA non lo fa per le card; "location-changed" è la navigazione interna di HA.
  // Si ricontrolla anche l'audio automatico (_render, in fondo): l'hash può arrivare
  // (hashchange, navigazione HA) senza che lo stato sia appena cambiato — se non si
  // richiama _render qui, un "in_call" già in corso non aggancerebbe mai l'audio da solo.
  _toAnchor = () => requestAnimationFrame(() => {
    const a = this._cfg?.anchor;
    if (a && this._root && this.isConnected && location.hash === `#${a}`) {
      this.scrollIntoView({ block: "start" });
      if (this._hass) this._render();
    }
  });

  connectedCallback() {
    for (const e of ["hashchange", "location-changed"]) window.addEventListener(e, this._toAnchor);
    this._toAnchor();
    // Rimessa in pagina (cambio di vista e ritorno, a chiamata in corso): il riquadro
    // video si rifà subito, senza aspettare un `hass` nuovo da HA.
    if (this._root && this._hass) this._render();
  }

  // Ultimi squilli: righe da 52 px con separatore quando cambia il giorno. La foto più
  // recente fa da tasto cronologia (e da sfondo dove la scena è visibile da fermo).
  async _loadHistory() {
    const n = (this._histN = (this._histN || 0) + 1);
    try {
      const rings = await this._hass.callApi("GET", `vimar_intercom/rings?limit=${this._cfg.history}`);
      // ?v=: la foto migliore sostituisce la prima sullo stesso nome; il browser non tiene la vecchia.
      const srcs = await Promise.all(rings.map((r) => r.photo && this._sign(`${r.photo}?v=${r.photo_v}`)));
      if (n !== this._histN) return;  // arrivata dopo una più recente
      const nodes = [];
      let day;
      rings.forEach((r, i) => {
        const d = new Date(r.time).toDateString();
        if (d !== day) {
          day = d;
          const l = dayLabel(r.time);
          if (l) { const s = document.createElement("div"); s.className = "day"; s.textContent = l; nodes.push(s); }
        }
        nodes.push(this._ringItem(r, srcs[i]));
      });
      this._hist.replaceChildren(...nodes);
      this._empty.textContent = "Nessuno squillo registrato";
      const still = srcs.find(Boolean);
      for (const img of [this._still, this._pic]) if (still) img.src = still; else img.removeAttribute("src");
      this._photo.disabled = !rings.length;
    } catch {
      if (n !== this._histN) return;
      this._hist.replaceChildren();
      this._empty.textContent = "Cronologia non disponibile";
    }
  }

  async _sign(file) {
    return (await this._hass.callWS({ type: "auth/sign_path", path: `/api/vimar_intercom/rings/${file}` })).path;
  }

  _ringItem(r, src) {
    const b = document.createElement("button");
    b.className = "ring";
    b.dataset.outcome = r.outcome;
    b.innerHTML = `<span class="th"><ha-icon icon="mdi:image-off-outline" aria-hidden="true"></ha-icon></span>` +
      `<span class="txt"><span class="at"></span><span class="out"></span></span>`;
    const [th, txt] = b.children;
    const [at, out] = txt.children;
    at.textContent = hm(r.time);
    out.textContent = OUTCOME[r.outcome] || "";
    const cap = `${whenFull(r.time)} · ${out.textContent}`;
    b.setAttribute("aria-label", `Squillo ${cap}${r.clip ? " · video" : ""}`);
    if (src) {
      const img = document.createElement("img");
      img.src = src;
      img.alt = "";
      th.replaceChildren(img);
    }
    if (r.clip) th.insertAdjacentHTML("beforeend", `<span class="play"><ha-icon icon="mdi:play-circle" aria-hidden="true"></ha-icon></span>`);
    if (!src && !r.clip) b.disabled = true;
    else {
      b.onclick = async () => {  // firmata di nuovo: la prima firma scade dopo poco
        try {
          const img = this._dlg.querySelector("img");
          img.hidden = !!r.clip;
          this._clipEl.hidden = !r.clip;
          if (r.clip) this._clipEl.src = await this._sign(r.clip);
          else img.src = await this._sign(`${r.photo}?v=${r.photo_v}`);
          this._dlg.querySelector(".cap").textContent = cap;
          this._dlg.showModal();
          if (r.clip) this._clipEl.play().catch(() => {});  // dove non parte da solo (iOS, dopo l'await) ci sono i controlli
        } catch (e) {  // HA scollegato, file sparito: detto sulla card, non in console
          this._err.textContent = `Media non disponibile: ${e.message || e}`;
        }
      };
    }
    return b;
  }

  // Stato con i secondi di collegamento; oltre SETUP_TIMEOUT_S lo dice in chiaro.
  _tickSetup() {
    const calling = this._state === "calling";
    if (calling && !this._setupAt) {
      this._setupAt = Date.now();
      this._setupTimer = setInterval(() => this._tickSetup(), 1000);
    } else if (!calling && this._setupAt) {
      clearInterval(this._setupTimer);
      this._setupAt = null;
      if (this._err.textContent === NO_ANSWER) this._err.textContent = this._hint;
    }
    const s = this._setupAt ? Math.round((Date.now() - this._setupAt) / 1000) : 0;
    this._pill.textContent = calling ? `${LABEL.calling} ${s} s` : LABEL[this._state];
    this._badge.textContent = this._pill.textContent;
    if (s >= SETUP_TIMEOUT_S && this._err.textContent === this._hint) this._err.textContent = NO_ANSWER;
  }

  // Il riquadro video. Dal vivo, con WebCodecs: i NAL del WebSocket su un canvas
  // (NalPlayer), senza aprire lo stream di HA; se il player non può, o si rompe,
  // la card standard di HA. Da fermo (o senza WebCodecs) sempre quella.
  _setVideo(live) {
    if (this._live === live) return;
    this._live = live;
    this._player?.close();
    this._player = null;
    if (!live || !NalPlayer.ok()) return this._setPicture(live);
    const canvas = document.createElement("canvas");
    this._player = new NalPlayer(this._hass, canvas, () => {
      this._player = null;
      if (this._live) this._setPicture(true);
    });
    this._videoBox.replaceChildren(canvas);
    this._video = null;
  }

  // La card standard di HA (picture-entity). "live" apre lo stream, che da fermo
  // farebbe chiamare la targa: lo si usa solo a chiamata o squillo in corso.
  async _setPicture(live) {
    this._helpers ||= window.loadCardHelpers();
    const el = (await this._helpers).createCardElement({
      type: "picture-entity", entity: this._ent("camera"), camera_view: live ? "live" : "auto",
      show_name: false, show_state: false, tap_action: { action: "none" }, hold_action: { action: "none" },
    });
    if (this._live !== live) return;  // stato cambiato nel frattempo
    el.hass = this._hass;
    this._videoBox.replaceChildren(el);
    this._video = el;
  }

  // Doppio tocco (confirm_open, default): il primo arma per 3 s, il secondo apre. La pressione lunga su iOS
  // litiga con VoiceOver e col menu contestuale; confirm() si conferma di riflesso.
  async _openDoor() {
    if (this._cfg.confirm_open && !this._armed) {
      this._resetOpen();
      this._armed = true;
      this._openTimer = setTimeout(() => this._resetOpen(), 3000);
      this._open.className = "warn";
      this._label(this._open, "Conferma");
      return;
    }
    this._resetOpen();
    this._err.textContent = this._hint;
    try {
      await this._hass.callService("lock", "unlock", { entity_id: this._ent("lock") });
      this._open.className = "ok";
      this._icon(this._open, "mdi:check");
      this._label(this._open, "Aperto");
      this._openTimer = setTimeout(() => this._resetOpen(), 3000);
    } catch (e) {
      this._err.textContent = `Apertura non riuscita: ${e.message || e}`;
    }
  }

  _resetOpen() {
    clearTimeout(this._openTimer);
    this._armed = false;
    this._open.className = "";
    this._icon(this._open, "mdi:door-open");
    this._label(this._open, "Apri");
  }

  async _call(service, button) {
    if (service === "hangup") {
      this._stopAudio();
      this._cancelled = this._state === "calling";  // "Annulla": non è un rifiuto della targa
    }
    this._err.textContent = this._hint;
    button.classList.add("busy");
    button.disabled = true;
    try {
      const r = await this._hass.callService("vimar_intercom", service, {}, undefined, true, true);
      if (r?.response?.ok === false) throw new Error(r.response.result);
    } catch (e) {
      this._err.textContent = `Non riuscito: ${e.message || e}`;
      throw e;
    } finally {
      button.classList.remove("busy");
      button.disabled = false;
    }
  }

  // `auto`: chiamata da sola all'arrivo sull'ancora già "in_call" (vedi _render), non da
  // un tocco. Niente "Rispondi" in HTTP (aprire la pagina non risponde da sola) e, se
  // Safari/iOS tiene l'AudioContext sospeso senza un gesto vero, si rinuncia in silenzio
  // prima ancora di chiedere il microfono: resta il tocco su "Microfono".
  async _startTalk(auto = false) {
    this._stopListen();  // l'ascolto allo squillo lascia il posto all'audio vero (stesso canale)
    this._err.textContent = this._hint;
    if (!window.isSecureContext) {  // solo "Rispondi": video sì, voce no
      if (auto) return;
      await this._call("answer", this._talk).catch(() => {});
      return;
    }
    this._starting = true;  // doppio tocco durante il permesso: una chiamata sola
    // Il tocco era "Rispondi": se lo squillo finisce mentre iOS chiede il permesso
    // del microfono (ha risposto il Tab), non si chiama la targa al suo posto.
    const answering = this._state === "ringing";
    // Nel gesto, prima di ogni await: creato dopo, Safari/iOS lo lascia sospeso
    // (niente voce del visitatore e onaudioprocess fermo, quindi niente microfono).
    const ctx = new AudioContext();
    if (auto) {
      await ctx.resume().catch(() => {});
      if (ctx.state !== "running") {  // niente gesto vero: non si chiede nemmeno il microfono
        ctx.close();
        this._starting = false;
        this._audioBlocked = true;  // "Microfono" → "Audio": un tocco resta a vista
        this._render();
        return;
      }
    }
    let mic;
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
      if (this._state === "ringing") await this._call("answer", this._talk);
      else if (answering && this._state !== "in_call") throw new Error("lo squillo è finito");
      else if (this._state !== "in_call") await this._call("call", this._talk);
      this._audioBlocked = false;
      await this._openAudio(mic, ctx);
    } catch (e) {
      if (!auto && this._err.textContent === this._hint) this._err.textContent = `Audio non disponibile: ${e.message || e}`;
      mic?.getTracks().forEach((t) => t.stop());  // il microfono non resta acceso
      if (!this._audio) ctx.close();
      this._stopAudio();
    } finally {
      this._starting = false;
    }
  }

  // Riproduce la voce del visitatore (0x01 + PCM16LE) su un AudioContext: usato sia dal
  // parlato vero (_openAudio, col microfono) sia dal solo ascolto allo squillo
  // (_startListen, senza microfono). Un chiusura sola per chiamata: `playAt` vive qui.
  _pcmSink(ctx) {
    let playAt = 0;
    return (ev) => {
      if (typeof ev.data === "string" || new Uint8Array(ev.data, 0, 1)[0] !== 0x01) return;
      const pcm = new Int16Array(ev.data.slice(1));
      const buf = ctx.createBuffer(1, pcm.length, RATE);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      playAt = Math.max(playAt, ctx.currentTime + 0.05);  // piccolo buffer contro gli scatti
      src.start(playAt);
      playAt += buf.duration;
    };
  }

  // Ascolto senza rispondere (`listen_on_ring`): solo ricezione, niente microfono né
  // "answer"/"call" — un WebSocket audio a sé, separato da quello video (NalPlayer scarta
  // i pacchetti 0x01) e da quello del parlato vero (_openAudio, che lo scavalca: vedi
  // _startTalk). Stesso limite iOS del parlato: senza un gesto vero l'AudioContext resta
  // sospeso, si rinuncia in silenzio e il tasto (se non è "Rispondi") diventa "Audio".
  async _startListen() {
    if (!window.isSecureContext) return;
    this._listenStarting = true;
    try {
      const ctx = new AudioContext();
      await ctx.resume().catch(() => {});
      if (ctx.state !== "running") {
        ctx.close();
        this._audioBlocked = true;
        this._render();
        return;
      }
      const { path } = await this._hass.callWS({ type: "auth/sign_path", path: "/api/vimar_intercom/audio_ws" });
      if (!this.isConnected || !this._cfg.listen_on_ring) return ctx.close();  // stato cambiato nell'attesa
      const ws = new WebSocket(location.origin.replace(/^http/, "ws") + path);
      ws.binaryType = "arraybuffer";
      ws.onmessage = this._pcmSink(ctx);
      ws.onclose = () => { if (this._listenWs === ws) this._stopListen(); };
      this._listenCtx = ctx;
      this._listenWs = ws;
      this._audioBlocked = false;
      this._render();
    } finally {
      this._listenStarting = false;
    }
  }

  _stopListen() {
    this._listenWs?.close();
    this._listenWs = null;
    this._listenCtx?.close();
    this._listenCtx = null;
  }

  async _openAudio(mic, ctx) {
    // Card tolta dalla pagina mentre iOS chiedeva il permesso del microfono: niente
    // WebSocket orfano (chi la chiama spegne il microfono).
    if (!this.isConnected) throw new Error("card chiusa");
    // WebSocket con percorso firmato: il browser non può mandare il token negli header.
    const { path } = await this._hass.callWS({ type: "auth/sign_path", path: "/api/vimar_intercom/audio_ws" });
    const ws = new WebSocket(location.origin.replace(/^http/, "ws") + path);
    this._ws = ws;  // da qui _stopAudio lo chiude anche se qualcosa sotto fallisce
    ws.binaryType = "arraybuffer";
    ctx.resume();
    ws.onmessage = this._pcmSink(ctx);  // voce del visitatore
    ws.onclose = () => {
      // Solo la sessione corrente: chiuso da noi, o un WS vecchio (microfono spento
      // e riacceso in fretta) che si chiude in ritardo e spegnerebbe quello nuovo.
      if (this._ws !== ws) return;
      this._err.textContent = "Audio interrotto.";
      this._stopAudio();
    };

    // Microfono → 8 kHz PCM16. ponytail: ScriptProcessor e media semplice per
    // ricampionare; AudioWorklet se servisse meno latenza.
    const source = ctx.createMediaStreamSource(mic);
    const proc = ctx.createScriptProcessor(2048, 1, 1);
    const step = ctx.sampleRate / RATE;
    proc.onaudioprocess = (ev) => {
      if (ws.readyState !== WebSocket.OPEN) return;
      const inp = ev.inputBuffer.getChannelData(0);
      const n = Math.floor(inp.length / step);
      const out = new Uint8Array(1 + n * 2);
      const view = new DataView(out.buffer);
      out[0] = 0x02;
      for (let i = 0; i < n; i++) {
        let sum = 0;
        const a = Math.floor(i * step), b = Math.floor((i + 1) * step);
        for (let j = a; j < b; j++) sum += inp[j];
        const s = Math.max(-1, Math.min(1, sum / (b - a)));
        view.setInt16(1 + i * 2, s * 32767, true);
      }
      ws.send(out);
    };
    source.connect(proc);
    proc.connect(ctx.destination);  // necessario perché onaudioprocess giri (esce silenzio)
    this._audio = { ctx, mic, proc, source };
    this._render();
  }

  _stopAudio() {
    const a = this._audio;
    this._audio = null;
    if (a) {
      a.proc.disconnect();
      a.source.disconnect();
      a.mic.getTracks().forEach((t) => t.stop());
      a.ctx.close();
    }
    const ws = this._ws;
    this._ws = null;
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    if ((a || ws) && this._root) this._render();
  }

  disconnectedCallback() {
    for (const e of ["hashchange", "location-changed"]) window.removeEventListener(e, this._toAnchor);
    this._stopAudio();
    this._stopListen();
    if (this._player) {  // card tolta dalla pagina: il WS video non resta aperto
      this._player.close();
      this._player = null;
      this._live = undefined;  // al prossimo hass si ricrea
    }
    clearInterval(this._setupTimer);
    clearTimeout(this._holdT);
    this._holdT = null;  // altrimenti al rientro il riquadro resterebbe "dal vivo" da fermo
    this._card.classList.remove("hold");
    this._setupAt = null;
  }

  getCardSize() {
    return this._live || this._card?.dataset.drawer === 'true' ? 4 : 1;
  }
}

// Editor visuale: un ha-form di HA con cinque campi. Le altre chiavi (status, lock, last_ring,
// anchor) restano in YAML e passano intatte. Un campo svuotato o lasciato al default non finisce in YAML.
const EDITOR_TAG = "vimar-intercom-card-editor";
const SCHEMA = [
  { name: "camera", selector: { entity: { domain: "camera" } } },
  { name: "name", selector: { text: {} } },
  { name: "layout", selector: { select: { mode: "dropdown", options: [
    { value: "overlay", label: "Video a tutta card" },
    { value: "sotto", label: "Tasti sotto il video" },
  ] } } },
  { name: "history", selector: { number: { min: 0, max: 50, mode: "box" } } },
  { name: "confirm_open", selector: { boolean: {} } },
  { name: "listen_on_ring", selector: { boolean: {} } },
];
const FIELD = { camera: "Telecamera", name: "Nome", layout: "In diretta", history: "Squilli in cronologia (0 = niente)",
  confirm_open: "Apri con doppio tocco", listen_on_ring: "Ascolta il visitatore durante lo squillo" };

class VimarIntercomCardEditor extends HTMLElement {
  setConfig(config) {
    this._cfg = config;
    this._sync();
  }

  set hass(hass) {
    this._hass = hass;
    this._sync();
  }

  _sync() {
    if (!this._hass || !this._cfg) return;  // il selettore entità vuole hass
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.schema = SCHEMA;
      this._form.computeLabel = (s) => FIELD[s.name] || s.name;
      this._form.addEventListener("value-changed", (e) => {
        e.stopPropagation();
        const config = { ...this._cfg };
        for (const { name } of SCHEMA) {
          const v = e.detail.value[name];
          if (v === undefined || v === "" || v === DEFAULTS[name]) delete config[name]; else config[name] = v;
        }
        this._cfg = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.append(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = { ...DEFAULTS, ...this._cfg };
  }
}

// Il frontend di HA, dove il browser non ha i registri "scoped" (Safari/iOS), installa un
// polyfill che sostituisce customElements: definita prima, la card si perde ("Custom element
// doesn't exist"). Si definisce quando HA è partito; le card in attesa si ricostruiscono da sole.
const TAG = "vimar-intercom-card";
const ready = () => document.querySelector("home-assistant")?.hass;
const define = () => {
  customElements.get(TAG) || customElements.define(TAG, VimarIntercomCard);
  customElements.get(EDITOR_TAG) || customElements.define(EDITOR_TAG, VimarIntercomCardEditor);
};
if (ready()) define();
else {
  const wait = setInterval(() => ready() && (clearInterval(wait), define()), 200);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "vimar-intercom-card",
  name: "Citofono Vimar",
  description: "Video (anche allo squillo), parla/ascolta, apri il portone e cronologia degli squilli.",
  preview: true,
});
