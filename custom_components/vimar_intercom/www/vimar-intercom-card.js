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
// registro del frontend; stato, ultimo squillo, serratura e impostazioni dall'attributo
// `card_entities` della camera. Scritte e esistenti, vincono quelle della config.
//   anchor: citofono      (URL con #citofono: la card si porta in vista; se lo stato è già
//                         "in_call" — es. "Rispondi" premuto sulla notifica, che risponde
//                         dall'automazione — anche l'audio riparte da sola; "" = no)
//   history: 8            (ultimi squilli con foto e clip, se c'è la cartella foto; 0 = no)
//   confirm_open: true    (Apri chiede un secondo tocco; false = apre al primo)
//   shortcuts: [lock.x]   (tasti tondi "Apri" sulla card a riposo, in tutti i layout, senza aprire il popup né
//                         chiamare la targa; entità lock (unlock) o button (press), anche {entity, name, icon};
//                         default = la serratura. Rispettano confirm_open. In diretta stanno i tasti veri;
//                         nel popup "Apri" apre la prima e le altre stanno in fila sotto la barra)
//   listen_on_ring: false (si sente il visitatore già allo squillo, senza rispondere:
//                         solo ricezione, il microfono resta spento; anche dall'editor)
//   layout: overlay       (o "sotto" o "popup"; anche dall'editor visuale)
//   compact_style: pillola (solo layout popup: la card compatta in dashboard, "pillola" o "tile")
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
//   layout: popup     In dashboard la card è compatta (foto dell'ultimo squillo + stato). Un tocco, o il
//                     tasto cronologia, apre un <dialog> (pannello scuro tondo centrato, margini 12 px, max 720) dove va la
//                     card intera, video con sotto i tasti [Parla|Rispondi] [Apri] [Riaggancia]. Il tocco
//                     sulla card = "Vedi esterno"; la cronologia non chiama mai la targa. Allo squillo (o
//                     in_call/calling dall'ancora) si apre da sola, una volta per squillo. Chiuderlo
//                     (X, Esc, tocco fuori) riaggancia se la chiamata l'ha avviata la card, e chiude l'audio.
//
// Stesso DOM per i layout: cambia il CSS, agganciato all'attributo `layout` sull'host (blocchi
// :host([layout="overlay"]) / "sotto" / "popup" in fondo a STYLE); il popup in più sposta la card nel <dialog>.
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
const FIT_KEY = "vimar_intercom_card_fit";
const NO_ANSWER = "La targa non risponde, riprova.";
const REFUSED = "La targa non ha accettato, riprova.";
const SETUP_TIMEOUT_S = 20;  // oltre, "la targa non risponde" (il cloud a volte ci mette 15 s)
const HOLD_MS = 1500;        // dopo il riaggancio: video fermo, tasti spenti, poi la card si richiude
const OPEN_FLASH_MS = 2000;  // "Aperto" / "Errore" sul tasto
const PENDING_MS = 10000;   // "Collegamento…" subito al tocco, senza aspettare lo stato di HA; oltre, si lascia stare
const SAY_MS = 4000;         // un avviso resta 4 s al posto della riga di stato
const LIVE = ["ringing", "calling", "in_call"];
const OUTCOME = { answered: "Risposto", declined: "Rifiutato", away: "Messaggio di assenza",
                  missed: "Nessuna risposta" };
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

