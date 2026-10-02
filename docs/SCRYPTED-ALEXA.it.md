# Echo Show come citofono (Scrypted)

🇬🇧 *[English](SCRYPTED-ALEXA.md)* · [← README](../README.it.md)

Uno script per Scrypted che fa di un Amazon Echo Show un monitor della targa Vimar: video dal vivo,
l'audio della strada sull'Echo e il microfono dell'Echo verso la targa. Provato su Home Assistant con
un Tab 5S Up (40515), Scrypted e un Echo Show.

## Cosa fa

- **Senza squillo**: apri la camera sull'Echo ("Alexa, mostra il citofono") e parte la chiamata
  alla targa: video e conversazione nei due sensi. Chiudi l'Echo e la chiamata si chiude.
- **Durante uno squillo**: l'annuncio del campanello apre la camera sull'Echo da solo. L'Echo
  **guarda e basta**, in silenzio: non risponde e gli altri telefoni continuano a suonare. Risponde
  solo se qualcuno parla all'Echo (circa 200 ms di voce di fila).
- **Chiamata già in corso** (card, app): l'Echo si aggancia e alla chiusura non la chiude.
- Lo script **non apre mai la porta**: le uniche azioni che manda sono `call`, `answer` e `hangup`.

## Cosa serve

- L'integrazione alla versione **1.0.19 o successiva** (correzioni allo stream passivo, vedi
  [#88](https://github.com/lollox80/ha-vimar-intercom/pull/88)).
  <!-- segnaposto: aggiornare con la release vera -->
- Scrypted con i plugin **Scripts**, **Rebroadcast** e **Alexa**.
- Lo squillo che arriva a Scrypted come pressione del campanello: il *Doorbell Button* (Dummy
  Switch) e i webhook dello squillo dell'integrazione, come ai
  [passi 2 e 3 di External systems](EXTERNAL.md#scrypted-alexa-chime--echo-show-live-view) (in inglese).
  Senza, funziona tutto tranne l'Echo che si apre da solo quando suonano.
- Un **token di accesso a lunga durata** di Home Assistant: il tuo profilo → *Sicurezza* → *Token
  di accesso a lunga durata* → *Crea token*. Se nell'integrazione è impostata l'opzione *Utenti
  ammessi a squilli e media live*, l'utente del token deve esserci.

## Installazione

1. **Dispositivo script.** In Scrypted: plugin *Scripts* → *Create Script*, con un nome (per
   esempio "Citofono").
2. **Codice.** Incolla tutto [`scrypted/vimar-intercom-alexa.ts`](scrypted/vimar-intercom-alexa.ts)
   al posto del modello.
3. **Costanti**, nelle prime tre righe:
   - `TOKEN`: il token a lunga durata;
   - `HA`: Home Assistant in LAN, in `http` semplice (per esempio `http://192.168.1.10:8123`);
   - `AV_KEY`: la [chiave di /av](EXTERNAL.md#the-av-key), per ora facoltativa: lascia `''` se non la usi.
4. **Save, poi Run.** Tutti e due. La versione precedente resta in esecuzione finché non premi
   *Run*: è il motivo più comune per cui una modifica "non fa niente".
5. **Audio sull'Echo.** Sul nuovo dispositivo: impostazioni di *Rebroadcast* (prebuffer) → *FFmpeg
   Output Prefix*:
   ```
   -vcodec copy -acodec libopus -ar 48000 -ac 1 -b:a 32k
   ```
   Lo stream porta AAC dentro MPEG-TS, che non si può copiare così com'è in RTSP/WebRTC: senza
   questa riga l'Echo ha il video ma non il suono.
6. **Campanello.** Attiva l'estensione *Doorbell Button* su questo dispositivo e punta lì i webhook
   dell'integrazione (vedi Cosa serve).
7. **Alexa.** Tra le estensioni del dispositivo attiva *Alexa*. Nell'app Alexa: *Dispositivi* →
   *+* → *Aggiungi dispositivo* → ricerca; il citofono compare come videocampanello. Nelle sue
   impostazioni accendi gli **annunci di pressione del campanello** (e la notifica, se la vuoi anche
   sul telefono).
8. Facoltativo, non misurato: nelle impostazioni del plugin Alexa, spegnere *Use TURN Servers* può
   far partire il video prima se tutto sta in LAN.

Prova: con nessuno alla porta, di' "Alexa, mostra il citofono". In pochi secondi la targa si deve
accendere e devi sentire la strada.

## Come funziona

- **Video**: `getVideoStream` legge lo stream passivo continuo
  `/api/vimar_intercom/av?autocall=0&idle=image` ([dettagli](EXTERNAL.md#the-one-rule-use-the-passive-url), in inglese):
  immagine di standby a riposo, la targa dal vivo durante squillo o chiamata, e non chiama mai da
  solo. L'Echo ha sempre un'immagine, e aprirlo non costa nulla.
- **Voce**: Alexa chiama `startIntercom` quando l'Echo apre la camera e `stopIntercom` quando la
  chiude. Lo script apre `/api/vimar_intercom/audio_ws` con `Authorization: Bearer <TOKEN>` e
  guarda il primo messaggio `state`:
  - `in_call`: si aggancia;
  - `ringing` vero: guarda, e manda `{"action": "answer"}` solo dopo ~200 ms di voce;
  - altrimenti: `{"action": "call"}`.

  Le versioni dell'integrazione senza il campo `ringing` (prima della 1.0.19) sono trattate come
  squillo, così l'Echo non chiama mai sopra uno squillo; con quelle versioni, aperto senza squillo,
  mostra solo lo standby.
- **Microfono**: l'audio dell'Echo, convertito da ffmpeg in PCM16LE 8 kHz mono, parte come `0x02`
  + un pacchetto da 20 ms (320 byte). Home Assistant lo gira alla targa solo durante una chiamata.
- **Riaggancio**: su `stopIntercom` manda `{"action": "hangup"}` solo se la chiamata l'ha aperta o
  presa l'Echo.

## Problemi

| Sintomo | Causa e rimedio |
|---|---|
| Dopo una chiamata l'Echo resta fermo sull'ultimo fotogramma della strada | Corretto nell'integrazione (1.0.19 o successiva). |
| Video a scatti o in ritardo di secondi | Corretto nella stessa versione (ordine delle opzioni dello stream, fps del decoder). Aggiorna. |
| Video ma niente audio sull'Echo | *FFmpeg Output Prefix* mancante o scritto male (passo 5). |
| L'Echo si apre ma non chiama, o fa come prima | Gira ancora la versione vecchia dello script: *Save* e *Run*. Nel log Scrypted del dispositivo, un comando ffmpeg con `-an` vuol dire script vecchio. |
| `audio_ws: error` nel log, 403 Forbidden da HA | Token sbagliato o revocato, oppure l'IP di Scrypted è bannato dopo accessi falliti: toglilo da `ip_bans.yaml` nella cartella di configurazione di HA e riavvia HA. |
| Connessione chiusa con "Not allowed" | L'utente del token non è tra gli *Utenti ammessi a squilli e media live* dell'integrazione. |
| Nelle impostazioni del dispositivo non c'è il campo del token | È normale: il token è la costante `TOKEN` in cima allo script. |
| I rumori di casa rispondono allo squillo | Alza `VOICE_RMS` (800 di default) nello script, poi *Save* e *Run*. |

## Togliere una camera vecchia

Se esiste già una camera per il citofono (FFmpeg Camera, uno script precedente), toglila **prima
dall'app Alexa** e solo dopo cancellala in Scrypted. Al contrario, in Alexa resta un dispositivo
morto che può ancora ricevere l'annuncio del campanello.