const CA = ':host([layout="popup"][compact]) ha-card:not(.pop)';
const CP = ':host([layout="popup"][compact="pillola"]) ha-card:not(.pop)';
const CT = ':host([layout="popup"][compact="tile"]) ha-card:not(.pop)';
const STYLE = `
  /* Misure e colori ritoccabili senza toccare il resto: chip sul video, tondi delle scorciatoie e della barra, pannello popup. */
  :host { display: block; scroll-margin-top: calc(var(--header-height, 56px) + 8px);
          /* Colori: quelli del tema di HA (variabili standard), i valori dopo la virgola sono il ripiego. Tutto il resto usa solo --vi-*. */
          --vi-primary: var(--primary-color, #0b5cad);
          --vi-ok: var(--success-color, #30b35f);
          --vi-bad: var(--error-color, #e5483d);
          --vi-warn: var(--warning-color, #ffb547);
          --vi-info: var(--info-color, #6cb2ff);
          --vi-line: var(--divider-color, rgba(127,127,127,.3));
          --vi-ink: var(--primary-text-color, #1b1b1f);
          --vi-icon: var(--state-icon-color, var(--secondary-text-color, #6f6a60));
          --vi-card: var(--ha-card-background, var(--card-background-color, #fff));
          --vi-radius: var(--ha-card-border-radius, 12px);
          /* Tinte derivate: pieno scurito (testo bianco leggibile), chiaro per il vetro scuro, squillo con testo scuro. */
          --vi-primary-solid: color-mix(in srgb, var(--vi-primary) 72%, #000);
          --vi-ok-solid: color-mix(in srgb, var(--vi-ok) 72%, #000);
          --vi-bad-solid: color-mix(in srgb, var(--vi-bad) 78%, #000);
          --vi-primary-lite: color-mix(in srgb, var(--vi-primary) 45%, #fff);
          --vi-ok-lite: color-mix(in srgb, var(--vi-ok) 45%, #fff);
          --vi-bad-lite: color-mix(in srgb, var(--vi-bad) 50%, #fff);
          --vi-warn-lite: color-mix(in srgb, var(--vi-warn) 55%, #fff);
          --vi-warn-bg: color-mix(in srgb, var(--vi-warn) 78%, #fff);
          --vi-on-warn: color-mix(in srgb, var(--vi-warn) 18%, #000);
          --vi-chip: rgba(0,0,0,.45); --vi-sc-size: 40px; --vi-bar-size: 64px; --vi-sc-pop-size: 48px;
          --vi-pop-bg: #111; --vi-pop-radius: 32px; --vi-backdrop: rgba(15,15,20,.5);
          --vi-glass: rgba(20,22,26,.5); --vi-glass-bar: rgba(20,22,26,.55); --vi-glass-btn: rgba(255,255,255,.18); }
  /* Senza backdrop-filter il vetro non sfoca: più opaco, così il testo resta leggibile. */
  @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
    :host { --vi-glass: rgba(20,22,26,.72); --vi-glass-bar: rgba(20,22,26,.72); }
  }
  ha-card { position: relative; overflow: hidden; --st: var(--vi-primary); --ink: var(--primary-text-color, #1b1b1f);
            --dim: var(--secondary-text-color, #6f6a60); --fill: color-mix(in srgb, var(--ink) 7%, transparent); }
  [data-state="ringing"] { --st: var(--vi-warn); }
  [data-state="calling"] { --st: var(--vi-info); }
  [data-state="in_call"] { --st: var(--vi-ok); }
  [data-state="ringing"] { --dot: var(--vi-warn); }
  [data-state="calling"] { --dot: var(--vi-info); }
  [data-state="in_call"] { --dot: var(--vi-ok); }
  [data-state="offline"] { --st: var(--disabled-text-color, var(--dim)); }
  button { all: unset; box-sizing: border-box; position: relative; cursor: pointer; -webkit-tap-highlight-color: transparent; }
  button:disabled { cursor: default; }
  button:focus-visible { outline: 2px solid var(--vi-primary); outline-offset: 2px; }
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
  .err { color: var(--vi-warn); font-weight: 500; }
  .sub:has(.err:not(:empty)) > :not(.err) { display: none; }

  /* Foto dell'ultimo squillo = tasto cronologia (senza foto: campanello, non cliccabile). */
  #photo { flex: none; width: 56px; height: 44px; border-radius: 8px; overflow: hidden; display: grid; place-items: center;
           color: var(--st); background: color-mix(in srgb, var(--st) 14%, transparent); transition: box-shadow .15s; }
  #photo img { display: none; width: 100%; height: 100%; object-fit: cover; }
  #photo img[src] { display: block; }
  #photo img[src] + ha-icon { display: none; }
  #photo ha-icon { --mdc-icon-size: 26px; }
  #photo:focus-visible { outline-offset: 0; }
  /* Bollino "cronologia" sull'angolo della foto: dice che è un tasto anche da fermo. */
  #photo .hb { position: absolute; right: 3px; bottom: 3px; width: 18px; height: 18px; border-radius: 50%; display: grid;
               place-items: center; --mdc-icon-size: 13px; color: #fff; background: var(--vi-chip); }
  #photo:disabled .hb { opacity: .5; }
  [data-drawer="true"] #photo { box-shadow: inset 0 0 0 2px var(--vi-primary); }
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
  [data-outcome="answered"] { --oc: var(--vi-ok); }
  [data-outcome="declined"] { --oc: var(--vi-bad); }
  [data-outcome="away"] { --oc: var(--vi-info); }
  [data-outcome="missed"] { --oc: var(--vi-warn); }
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
         place-items: center; color: #fff; background: var(--vi-chip); }
  #log ha-icon { --mdc-icon-size: 22px; }
  .live #log { display: grid; }
  [data-drawer="true"] #log { background: var(--vi-primary); }

  /* Muto locale (audio in arrivo dalla targa): tondo sul video, accanto alla cronologia;
     visibile per tutta la diretta (in JS, hidden segue lo stato "live"), non solo mentre suona. */
  #mute { position: absolute; top: 8px; right: 56px; z-index: 3; width: 40px; height: 40px; border-radius: 50%;
          display: grid; place-items: center; color: #fff; background: var(--vi-chip); }
  #mute ha-icon { --mdc-icon-size: 22px; }

  /* Adatta/Riempi: tondo accanto a cronologia e muto, solo con il video in vista. */
  #fit { position: absolute; top: 8px; right: 104px; z-index: 3; width: 40px; height: 40px; border-radius: 50%; display: none;
         place-items: center; color: #fff; background: var(--vi-chip); }
  #fit ha-icon { --mdc-icon-size: 22px; }
  .live #fit { display: grid; }
  ha-card[data-fit="contain"] #video > canvas, ha-card[data-fit="contain"] .still { object-fit: contain; }
  /* Video and box of the same shape: fill and fit draw the same picture (#42). */
  ha-card[data-fit-same] #fit { display: none !important; }

  /* Impostazioni: ingranaggio accanto alla cronologia (sul video e nella card compatta). */
  #cfg { position: absolute; top: 8px; right: 152px; z-index: 3; width: 40px; height: 40px; border-radius: 50%; display: none;
         place-items: center; color: #fff; background: var(--vi-chip); }
  #cfgc { display: none; }
  #cfg ha-icon { --mdc-icon-size: 22px; }
  .live #cfg { display: grid; }
  .row { display: flex; gap: 6px; min-width: 0; }
  /* Se la riga è stretta (anteprima dell'editor, ~330 px) si accorcia solo la pill più lunga
     ("Vedi es…"): "Parla" e "Apri" restano leggibili per intero. */
  #view, #hangup { order: 1; flex: 0 1 auto; } #talk { order: 2; } #open { order: 3; }
  .row button { display: inline-flex; flex: none; align-items: center; gap: 4px; height: 36px; padding: 0 9px; border-radius: 18px; min-width: 0;
                font-size: 12px; font-weight: 600; color: var(--vi-primary);
                background: color-mix(in srgb, var(--vi-primary) 12%, transparent); transition: transform .1s, background .2s; }
  .row button::before { content: ""; position: absolute; inset: -4px 0; }  /* bersaglio 44 px */
  .ic { display: grid; place-items: center; flex: none; width: 18px; height: 18px; }
  .ic ha-icon { --mdc-icon-size: 18px; }
  .row button:active { transform: scale(.96); }
  button.fill { background: var(--vi-primary); color: var(--text-primary-color); }
  button.ok { background: var(--vi-ok); color: var(--text-primary-color); }
  button.warn { background: var(--vi-warn); color: var(--text-primary-color); }
  button.bad { background: var(--vi-bad); color: var(--text-primary-color); }
  #hangup { background: var(--vi-bad); color: var(--text-primary-color); }
  .row button:disabled { opacity: .45; }
  button.answer { box-shadow: 0 0 0 0 color-mix(in srgb, var(--vi-ok) 55%, transparent); animation: halo 1.4s ease-out infinite; }

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

  /* ---- layout="overlay" (vetro): in diretta il video riempie la card; pill di stato e cronologia in alto,
     barra di vetro flottante in basso con 4 posti uguali [audio][rispondi/parla][apri][rifiuta/riaggancia]. */
  :host([layout="overlay"]) ha-card.live { --ha-card-border-radius: 28px; }
  :host([layout="overlay"]) ha-card:not(.live) .ph { right: min(64%, 320px); }
  :host([layout="overlay"]) .live .head { position: absolute; inset: 0; z-index: 3; display: block; padding: 0; pointer-events: none; }
  :host([layout="overlay"]) .live :is(.ttl, #view) { display: none; }
  :host([layout="overlay"]) .live .badge { top: 12px; left: 12px; height: 36px; padding: 0 14px; gap: 8px; font-size: 14px;
               background: var(--vi-glass); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  :host([layout="overlay"]) .live .badge::before { width: 8px; height: 8px; }
  :host([layout="overlay"]) .live .badge::before, .pop .badge::before { animation: none; background: var(--dot, var(--st)); }
  :host([layout="overlay"]) .live :is(#log, #fit, #cfg) { top: 12px; right: 12px; width: 44px; height: 44px; background: var(--vi-glass);
               -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  :host([layout="overlay"]) .live #fit { right: 64px; }
  :host([layout="overlay"]) .live #cfg { right: 116px; }
  :host([layout="overlay"]) .live[data-drawer="true"] #log { background: var(--vi-primary); }
  :host([layout="overlay"]) .live .row { position: absolute; left: 12px; right: 12px; bottom: 12px; height: 84px; padding: 0; gap: 0;
               display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); align-items: center; pointer-events: none;
               border-radius: 22px; border: 1px solid var(--vi-glass-btn); background: var(--vi-glass-bar);
               -webkit-backdrop-filter: blur(16px); backdrop-filter: blur(16px); }
  :host([layout="overlay"]) .live .row button { pointer-events: auto; display: flex; flex-direction: column; align-items: center;
               justify-content: center; gap: 6px; order: 0; min-height: 64px; height: auto; padding: 0; border-radius: 0; background: none;
               color: #fff; font-size: 12px; font-weight: 600; }
  :host([layout="overlay"]) .live .row button::before { inset: 0; }
  :host([layout="overlay"]) .live .row button:active { transform: none; opacity: .7; }
  :host([layout="overlay"]) .live #talk { grid-column: 2; }
  :host([layout="overlay"]) .live #open { grid-column: 3; }
  :host([layout="overlay"]) .live #hangup { order: 4; grid-column: 4; background: none; color: var(--vi-bad-lite); }
  :host([layout="overlay"]) .live button.ok, :host([layout="overlay"]) .live button.answer { background: none; color: var(--vi-ok-lite); }
  :host([layout="overlay"]) .live button.fill { background: none; color: var(--vi-primary-lite); }
  :host([layout="overlay"]) .live button.warn { background: none; color: var(--vi-warn-lite); }
  :host([layout="overlay"]) .live button.bad { background: none; color: var(--vi-bad-lite); }
  :host([layout="overlay"]) .live button.answer { animation: none; box-shadow: none; }
  :host([layout="overlay"]) .live .ic { width: 24px; height: 24px; background: none; }
  :host([layout="overlay"]) .live .ic ha-icon { --mdc-icon-size: 24px; }
  :host([layout="overlay"]) .live button.busy .ic::after { width: 20px; height: 20px; border-width: 3px; }
  /* Audio (#mute) sta nel primo posto della barra: stessa cella, etichetta da CSS. */
  :host([layout="overlay"]) .live #mute { top: auto; right: auto; bottom: 12px; left: 12px; z-index: 4; width: calc((100% - 24px) / 4);
               height: 84px; border-radius: 0; background: none; display: flex; flex-direction: column; align-items: center;
               justify-content: center; gap: 6px; font-size: 12px; font-weight: 600; }
  :host([layout="overlay"]) .live #mute::after { content: "Audio"; line-height: 16px; }
  :host([layout="overlay"]) .live #mute ha-icon { --mdc-icon-size: 24px; }
  /* Scorciatoie "Apri" extra: chip di vetro sopra la barra (la prima è "Apri", già nella barra). */
  :host([layout="overlay"]) .live .sc { display: flex; position: absolute; left: 12px; right: 12px; bottom: 108px; justify-content: center;
               gap: 8px; pointer-events: none; }
  :host([layout="overlay"]) .live .sc:not(:has(button:nth-child(2))), :host([layout="overlay"]) .live .sc button:first-child { display: none; }
  :host([layout="overlay"]) .live .drawer { top: 64px; right: 12px; bottom: 108px; border-radius: 16px; }
  :host([layout="overlay"]) ha-card.live:has(.sc button:nth-child(2)) .drawer { bottom: 156px; }

  /* ---- layout="sotto": in diretta il video sta sopra la riga, i tasti restano nella riga (a piena larghezza).
     Da fermo la cronologia è una lista sotto la riga, non un palco 4:3. */
  :host([layout="sotto"]) .live .ttl, :host([layout="sotto"]) .live .row { grid-column: 1 / 3; }
  :host([layout="sotto"]) .live .head { padding-top: 8px; }
  :host([layout="sotto"]) ha-card:not(.live)[data-drawer="true"] .media { order: 1; aspect-ratio: auto; background: none; color: var(--ink); }
  :host([layout="sotto"]) ha-card:not(.live)[data-drawer="true"] :is(.still, .ph) { display: none; }
  :host([layout="sotto"]) ha-card:not(.live) .drawer { position: static; width: auto; transform: none; visibility: visible; box-shadow: none;
               background: none; transition: none; border-top: 1px solid var(--vi-line); }
  :host([layout="sotto"]) ha-card:not(.live) .hist { max-height: 176px; }

  /* ---- layout="popup": compatta in dashboard (mai .live: niente video). Nel <dialog> la card intera, in un
     pannello tondo centrato (vetro sul video: margini 12 px, max 720): il video riempie il pannello, in alto
     pill di stato + cronologia + X, in basso il pannello di vetro con i 4 tondi [audio][parla|rispondi][apri][riaggancia]
     e sotto i chip delle scorciatoie. Su desktop il pannello è 4:3. */
  :host([layout="popup"]) ha-card:not(.pop) { cursor: pointer; }
  #x { display: none; position: absolute; top: 8px; right: 8px; z-index: 3; place-items: center; color: #fff; background: var(--vi-chip); }
  /* Scorciatoie: tondi sulla card compatta; nel popup solo le altre (la prima è "Apri"), in fila sotto la barra. */
  .sc, #hist, .bell { display: none; }  /* #hist e .bell: solo nella card compatta del layout popup */
  ha-card:not(.live):not(.pop):has(.sc button) .head { grid-template-columns: 56px minmax(0, 1fr) auto; }
  ha-card:not(.live):not(.pop) .sc { display: flex; gap: 4px; grid-column: 3; grid-row: 1 / 3; }
  ha-card:not(.live):not(.pop):has(.sc button) #open { display: none; }  /* da fermo "Apri" è la scorciatoia */
  .sc button { display: flex; flex-direction: column; align-items: center; gap: 2px; max-width: 60px; font-size: 11px; color: var(--ink); }
  .sc button::before { content: ""; position: absolute; inset: -2px -4px; }
  .sc .ic { width: var(--vi-sc-size); height: var(--vi-sc-size); border-radius: 50%; background: color-mix(in srgb, var(--vi-primary) 14%, transparent); color: var(--vi-primary); }
  .sc button.warn .ic { background: var(--vi-warn); color: var(--text-primary-color); }
  .sc button.ok .ic { background: var(--vi-ok); color: var(--text-primary-color); }
  .sc button:disabled { opacity: .45; }

  /* ---- layout="popup", card compatta in dashboard: due stili (compact_style). Stesso DOM: .head diventa la riga/griglia,
     .row e .sc "spariscono" (display: contents) e i loro tasti si dispongono con order. Il tocco fuori dai tasti apre il popup. */
  ${CA} { border-radius: var(--vi-radius); --fill: color-mix(in srgb, var(--ink) 12%, transparent);
          background: var(--vi-card);
          box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.18)); }
  ${CA} .media { display: none; }
  ${CA} :is(.row, .sc) { display: contents; }
  ${CA} .ttl { flex-direction: column; align-items: flex-start; gap: 0; min-width: 0; }
  ${CA} .ttl .sub::before { content: none; }
  ${CA} .pill { color: var(--dim); font-weight: 400; padding: 0; background: none; }
  ${CA} .pill::before, ${CA} #photo .hb { display: none; }
  ${CA} .name { max-width: 100%; }
  ${CA} :is(#view, #talk, #hangup, #open, #hist, .sc button) { flex: none; min-width: 0; max-width: none; animation: none; box-shadow: none; }
  ${CA} :is(#view, #talk, #hangup, #open, #hist, .sc button)::before { content: none; }
  ${CA} :is(#view, #talk, #hangup, #hist, .sc button) .ic { background: none; color: inherit; width: 22px; height: 22px; }
  ${CA} :is(#view, #talk, #hangup, #hist, .sc button) .ic ha-icon { --mdc-icon-size: 22px; }
  ${CA} :is(#open, #view, #talk, #hangup) { display: none; }
  ${CA}[data-state="ringing"] :is(#hist, #cfgc, .last) { display: none; }
  ${CA}[data-state="ringing"] #talk { display: flex; }
  ${CT}[data-state="ringing"] #hangup:not([hidden]) { display: flex; }

  /* Feedback al tocco (compatta): "Collegamento…" blu subito, tondi che girano / verde "Aperto" / rosso "Errore", :active visibile. */
  @keyframes ringpulse { 50% { box-shadow: 0 0 0 5px color-mix(in srgb, var(--vi-info) 45%, transparent); } }
  ${CA}[data-pending="true"] { --ha-card-background: color-mix(in srgb, var(--vi-info) 22%, var(--vi-card));
                background: color-mix(in srgb, var(--vi-info) 22%, var(--vi-card)); --ring: var(--vi-info); }
  ${CA}[data-pending="true"] #photo { animation: ringpulse 1.2s ease-in-out infinite; }
  ${CT}[data-pending="true"] .bell { color: color-mix(in srgb, var(--vi-info) 25%, #000); background: var(--vi-info); }
  ${CA} :is(#hist, #cfgc, .sc button) { transition: transform .1s, filter .1s; }
  ${CA} :is(#hist, #cfgc, .sc button, #view, #talk, #hangup):active { transform: scale(.94); filter: brightness(1.25) saturate(1.1); }
  ${CA}:active:not(:has(button:active)) { filter: brightness(.94); }
  ${CA} .sc button.busy { opacity: .85; }
  ${CA} .sc button.warn { animation: ringpulse 1s ease-in-out infinite; }
  ${CP} .sc button:first-child.bad { background: var(--vi-bad-solid); color: #fff; }
  ${CT} :is(#open, .sc button).bad { color: #fff; background: var(--vi-bad-solid); }

  /* Pillola: 64 px, raggio 32, foto tonda con anello di stato; a destra tondi 44 px. */
  ${CP} { --ha-card-border-radius: 32px; --ring: var(--vi-ok); }
  ${CP}[data-state="offline"] { --ring: var(--st); }
  ${CP} .head { display: flex; align-items: center; gap: 12px; height: 64px; padding: 0 8px; }
  ${CP} .bell { display: none; }
  ${CP} #photo { order: 1; width: 48px; height: 48px; border-radius: 50%; box-shadow: none; border: 2px solid var(--ring); box-sizing: border-box; }
  ${CP} #photo img { border-radius: 50%; }
  ${CP} .ttl { order: 2; flex: 1 1 0; }
  ${CP} .name { font-size: 15px; font-weight: 700; line-height: 20px; }
  ${CP} .sub { font-size: 12px; }
  ${CP} :is(#hist, #talk, .sc button:first-child) { display: grid; place-items: center; width: 44px; height: 44px; padding: 0; border-radius: 50%; }
  ${CP} :is(#hist, #cfgc) { order: 3; background: var(--fill); color: var(--vi-icon); }
  ${CP} #cfgc:not([hidden]), ${CP} #hist { flex: none; display: grid; place-items: center; width: 44px; height: 44px; padding: 0; border-radius: 50%; }
  ${CP} #cfgc ha-icon { --mdc-icon-size: 22px; }
  ${CP}[data-state="ringing"] #cfgc { display: none; }
  ${CT}[data-state="ringing"] #cfgc:not([hidden]) { display: none; }
  ${CP} .sc button:not(:first-child) { display: none; }
  ${CP} #talk { display: none; order: 4; background: var(--vi-ok-solid); color: #fff; }
  ${CP}[data-state="ringing"] #talk { display: grid; }
  ${CP} .sc button:first-child { order: 5; background: var(--vi-primary-solid); color: #fff; }
  ${CP} .sc button:first-child.warn { background: var(--vi-warn); color: var(--vi-on-warn); }
  ${CP} .sc button:first-child.ok { background: var(--vi-ok-solid); color: #fff; }
  ${CP} .lbl { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }  /* solo per i lettori di schermo */
  ${CP} :is(#hist, #talk, .sc button):disabled { opacity: .45; }
  ${CP}[data-state="ringing"] { --ha-card-background: var(--vi-warn-bg); background: var(--vi-warn-bg); --ink: var(--vi-on-warn); --dim: var(--vi-on-warn); --ring: #fff; }

  /* Tile: card 16, riga campanello + nome + miniatura, sotto griglia di tasti da 44. */
  ${CT} { --ha-card-border-radius: 16px; }
  ${CT} .head { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 8px; padding: 12px; }
  ${CT} .head::before { content: ""; order: 4; flex: 0 0 100%; height: 0; margin-top: -4px; }
  ${CT} .bell { order: 1; display: grid; place-items: center; flex: none; width: 40px; height: 40px; margin-right: 4px; border-radius: 50%;
                color: var(--vi-ok); background: color-mix(in srgb, var(--vi-ok) 18%, transparent); }
  ${CT} .bell ha-icon { --mdc-icon-size: 22px; }
  ${CT}[data-state="ringing"] .bell { color: var(--vi-on-warn); background: var(--vi-warn); }
  ${CT} .ttl { order: 2; flex: 1 1 0; }
  ${CT} .name { font-size: 15px; font-weight: 600; line-height: 20px; }
  ${CT}[data-state="ringing"] .name { font-weight: 700; }
  ${CT} .sub { font-size: 13px; line-height: 18px; }
  ${CT} #photo { order: 3; width: 48px; height: 36px; border-radius: 8px; }
  ${CT} :is(#view, #hist, #talk, #hangup, .sc button) { display: flex; flex-direction: row; align-items: center; justify-content: center; gap: 6px;
                height: 44px; flex: 1 1 calc(33.333% - 8px); padding: 0 8px; border-radius: 12px; font-size: 14px; font-weight: 600;
                color: var(--ink); background: var(--fill); }
  ${CT} #view { order: 5; display: flex; }
  ${CT} #view .lbl { font-size: 0; }  /* nel tile solo "Vedi": il nome accessibile resta "Vedi esterno" (aria-label) */
  ${CT} #view .lbl::after { content: "Vedi"; font-size: 14px; }
  ${CT}[data-state="ringing"] #view { display: none; }
  ${CT} :is(#open, .sc button) { order: 6; color: color-mix(in srgb, var(--vi-primary) 60%, var(--ink)); background: color-mix(in srgb, var(--vi-primary) 16%, transparent); }
  ${CT} :is(#open, .sc button).warn { color: var(--vi-on-warn); background: var(--vi-warn); }
  ${CT} :is(#open, .sc button).ok { color: #fff; background: var(--vi-ok-solid); }
  ${CT} #hist { order: 7; }
  ${CT} #cfgc:not([hidden]) { order: 2; display: grid; place-items: center; flex: none; width: 40px; height: 40px; border-radius: 50%; color: var(--vi-icon); background: var(--fill); }
  ${CT} #cfgc ha-icon { --mdc-icon-size: 22px; }
  ${CT} #talk { order: 5; color: #fff; background: var(--vi-ok-solid); font-weight: 700; }
  ${CT}:not([data-state="ringing"]) #talk { display: none; }
  ${CT} #hangup { order: 7; font-weight: 700; color: color-mix(in srgb, var(--vi-bad) 60%, var(--ink)); background: color-mix(in srgb, var(--vi-bad) 14%, transparent); }
  ${CT}[data-state="ringing"] { --ha-card-background: color-mix(in srgb, var(--vi-warn) 14%, var(--vi-card));
                background: color-mix(in srgb, var(--vi-warn) 14%, var(--vi-card)); }
  ${CT} :is(#view, #hist, #talk, #hangup, .sc button):disabled { opacity: .45; }

  /* Popup delle impostazioni: <dialog> nativo, nei colori del tema. */
  dialog.set { width: min(92vw, 420px); max-height: 90vh; margin: auto; padding: 0; border: 0; border-radius: 20px; overflow: auto; box-sizing: border-box;
               color: var(--vi-ink); background: var(--card-background-color, #fff); box-shadow: 0 20px 60px rgba(0,0,0,.35); }
  dialog.set::backdrop { background: rgba(15,15,20,.5); -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px); }
  .set-c { padding: 16px; }  /* il padding sta qui: cliccarlo non deve chiudere il dialog */
  .set-h { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .set-h h2 { flex: 1; margin: 0; font-size: 18px; font-weight: 600; }
  .set-x { display: grid; place-items: center; width: 44px; height: 44px; border-radius: 50%; color: var(--vi-ink);
           background: color-mix(in srgb, var(--vi-ink) 10%, transparent); }
  .set-r { display: flex; align-items: center; gap: 12px; min-height: 52px; padding: 6px 0; border-top: 1px solid var(--vi-line); }
  .set-r:first-of-type { border-top: 0; }
  .set-r.col { flex-direction: column; align-items: stretch; gap: 6px; }
  .set-l { flex: 1; min-width: 0; display: flex; flex-direction: column; font-size: 15px; font-weight: 500; }
  .set-l small { font-size: 12px; font-weight: 400; color: var(--secondary-text-color, #6f6a60); }
  .set-tg { flex: none; width: 52px; height: 32px; border-radius: 16px; background: color-mix(in srgb, var(--vi-ink) 25%, transparent); transition: background .15s; }
  .set-tg::before { content: ""; position: absolute; inset: -6px -4px; }
  .set-tg::after { content: ""; position: absolute; top: 4px; left: 4px; width: 24px; height: 24px; border-radius: 50%; background: #fff; transition: transform .15s; box-shadow: 0 1px 3px rgba(0,0,0,.3); }
  .set-tg[aria-checked="true"] { background: var(--vi-ok-solid); }
  .set-tg[aria-checked="true"]::after { transform: translateX(20px); }
  .set-tg:disabled { opacity: .45; }
  .set-in { box-sizing: border-box; width: 100%; min-height: 44px; padding: 8px 12px; font: inherit; font-size: 15px; border-radius: 10px;
            color: var(--vi-ink); background: color-mix(in srgb, var(--vi-ink) 8%, transparent); border: 1px solid var(--vi-line); }
  select.set-in { width: auto; max-width: 55%; }
  .set-r.col select.set-in { width: 100%; max-width: none; }
  .set-in:focus-visible { outline: 2px solid var(--vi-primary); outline-offset: 1px; }
  .set-e { min-height: 0; margin: 4px 0 0; font-size: 13px; color: var(--vi-bad); }
  .set-e:empty { display: none; }
  .pop .media { display: block; position: absolute; inset: 0; aspect-ratio: auto; background: #000; }
  .pop :is(#x, #log, #fit, #cfg) { display: grid; top: 14px; width: 44px; height: 44px; border-radius: 50%; background: var(--vi-glass);
                       -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  .pop :is(#x, #log, #fit, #cfg, #mute) ha-icon { --mdc-icon-size: 22px; }
  .pop #x { right: 14px; } .pop #log { right: 66px; } .pop #fit { right: 118px; } .pop #cfg { right: 170px; }
  .pop[data-drawer="true"] #log { background: var(--vi-primary); }
  /* Pill di stato larga nella riga in alto (poi: ingranaggio, adatta/riempi, cronologia, X); se non ci sta si accorcia con i puntini, i tondi restano da 44. */
  .pop .badge { display: block; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; top: 14px; left: 14px; right: 224px; height: 44px; padding: 0 14px;
                font-size: 15px; font-weight: 700; line-height: 44px; background: var(--vi-glass); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  .pop .badge::before { content: ""; display: inline-block; vertical-align: middle; margin: -2px 8px 0 0; width: 8px; height: 8px; border-radius: 50%; }
  .pop .ttl, .pop #photo, .pop #view { display: none; }
  /* Pannello di vetro in basso: riga dei tondi + chip delle scorciatoie (le altre: la prima è "Apri"). */
  .pop .head { position: absolute; left: 14px; right: 14px; bottom: 14px; z-index: 3; display: flex; flex-direction: column; align-items: stretch; gap: 12px;
               padding: 14px; border-radius: 26px; border: 1px solid var(--vi-glass-btn); background: var(--vi-glass-bar);
               -webkit-backdrop-filter: blur(18px); backdrop-filter: blur(18px); }
  .pop .row { display: grid; grid-template-columns: repeat(4, 1fr); justify-items: center; gap: 0; }
  .pop #talk { grid-column: 2; } .pop #open { grid-column: 3; } .pop #hangup { grid-column: 4; order: 4; }
  .pop .row button { order: 0; display: grid; place-items: center; width: 56px; height: 56px; padding: 0; border-radius: 50%; background: none; color: #fff; }
  .pop .row button::before { inset: -4px; }
  .pop .row button:active { transform: none; opacity: .7; }
  .pop .row .lbl { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }  /* solo per i lettori di schermo */
  .pop .ic { width: 56px; height: 56px; border-radius: 50%; background: var(--vi-glass-btn); color: #fff; }
  .pop .ic ha-icon { --mdc-icon-size: 24px; }
  .pop #talk .ic, .pop button.ok .ic { background: var(--vi-ok); }
  .pop #talk.fill .ic { background: var(--vi-primary); }
  .pop button.warn .ic { background: var(--vi-warn); }
  .pop button.bad .ic { background: var(--vi-bad); }
  .pop #hangup .ic { background: var(--vi-bad); }
  .pop button.answer { animation: none; box-shadow: none; }
  .pop button.answer .ic { box-shadow: 0 0 0 0 color-mix(in srgb, var(--vi-ok) 55%, transparent); animation: halo 1.4s ease-out infinite; }
  .pop button.busy .ic::after { width: 24px; height: 24px; border-width: 3px; }
  /* Audio (#mute) nel primo posto dei tondi: allineato alla griglia a 4 colonne del pannello. */
  .pop #mute { top: auto; right: auto; bottom: 29px; left: calc((100% - 58px) / 8 + 1px); z-index: 4; width: 56px; height: 56px; border-radius: 50%;
               background: var(--vi-glass-btn); }
  .pop:has(.sc button:nth-child(2)) #mute { bottom: 77px; }
  .pop .sc { display: flex; flex-wrap: nowrap; justify-content: center; gap: 8px; }
  .pop .sc:not(:has(button:nth-child(2))), .pop .sc button:first-child { display: none; }
  .pop .drawer { top: 70px; right: 14px; bottom: 130px; border-radius: 20px; }
  .pop:has(.sc button:nth-child(2)) .drawer { bottom: 178px; }
  /* Chip di vetro (popup e overlay). Tocco 44 px con ::before. */
  .pop .sc button, :host([layout="overlay"]) .live .sc button { flex-direction: row; gap: 6px; height: 36px; max-width: none; padding: 0 14px;
               border-radius: 18px; font-size: 14px; color: #fff; background: var(--vi-glass-btn); pointer-events: auto;
               -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  .pop .sc button::before, :host([layout="overlay"]) .live .sc button::before { inset: -4px 0; }
  .pop .sc .ic, :host([layout="overlay"]) .live .sc .ic { width: 18px; height: 18px; background: none; color: #fff; }
  .pop .sc .ic ha-icon, :host([layout="overlay"]) .live .sc .ic ha-icon { --mdc-icon-size: 18px; }
  /* Il focus da tastiera resta visibile sul vetro. */
  .pop button:focus-visible, :host([layout="overlay"]) .live button:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  dialog.pop { width: calc(100% - 24px); max-width: 720px; height: min(620px, calc(100% - 24px)); max-height: calc(100% - 24px); margin: auto;
               padding: 0; border: 0; border-radius: var(--vi-pop-radius); overflow: hidden; color: #fff; background: var(--vi-pop-bg);
               box-shadow: 0 20px 60px rgba(0,0,0,.45); color-scheme: dark;
               /* Scuro fisso, qualunque tema: le variabili del tema chiaro non passano qui dentro. */
               --primary-text-color: #fff; --secondary-text-color: #c4c4c4; --card-background-color: var(--vi-pop-bg); --ha-card-background: var(--vi-pop-bg); }
  dialog.pop, .hist { overscroll-behavior: contain; }
  dialog.pop::backdrop { background: var(--vi-backdrop); -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px); }
  dialog.pop ha-card { height: 100%; background: none; --ha-card-border-radius: 0; --ha-card-box-shadow: none; --ha-card-border-width: 0; }
  /* Schermo largo (PC, tablet, telefono in orizzontale): il pannello è il video 4:3, a tutta larghezza (al massimo 900 px e 90% dell'altezza),
     con riga in alto e barra dei tasti di vetro SUL video: niente vuoto sotto. Il telefono in verticale resta il pannello alto. */
  @media (min-width: 700px), (min-aspect-ratio: 1 / 1) {
    dialog.pop { width: min(900px, calc(100% - 24px), calc((100vh - 24px) * 4 / 3)); max-width: none; height: fit-content; }  /* non auto: un <dialog> modale si allungherebbe a tutta altezza */
    dialog.pop ha-card { height: auto; }
    .pop .media { position: relative; inset: auto; aspect-ratio: 4 / 3; }
  }
`;

const btn = (id, icon, label) =>
  `<button${id ? ` id="${id}"` : ""} data-icon="${icon}" data-label="${label}"><span class="ic"><ha-icon icon="${icon}" aria-hidden="true"></ha-icon></span>` +
  `<span class="lbl">${label}</span></button>`;
const DRAWER = `<aside class="drawer" id="drawer" aria-label="Ultimi squilli"><div class="hist"></div>
  <div class="empty"><ha-icon icon="mdi:bell-off-outline" aria-hidden="true"></ha-icon><span></span></div></aside>`;
const SCENE = `<div id="video"></div><img class="still" alt="">
  <span class="ph"><ha-icon icon="mdi:doorbell-video" aria-hidden="true"></ha-icon></span>`;
const PHOTO = `<button id="photo" aria-label="Cronologia squilli" title="Cronologia squilli" aria-expanded="false" aria-controls="drawer" disabled>
  <img alt=""><ha-icon icon="mdi:doorbell-video" aria-hidden="true"></ha-icon><ha-icon class="hb" icon="mdi:history" aria-hidden="true"></ha-icon></button>`;
const X = `<button id="x" aria-label="Chiudi"><ha-icon icon="mdi:close" aria-hidden="true"></ha-icon></button>`;
const LOG = `<button id="log" aria-label="Cronologia squilli" title="Cronologia squilli" aria-expanded="false" aria-controls="drawer">
  <ha-icon icon="mdi:history" aria-hidden="true"></ha-icon></button>`;
// Sempre in vista per tutta la diretta: se l'audio non sta ancora suonando (bloccato su
// iOS o mai partito) il tocco avvia l'ascolto — è il gesto vero che sblocca l'AudioContext;
// se sta suonando (ascolto allo squillo o parlato) lo muta/smuta soltanto. Mai la chiamata,
// mai il microfono, mai il WebSocket. Icona sola, senza etichetta.
const MUTE = `<button id="mute" aria-label="Audio" aria-pressed="true">
  <ha-icon icon="mdi:volume-off" aria-hidden="true"></ha-icon></button>`;
// Adatta (video intero, bande scure: predefinito) / Riempi (cover, zoom). Si ricorda per dispositivo.
const FIT = `<button id="fit" aria-label="Riempi schermo" title="Riempi schermo" aria-pressed="false">
  <ha-icon icon="mdi:arrow-expand-all" aria-hidden="true"></ha-icon></button>`;
const HIST = `<button id="hist" aria-label="Cronologia squilli" title="Cronologia squilli" disabled><span class="ic"><ha-icon icon="mdi:history" aria-hidden="true"></ha-icon></span><span class="lbl">Storico</span></button>`;
const BELL = `<span class="bell" aria-hidden="true"><ha-icon icon="mdi:bell"></ha-icon></span>`;
// Impostazioni del citofono (Non disturbare, Segreteria…): tondo con l'ingranaggio, uno sul video e uno nella card compatta.
// Due copie dello stesso tasto (id diversi) perché vivono in contenitori diversi: .media (video) e .head (card compatta).
const cfgBtn = (id) => `<button id="${id}" class="cfg" aria-label="Impostazioni citofono" title="Impostazioni citofono" hidden>
  <ha-icon icon="mdi:cog" aria-hidden="true"></ha-icon></button>`;
const CFG = cfgBtn("cfg"), CFGC = cfgBtn("cfgc");
const SUB = `<span class="sub"><span class="pill" role="status" aria-live="polite"></span><span class="last"></span><span class="err" role="alert"></span></span>`;
const ROW = `<div class="row">${btn("view", "mdi:cctv", "Vedi esterno")}${btn("talk", "mdi:microphone", "Parla")}` +
  `${btn("hangup", "mdi:phone-hangup", "Riaggancia")}${btn("open", "mdi:door-open", "Apri")}</div>`;
// Foto dello squillo in grande, o il suo clip (<video>) se c'è.
const PHOTO_DLG = `<dialog class="photo" aria-label="Squillo"><img alt="Foto dello squillo">` +
  `<video controls playsinline preload="metadata" hidden></video><p class="cap"></p></dialog>`;

const TEMPLATE = `<ha-card>
  <div class="media">${SCENE}<span class="badge dyn" aria-hidden="true"></span>${MUTE}${FIT}${LOG}${CFG}${X}${DRAWER}</div>
  <div class="head">${BELL}${PHOTO}<div class="ttl"><span class="name"></span>${SUB}</div>${ROW}<div class="sc"></div>${HIST}${CFGC}</div>
  ${PHOTO_DLG}</ha-card><dialog class="pop" aria-label="Citofono"></dialog>
  <dialog class="set" aria-label="Impostazioni citofono"></dialog>`;

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
    this.onsize = null;  // the video's size changed: the card checks whether Fill/Fit matters
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
      this.onsize?.();
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

const SC = { lock: ["unlock", "mdi:door-open"], button: ["press", "mdi:gesture-tap-button"] };  // dominio → servizio, icona
const DEFAULTS = {
  name: "Citofono",
  camera: "camera.vimar_intercom_intercom",
  status: "sensor.vimar_intercom_intercom_stato",
  lock: "lock.vimar_intercom_serratura",
  last_ring: "sensor.vimar_intercom_intercom_ultimo_squillo",
  anchor: "citofono",
  history: 8,
  layout: "overlay",  // o "sotto" o "popup"
  compact_style: "pillola",  // card compatta del layout popup: o "tile"
  confirm_open: true,
  listen_on_ring: false,
};

// Popup aperto: la dashboard sotto non scorre (su iOS il dito sulla cronologia la trascinava).
const lockPageScroll = (on) => {
  for (const el of [document.documentElement, document.body]) el.style.overflow = on ? "hidden" : "";
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
    this.setAttribute("layout", ["sotto", "popup"].includes(this._cfg.layout) ? this._cfg.layout : "overlay");  // il CSS si aggancia qui
    this.setAttribute("compact", this._cfg.compact_style === "tile" ? "tile" : "pillola");
    if (this._root) {  // l'editor richiama setConfig sulla card viva: nome, cronologia e layout cambiano subito
      this._applyCfg();
      this._histKey = null;
      if (this._hass) this._render();
    }
  }

  get _popup() {
    return this._cfg.layout === "popup";
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

  get _preview() {
    return !!this.closest("hui-card-preview, hui-dialog-edit-card");
  }

  // Compatta = un tasto (anche da tastiera); nel popup no.
  _armCard() {
    const on = this._popup && !this._pop.open;
    for (const [k, v] of [["role", "button"], ["tabindex", "0"]]) on ? this._card.setAttribute(k, v) : this._card.removeAttribute(k);
  }

  _applyCfg() {
    this._root.querySelector(".name").textContent = this._cfg.name;
    this._log.hidden = !(this._cfg.history > 0);
    this._armCard();
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
    const show = (live || !!this._holdT) && (!this._popup || this._pop.open);  // compatta: niente video
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
    const ring = state === "ringing", inCall = state === "in_call" || state === "calling";
    this._hangup.hidden = !LIVE.includes(state) && !this._pop.open;  // squillo: "Rifiuta" in ogni layout
    this._label(this._hangup, state === "calling" ? "Annulla" : ring ? "Rifiuta" : "Riaggancia");
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
    this._shortcuts();
    this._syncSettings();
    this._histBtn.disabled = this._photo.disabled;
    this._open.disabled = state === "offline" || hass.states[this._ent("lock")]?.state === "unavailable";
    if ((state === "idle" || state === "offline") && this._ws) this._stopAudio();

    // Muto (tondo sul video): sempre visibile per tutta la diretta (ringing/calling/
    // in_call), non solo quando l'audio è già partito — su iOS l'ascolto automatico può
    // restare bloccato senza un tocco vero, e il tasto è anche il modo per darglielo
    // (vedi onclick, sotto: se non sta ancora suonando avvia l'ascolto invece di mutare).
    const audible = (!!this._ws || !!this._listenWs) && !this._muted;
    this._mute.hidden = !live;
    this._applyFit();
    this._icon(this._mute, audible ? "mdi:volume-high" : "mdi:volume-off");
    this._mute.setAttribute("aria-pressed", !audible);

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
    if (!live) { this._audioBlocked = false; this._muted = false; }
    if (LIVE.includes(was) && !live) this._mine = false;  // chiamata finita: non più "della card"  // il muto vale una sessione dal vivo sola

    // `listen_on_ring`: si sente il visitatore già a video (ringing/calling/in_call in
    // anteprima), senza rispondere né aprire il microfono — smette da sola a fine
    // squillo/preview o quando parte l'audio vero (_ws, mic compreso: si passa a quello,
    // niente doppio canale). "Rispondi" resta al suo posto durante lo squillo: un tocco
    // solo, già pronto, anche se l'ascolto automatico non parte (iOS senza gesto).
    // Chiusura solo per fine diretta o audio vero: un ascolto avviato a mano dal tasto
    // Audio (anche a `listen_on_ring` spento) resta finché dura la diretta.
    if (!live || this._ws || this._starting) this._stopListen();
    else if (this._cfg.listen_on_ring && (!this._popup || this._pop.open) && !this._listenWs && !this._listenStarting) this._startListen(true);

    // Popup: si apre da solo allo squillo (o dall'ancora), una volta per squillo.
    if (!live) this._popTried = false;
    else if (this._pop.open) this._popTried = true;
    else if (this._popup && !this._popTried && this.isConnected && !this._preview
        && (state === "ringing" || (this._cfg.anchor && location.hash === `#${this._cfg.anchor}`))) {
      this._popTried = true;
      this._openPop();
    }
  }

  // Sposta la card nel dialog; `view`: il tocco sulla card fa "Vedi esterno".
  _openPop(view) {
    if (this._pop.open) return;
    this._card.classList.add("pop");
    this._pop.append(this._card);
    this._pop.showModal();
    lockPageScroll(true);
    this._armCard();
    this._render();
    if (view && !this._view.disabled) {
      this._mine = true;
      this._call("call", this._view).catch(() => {});
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
    this._fit = $("#fit");
    this._photo = $("#photo");
    this._pic = $("#photo img");
    this._still = $(".still");
    this._hist = $(".hist");
    this._set = $("dialog.set");
    this._cfgs = [$("#cfg"), $("#cfgc")];
    this._empty = $(".empty span");
    this._dlg = $("dialog.photo");
    this._pop = $("dialog.pop");
    this._clipEl = $("dialog.photo video");
    // Tocco ovunque (o Esc) chiude, tranne sui controlli del video.
    this._dlg.onclick = (e) => e.target !== this._clipEl && this._dlg.close();
    this._dlg.onclose = () => { this._clipEl.pause(); this._clipEl.removeAttribute("src"); this._clipEl.load(); };
    // Popup chiuso (X, Esc, fuori): la card torna al suo posto, audio chiuso, riaggancio solo se la chiamata è della card.
    this._pop.onclick = (e) => e.target === this._pop && this._pop.close();
    this._pop.onclose = () => {
      lockPageScroll(false);
      this._card.classList.remove("pop");
      this._root.insertBefore(this._card, this._pop);
      this._armCard();
      this._stopAudio();
      this._stopListen();
      const mine = this._mine;
      this._mine = false;
      if (mine && ["calling", "in_call"].includes(this._state)) this._call("hangup", this._hangup).catch(() => {});
      this._render();
    };
    this._err = $(".err");
    this._hint = "";  // niente avviso permanente: in HTTP il microfono è semplicemente spento
    this._view = $("#view");
    this._view.setAttribute("aria-label", "Vedi esterno");
    this._talk = $("#talk");
    this._hangup = $("#hangup");
    this._open = $("#open");
    this._mute = $("#mute");
    this._videoBox = $("#video");
    // The box changes shape with the layout, the popup and the phone's rotation.
    if (window.ResizeObserver) new ResizeObserver(() => this._fitShape()).observe(this._videoBox);
    this._applyCfg();
    this._view.className = "fill";
    this._open.setAttribute("aria-label", "Apri portone, tocca due volte");
    this._sc = $(".sc");
    for (const b of this._cfgs) b.onclick = (e) => { e.stopPropagation(); this._openSettings(); };
    this._set.onclick = (e) => e.target === this._set && this._set.close();
    this._histBtn = $("#hist");
    if (!window.isSecureContext) this._talk.title = "Per parlare serve Home Assistant in HTTPS.";
    for (const b of [this._log, this._photo, this._histBtn]) b.onclick = (e) => {
      if (this._popup && !this._pop.open) { e.stopPropagation(); this._setDrawer(true); return this._openPop(); }  // cronologia: mai la targa
      this._setDrawer(this._card.dataset.drawer !== "true");
    };
    this._card.onclick = (e) => this._popup && !this._pop.open && !e.target.closest("button") && this._openPop(!this._preview);  // anteprima dell'editor: niente Vedi esterno
    this._card.onkeydown = (e) => e.target === this._card && (e.key === "Enter" || e.key === " ") && (e.preventDefault(), this._card.click());
    this._root.getElementById("x").onclick = () => this._pop.close();
    this._view.onclick = () => {
      if (this._popup && !this._pop.open) return this._openPop(!this._preview);  // card compatta: apre il popup e fa "Vedi esterno"
      this._mine = true;
      this._call("call", this._view).catch(() => {});
    };
    this._talk.onclick = () => {
      if (this._popup && !this._pop.open) this._openPop();  // "Rispondi" sulla card compatta: prima il popup
      this._ws ? this._stopAudio() : this._starting || this._startTalk();
    };
    this._hangup.onclick = () => {  // allo squillo "Rifiuta" (smette di suonare in tutta la casa), poi "Riaggancia"
      if (this._state === "ringing") this._call("decline", this._hangup).catch(() => {});
      else if (LIVE.includes(this._state)) this._call("hangup", this._hangup).catch(() => {});
      if (this._pop.open) {
        this._mine = false;  // già fatto qui: la chiusura non riaggancia di nuovo
        this._pop.close();
      }
    };
    this._open.onclick = () => this._openDoor();
    // Non ancora in ascolto (bloccato su iOS o mai partito): il tocco stesso è il gesto
    // vero che sblocca l'AudioContext, quindi avvia l'ascolto invece di mutare un canale
    // che non c'è ancora. Se il parlato vero è già in corso, non lo tocca: solo il muto.
    this._mute.onclick = () => {
      if (this._ws || this._listenWs) this._toggleMute();
      else if (!this._listenStarting) this._startListen();
    };
    // Adatta (contain, predefinito) / Riempi (cover): la scelta resta sul dispositivo.
    try { this._cover = localStorage.getItem(FIT_KEY) === "cover"; } catch { this._cover = false; }
    this._fit.onclick = () => {
      this._cover = !this._cover;
      try { localStorage.setItem(FIT_KEY, this._cover ? "cover" : "contain"); } catch { /* storage bloccato: vale per questa sessione */ }
      this._applyFit();
      // HA's picture-entity card (no WebCodecs, or the player failed) keeps its <video> in its
      // own shadow DOM, out of reach of our object-fit: it takes fit_mode, so it is rebuilt (#42).
      if (this._video && !this._player) this._setPicture(this._live);
    };
    this._applyFit();
    // Un avviso vive SAY_MS al posto della riga di stato, poi sparisce (NO_ANSWER resta finché si collega).
    new MutationObserver(() => {
      clearTimeout(this._sayT);
      const t = this._err.textContent;
      if (t && t !== NO_ANSWER) this._sayT = setTimeout(() => { if (this._err.textContent === t) this._err.textContent = ""; }, SAY_MS);
    }).observe(this._err, { childList: true, characterData: true, subtree: true });
  }

  // Le entità delle impostazioni, dall'attributo `card_entities` della camera (chiavi dnd, segreteria, delay, file, text);
  // se una manca, la sua riga non compare.
  _setIds() {
    const ids = {};
    for (const [k, key] of [["dnd", "dnd"], ["vm", "segreteria"], ["delay", "delay"], ["file", "file"], ["text", "text"]]) {
      const id = this._ent(key);
      if (id && this._hass.states[id]) ids[k] = id;
    }
    if (!this._hass.user?.is_admin) delete ids.file, delete ids.text;  // testo e file audio: solo admin
    return ids;
  }

  _syncSettings() {
    const ids = this._setIds(), any = Object.keys(ids).length > 0;
    for (const b of this._cfgs) b.hidden = !any;
    if (this._set.open) this._fillSettings(ids);
  }

  _openSettings() {
    const ids = this._setIds();
    if (!Object.keys(ids).length || this._set.open) return;
    const h = document.createElement("template");
    h.innerHTML = `<div class="set-c"><div class="set-h"><h2>Impostazioni citofono</h2><button class="set-x" aria-label="Chiudi"><ha-icon icon="mdi:close" aria-hidden="true"></ha-icon></button></div>
      <div class="set-body"></div><p class="set-e" role="alert"></p></div>`;
    this._set.replaceChildren(h.content);
    this._set.querySelector(".set-x").onclick = () => this._set.close();
    this._setBuilt = null;
    this._fillSettings(ids);
    this._set.showModal();
  }

  // Le righe si costruiscono una volta per elenco di entità; poi si aggiornano soltanto i valori (mai sotto le dita di chi scrive).
  _fillSettings(ids) {
    const st = (id) => this._hass.states[id], body = this._set.querySelector(".set-body");
    const key = Object.values(ids).join();
    const call = (dom, sv, data) => this._hass.callService(dom, sv, data).then(() => { this._set.querySelector(".set-e").textContent = ""; },
      (e) => { this._set.querySelector(".set-e").textContent = `Non riuscito: ${e.message || e}`; });
    const row = (k, label, control, col) => {
      const r = document.createElement("div");
      r.className = "set-r" + (col ? " col" : "");
      r.dataset.k = k;
      const l = document.createElement("span");
      l.className = "set-l";
      l.append(label);
      r.append(l, control);
      return r;
    };
    if (this._setBuilt !== key) {
      this._setBuilt = key;
      const rows = [];
      const toggle = (k, label) => {
        const b = document.createElement("button");
        b.className = "set-tg";
        b.setAttribute("role", "switch");
        b.setAttribute("aria-label", label);
        b.onclick = () => call("switch", b.getAttribute("aria-checked") === "true" ? "turn_off" : "turn_on", { entity_id: ids[k] });
        rows.push(row(k, label, b));
      };
      const select = (k, label, col) => {
        const el = document.createElement("select");
        el.className = "set-in";
        el.setAttribute("aria-label", label);
        el.onchange = () => call("select", "select_option", { entity_id: ids[k], option: el.value });
        rows.push(row(k, label, el, col));
      };
      if (ids.dnd) toggle("dnd", "Non disturbare");
      if (ids.vm) toggle("vm", "Segreteria");
      if (ids.delay) select("delay", "Ritardo segreteria");
      if (ids.text) {
        const el = document.createElement("input");
        el.className = "set-in";
        el.type = "text";
        el.setAttribute("aria-label", "Testo del messaggio");
        el.onchange = () => call("text", "set_value", { entity_id: ids.text, value: el.value });
        rows.push(row("text", "Testo del messaggio", el, true));
      }
      if (ids.file) select("file", "File audio del messaggio", true);
      body.replaceChildren(...rows);
    }
    for (const r of body.children) {
      const k = r.dataset.k, s = st(ids[k]), ctl = r.querySelector("button, select, input"), off = !s || s.state === "unavailable";
      ctl.disabled = off;
      if (ctl.matches("button")) ctl.setAttribute("aria-checked", s?.state === "on");
      else if (ctl.matches("select")) {
        const opts = s?.attributes?.options || [];
        if (ctl.options.length !== opts.length || opts.some((o, i) => ctl.options[i].value !== o)) ctl.replaceChildren(...opts.map((o) => new Option(o, o)));
        if (this._root.activeElement !== ctl) ctl.value = s?.state;
      } else if (this._root.activeElement !== ctl) ctl.value = s && s.state !== "unknown" ? s.state : "";
      if (k === "vm") {  // da dove viene il messaggio: dall'attributo `modo` dello switch, se c'è
        const modo = s?.attributes?.modo, small = r.querySelector("small") || r.querySelector(".set-l").appendChild(document.createElement("small"));
        small.textContent = modo === "Home Assistant" ? "Messaggio di Home Assistant" : modo === "Tab" ? "Segreteria del Tab" : "";
      }
    }
  }

  // Fill and fit differ only when the video and its box have different shapes: a 4:3 panel in the
  // default 4:3 box looks the same either way, and the button would seem to do nothing (#42).
  // Known only for our own canvas; with HA's card the button stays.
  _fitShape() {
    const c = this._player?.canvas, box = this._videoBox;
    const w = c?.width, h = c?.height, bw = box?.clientWidth, bh = box?.clientHeight;
    const same = !!(w && h && bw && bh) && Math.abs(w / h - bw / bh) < 0.02 * (w / h);
    this._card?.toggleAttribute("data-fit-same", same);
  }

  _applyFit() {
    this._card.dataset.fit = this._cover ? "cover" : "contain";
    const label = this._cover ? "Adatta video" : "Riempi schermo";  // il tasto dice cosa farà
    this._fit.setAttribute("aria-label", label);
    this._fit.title = label;
    this._fit.setAttribute("aria-pressed", this._cover);
    this._icon(this._fit, this._cover ? "mdi:fit-to-screen-outline" : "mdi:arrow-expand-all");
  }

  _setDrawer(open) {
    this._card.dataset.drawer = open;
    for (const b of [this._log, this._photo]) b.setAttribute("aria-expanded", open);
    this._icon(this._log, "mdi:history");  // aperto o chiuso: lo dice lo sfondo del tasto
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
      this._photo.disabled = this._histBtn.disabled = !rings.length;
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
    // Card compatta che suona: "Suonano alla porta" nel nome e "Tocca per vedere · mm:ss" nello stato.
    if (this._state !== "idle") this._pendingAt = 0;
    const pending = this._state === "idle" && Date.now() - (this._pendingAt || 0) < PENDING_MS;
    this._card.dataset.pending = pending;
    const ring = this._state === "ringing", compactRing = ring && this._popup && !this._pop.open;
    if (ring && !this._ringAt) {
      this._ringAt = Date.now();
      this._ringTimer = setInterval(() => this._tickSetup(), 1000);
    } else if (!ring && this._ringAt) {
      clearInterval(this._ringTimer);
      this._ringAt = null;
    }
    const r = Math.floor((Date.now() - (this._ringAt || Date.now())) / 1000), two = (n) => String(n).padStart(2, "0");
    this._root.querySelector(".name").textContent = compactRing ? LABEL.ringing : this._cfg.name;
    this._pill.textContent = calling ? `${LABEL.calling} ${s} s`
      : pending ? LABEL.calling
      : compactRing ? `Tocca per vedere · ${two(Math.floor(r / 60))}:${two(r % 60)}` : LABEL[this._state];
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
    this._fitShape();
    if (!live || !NalPlayer.ok()) return this._setPicture(live);
    const canvas = document.createElement("canvas");
    this._player = new NalPlayer(this._hass, canvas, () => {
      this._player = null;
      this._fitShape();
      if (this._live) this._setPicture(true);
    });
    this._player.onsize = () => this._fitShape();
    this._videoBox.replaceChildren(canvas);
    this._video = null;
  }

  // La card standard di HA (picture-entity). "live" apre lo stream, che da fermo
  // farebbe chiamare la targa: lo si usa solo a chiamata o squillo in corso.
  async _setPicture(live) {
    this._helpers ||= window.loadCardHelpers();
    const el = (await this._helpers).createCardElement({
      type: "picture-entity", entity: this._ent("camera"), camera_view: live ? "live" : "auto",
      fit_mode: this._cover ? "cover" : "contain",
      show_name: false, show_state: false, tap_action: { action: "none" }, hold_action: { action: "none" },
    });
    if (this._live !== live) return;  // stato cambiato nel frattempo
    el.hass = this._hass;
    this._videoBox.replaceChildren(el);
    this._video = el;
  }

  // Doppio tocco (confirm_open, default): il primo arma per 3 s, il secondo apre. La pressione lunga su iOS
  // litiga con VoiceOver e col menu contestuale; confirm() si conferma di riflesso.
  // Scorciatoie (config `shortcuts`, default la serratura): [{ entity, name, icon }], con dominio lock o button.
  _list() {
    const raw = this._cfg.shortcuts ?? [this._ent("lock")];
    return raw.map((s) => (typeof s === "string" ? { entity: s } : s)).filter((s) => SC[s.entity?.split(".")[0]]);
  }

  // Tasti tondi nella card compatta; si rifanno solo se cambia l'elenco (nome/icona dallo stato).
  _shortcuts() {
    const list = this._list().map((s) => {
      const st = this._hass.states[s.entity], dom = s.entity.split(".")[0];
      return { ...s, name: s.name || (dom === "lock" ? "Apri" : st?.attributes?.friendly_name || s.entity),
        icon: s.icon || st?.attributes?.icon || SC[dom][1], off: st?.state === "unavailable" };
    });
    const key = JSON.stringify(list);
    if (key === this._scKey) return;
    this._scKey = key;
    this._sc.replaceChildren(...list.map((s) => {
      const t = document.createElement("template");
      t.innerHTML = btn("", s.icon, s.name);
      const b = t.content.firstElementChild;
      b.disabled = s.off;
      b.onclick = (e) => { e.stopPropagation(); this._openDoor(b, s.entity); };  // mai il popup né la targa
      return b;
    }));
  }

  // `b`: il tasto che si arma / si conferma (Apri o una scorciatoia), `entity`: la prima scorciatoia se non detto.
  async _openDoor(b = this._open, entity = this._list()[0]?.entity || this._ent("lock")) {
    if (this._cfg.confirm_open && this._armed !== b) {
      this._resetOpen();
      this._armed = this._flash = b;
      this._openTimer = setTimeout(() => this._resetOpen(), 3000);
      b.className = "warn";
      this._label(b, "Tocca ancora");
      return;
    }
    this._resetOpen();
    this._err.textContent = this._hint;
    this._flash = b;
    b.className = "busy";  // subito: il tondo gira mentre il servizio lavora
    try {
      await this._hass.callService(entity.split(".")[0], SC[entity.split(".")[0]][0], { entity_id: entity });
      b.className = "ok";
      this._icon(b, "mdi:check");
      this._label(b, "Aperto");
    } catch (e) {
      this._err.textContent = `Apertura non riuscita: ${e.message || e}`;
      b.className = "bad";
      this._icon(b, "mdi:alert-circle-outline");
      this._label(b, "Errore");
    }
    this._openTimer = setTimeout(() => this._resetOpen(), OPEN_FLASH_MS);
  }

  _resetOpen() {
    clearTimeout(this._openTimer);
    this._armed = null;
    const b = this._flash;
    this._flash = null;
    if (!b) return;
    b.className = "";
    this._icon(b, b.dataset.icon);
    this._label(b, b.dataset.label);
  }

  async _call(service, button) {
    if (service === "hangup") {
      this._stopAudio();
      this._cancelled = this._state === "calling";  // "Annulla": non è un rifiuto della targa
    }
    this._err.textContent = this._hint;
    button.classList.add("busy");
    button.disabled = true;
    if (service === "call") {  // feedback al tocco: "Collegamento…" finché lo stato non cambia (o fallisce)
      this._pendingAt = Date.now();
      this._tickSetup();
      setTimeout(() => this._tickSetup(), PENDING_MS + 50);
    }
    try {
      const r = await this._hass.callService("vimar_intercom", service, {}, undefined, true, true);
      if (r?.response?.ok === false) throw new Error(r.response.result);
    } catch (e) {
      this._err.textContent = `Non riuscito: ${e.message || e}`;
      this._pendingAt = 0;
      this._tickSetup();
      throw e;
    } finally {
      button.classList.remove("busy");
      button.disabled = false;
    }
  }

  // Crea un AudioContext e prova a sbloccarlo (resume): se iOS lo tiene sospeso per
  // mancanza di un gesto vero, lo chiude, segnala _audioBlocked ("Microfono"/"Ascolta"
  // → "Audio") e torna null. Usato da _startTalk (solo per `auto`, senza un tocco
  // davanti) e da _startListen (mai un tocco davanti).
  async _unlockedContext() {
    const ctx = new AudioContext();
    await ctx.resume().catch(() => {});
    if (ctx.state === "running") return ctx;
    ctx.close();
    this._audioBlocked = true;
    this._render();
    return null;
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
      this._mine = true;
      await this._call("answer", this._talk).catch(() => {});
      return;
    }
    this._starting = true;  // doppio tocco durante il permesso: una chiamata sola
    // Il tocco era "Rispondi": se lo squillo finisce mentre iOS chiede il permesso
    // del microfono (ha risposto il Tab), non si chiama la targa al suo posto.
    const answering = this._state === "ringing";
    // Nel gesto, prima di ogni await: creato dopo, Safari/iOS lo lascia sospeso
    // (niente voce del visitatore e onaudioprocess fermo, quindi niente microfono).
    const ctx = auto ? await this._unlockedContext() : new AudioContext();
    if (auto && !ctx) {  // niente gesto vero: non si chiede nemmeno il microfono
      this._starting = false;
      return;
    }
    let mic;
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
      if (this._state === "ringing") {
        this._mine = true;
        await this._call("answer", this._talk);
      } else if (answering && this._state !== "in_call") throw new Error("lo squillo è finito");
      else if (this._state !== "in_call") {
        this._mine = true;
        await this._call("call", this._talk);
      }
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
  // Passa da un GainNode (_gain) così il tasto "Audio" può azzerare solo questa
  // riproduzione: niente riaggancio, niente microfono, WebSocket sempre aperto.
  _pcmSink(ctx, gain) {
    let playAt = 0;
    return (ev) => {
      if (typeof ev.data === "string" || new Uint8Array(ev.data, 0, 1)[0] !== 0x01) return;
      const pcm = new Int16Array(ev.data.slice(1));
      const buf = ctx.createBuffer(1, pcm.length, RATE);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(gain);
      playAt = Math.max(playAt, ctx.currentTime + 0.05);  // piccolo buffer contro gli scatti
      src.start(playAt);
      playAt += buf.duration;
    };
  }

  // Un guadagno per sessione (`key`: _listenGain o _talkGain): lo tocca solo il tasto "Audio",
  // mai il microfono o il WebSocket. Il muto dura finché dura la diretta.
  _gain(ctx, key) {
    const g = (this[key] = ctx.createGain());
    g.gain.value = this._muted ? 0 : 1;
    g.connect(ctx.destination);
    return g;
  }

  _toggleMute() {
    this._muted = !this._muted;
    for (const g of [this._listenGain, this._talkGain]) if (g) g.gain.value = this._muted ? 0 : 1;
    this._render();
  }

  // Ascolto senza rispondere: solo ricezione, niente microfono né "answer"/"call" — un
  // WebSocket audio a sé, separato da quello video (NalPlayer scarta i pacchetti 0x01) e
  // da quello del parlato vero (_openAudio, che lo scavalca: vedi _startTalk). `auto`:
  // richiamato da solo da `_render` quando `listen_on_ring` è acceso (stesso limite iOS
  // del parlato: senza un gesto vero l'AudioContext resta sospeso, si rinuncia in
  // silenzio e il tasto "Audio" spento resta lì pronto al tocco). Senza `auto`: il tasto
  // "Audio" stesso, tocco vero — parte comunque, anche a `listen_on_ring` spento: è
  // l'utente a chiederlo, non l'anteprima automatica.
  async _startListen(auto = false) {
    if (!window.isSecureContext) return;
    this._listenStarting = true;
    try {
      const ctx = await this._unlockedContext();
      if (!ctx) return;
      const { path } = await this._hass.callWS({ type: "auth/sign_path", path: "/api/vimar_intercom/audio_ws" });
      // Stato cambiato nell'attesa: uscito dalla card, o (solo per l'automatico) l'anteprima
      // non serve più (config spenta dall'editor, oppure il parlato vero l'ha scavalcata).
      if (!this.isConnected || this._ws || (auto && !this._cfg.listen_on_ring)) return ctx.close();
      const ws = new WebSocket(location.origin.replace(/^http/, "ws") + path);
      ws.binaryType = "arraybuffer";
      ws.onmessage = this._pcmSink(ctx, this._gain(ctx, "_listenGain"));
      ws.onclose = () => { if (this._listenWs === ws) this._stopListen(); };
      this._listenCtx = ctx;
      this._listenWs = ws;
      this._audioBlocked = false;
      this._render();
    } finally {
      this._listenStarting = false;
    }
  }

  // `_render` la richiama a ogni giro: senza nulla da fermare è un no-op.
  _stopListen() {
    this._listenWs?.close();
    this._listenWs = null;
    this._listenCtx?.close();
    this._listenCtx = null;
    this._listenGain = null;
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
    ws.onmessage = this._pcmSink(ctx, this._gain(ctx, "_talkGain"));  // voce del visitatore
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
    this._talkGain = null;
    if ((a || ws) && this._root) this._render();
  }

  disconnectedCallback() {
    for (const e of ["hashchange", "location-changed"]) window.removeEventListener(e, this._toAnchor);
    if (this._pop.open) this._pop.close();
    if (this._set.open) this._set.close();
    this._stopAudio();
    this._stopListen();
    if (this._player) {  // card tolta dalla pagina: il WS video non resta aperto
      this._player.close();
      this._player = null;
      this._live = undefined;  // al prossimo hass si ricrea
    }
    clearInterval(this._setupTimer);
    clearInterval(this._ringTimer);
    this._ringAt = null;
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
    { value: "popup", label: "Popup al tocco" },
  ] } } },
  { name: "compact_style", selector: { select: { mode: "dropdown", options: [
    { value: "pillola", label: "Pillola" },
    { value: "tile", label: "Tile" },
  ] } } },
  { name: "shortcuts", selector: { entity: { multiple: true, domain: ["lock", "button"] } } },
  { name: "history", selector: { number: { min: 0, max: 50, mode: "box" } } },
  { name: "confirm_open", selector: { boolean: {} } },
  { name: "listen_on_ring", selector: { boolean: {} } },
];
const FIELD = { camera: "Telecamera", name: "Nome", layout: "In diretta", compact_style: "Card compatta (layout popup)", shortcuts: "Scorciatoie Apri sulla card compatta (vuoto = serratura)", history: "Squilli in cronologia (0 = niente)",
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
          if (v === undefined || v === "" || v === DEFAULTS[name] || v?.length === 0) delete config[name]; else config[name] = v;
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
