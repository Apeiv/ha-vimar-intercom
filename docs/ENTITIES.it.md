# Entità, servizi e automazioni

🇬🇧 *[English](ENTITIES.md)* · [← README](../README.it.md)

## Entità

| Entità | Piattaforma | Descrizione |
|---|---|---|
| Intercom (Videocitofono) | `camera` | Video **on‑demand** (stream): aprendolo parte la chiamata SIP (non a una riconnessione entro 5 s dall'uscita dell'ultimo spettatore, per 60 s dalla fine di una chiamata: `/av` risponde 503; `/av` risponde 503 subito anche quando la chiamata che aspettava viene rifiutata o finisce, salvo con il WebSocket dell'app iOS collegato: allora aspetta fino a 25 s la chiamata dell'app); durante uno squillo mostra l'anteprima senza rispondere. Le foto sono istantanee in chiamata o durante lo squillo (fotogrammi completi, puliti), assenti altrimenti: le miniature non fanno mai squillare la targa |
| Doorbell (Campanello) | `event` | Entità `event` (device_class DOORBELL), event_type `ring`, allo squillo (INVITE in arrivo) |
| Serratura | `lock` | Apri porta: il comando dell'attuatore porta della rubrica (`OPEN_2F` se non c'è) → `door_target`; auto‑relock dopo 5 s (nessun feedback fisico) |
| Chiama | `button` | Chiamata SIP verso la targa di default |
| Chiama Video (esterno) / Chiama Casa (interno) | `button` | Chiamata verso `camera_target` / `internal_panel_target` |
| Rispondi / Riaggancia | `button` | Rispondi (200 OK) / termina (BYE) |
| Rifiuta | `button` | Solo mentre suona: rifiuta con `603 Decline`, così smette di suonare tutta la casa, come nell'app. Anche il servizio `vimar_intercom.decline` |
| Apri Porta | `button` | Come la serratura: il comando porta della rubrica (altrimenti `OPEN_2F`) verso `door_target` |
| Squillo di prova | `button` (diagnostica) | Uno squillo completo senza la targa, come `vimar_intercom.simulate_ring` con i 20 s predefiniti: per provare automazioni e notifiche. Non parte durante una chiamata o uno squillo veri |
| *Attuatori dinamici* | `button` | Uno per voce in `options["actuators"]` (F1/F2, luci scala, relè…); invia `MSG` con `Panda: command` |
| Segreteria | `switch` | `VOICEMAIL;ON/OFF` (Panda: blue) verso l'SGA; stato letto dagli annunci del Tab e da `GET_INIT_STATUS`, chiesto dopo ogni comando. Il valore comandato si vede per 10 s al massimo: senza conferma lo stato diventa *sconosciuto* ([#9](https://github.com/lollox80/ha-vimar-intercom/issues/9)) |
| Non Disturbare | `switch` | `DND;ON/OFF` (Panda: blue) verso l'SGA; stesse regole della Segreteria |
| Segreteria · ritardo | `select` | Solo sugli impianti con la risposta lunga di `GET_INIT_STATUS`: `vm_timeout`, uno dei `vm_timeout_values` dichiarati dall'impianto, scritto con `SET_APT_PARAMS` ([#4](https://github.com/lollox80/ha-vimar-intercom/issues/4)). Sugli impianti con la risposta corta non compare |
| Intercom SIP | `binary_sensor` | Registrazione SIP attiva (connectivity) |
| Intercom In Call | `binary_sensor` | Chiamata attiva |
| Intercom Squillo | `binary_sensor` | ON mentre una targa chiama (attr: chiamante) |
| Intercom Chiamata In Uscita | `binary_sensor` | ON mentre HA chiama |
| Intercom Dispositivi | `sensor` | numero di dispositivi visti sull'impianto (telefoni che condividono l'account SIP, targhe); l'attributo `dispositivi` li elenca con l'identificativo mascherato e senza indirizzo, e resta dopo un riavvio. Ogni utente di Home Assistant può leggerlo, nomi dei dispositivi compresi (il nome di un telefono è spesso quello di chi lo usa) |
| Intercom Stato | `sensor` (enum) | offline / idle / ringing / in_call / calling (+ attributi rete; sugli impianti con la risposta lunga anche il `GID` dell'appartamento, `apt_names` e il `media_enc` dichiarato) |
| Intercom Ultimo Chiamante | `sensor` | targa/monitor dell'ultimo squillo |
| Intercom Ultimo Squillo | `sensor` (timestamp) | ora dell'ultimo squillo (attr: chiamante, foto/foto_url e clip/clip_url con `snapshot_dir`) |
| Intercom Squilli | `sensor` (contatore) | squilli dall'avvio |
| Intercom Chiamate | `sensor` (contatore) | chiamate connesse |
| Intercom Durata Ultima Chiamata | `sensor` (s) | durata ultima chiamata |
| Intercom Ultima Apertura | `sensor` (timestamp) | ultima apertura porta (attr: targa, esito, contatore) |
| Intercom Ultimo Comando | `sensor` | esito ultimo `send_command` |
| Intercom Ultimo Messaggio Ricevuto | `sensor` | ultimo SIP MESSAGE dal citofono |


---

## Servizi (`services.yaml`)

| Servizio | Descrizione | Campi |
|---|---|---|
| `vimar_intercom.send_command` | SIP MESSAGE arbitrario (per test). Solo amministratori e automazioni | `body`, `target`, `header_name`, `header_value` |
| `vimar_intercom.call` | Chiamata SIP verso una targa/monitor | `target` |
| `vimar_intercom.answer` | Risponde alla chiamata in arrivo | — |
| `vimar_intercom.hangup` | Termina la chiamata attiva | — |
| `vimar_intercom.open_door` | Comando di apertura. Senza `command`: quello dell'attuatore porta della rubrica per quella targa, altrimenti `OPEN_2F`; un `command` dato deve essere `OPEN` / `OPEN_*`. Senza `target` va a `door_target`. Risposta: `ok`, `result` (`opened`, `busy`, `queued`, `unconfirmed`, `not_registered`, `timeout`, `error`, `send_failed`) e il `code` SIP | `target`, `command` |
| `vimar_intercom.fetch_local` | GET HTTP Digest verso l'interfaccia locale del Tab (home mode). Solo amministratori e automazioni | `path`, `save_as`, `host`, `scheme` |
| `vimar_intercom.find_sga` | Cerca il PICG interrogando una serie di indirizzi ([#14](https://github.com/lollox80/ha-vimar-intercom/issues/14)). Solo amministratori e automazioni | `start`, `end`, `targets`, `probe`, `delay`, `reply_wait`, `sip_timeout`, `apply`, `apply_sga` |
| `vimar_intercom.simulate_ring` | Squillo di prova (admin): uno squillo completo senza la targa e senza traffico SIP. Lo stato passa a squilla (card, sensori), partono l'evento campanello e il webhook di inizio, e dopo `duration` secondi finisce come uno squillo senza risposta (webhook di fine). Non si può rispondere, il messaggio di assenza lo ignora, uno squillo vero lo sostituisce e non finisce nel registro squilli | `duration` (da 1 a 90 s, predefinito 20) |

Esempio (Strumenti per sviluppatori → Azioni):

```yaml
action: vimar_intercom.send_command
data:
  body: OPEN_2F
  target: "55001"
  header_name: Panda
  header_value: command
```

**Trovare SGA/PICG senza la rubrica** (1.0.13, per gli impianti solo cloud dove non funziona né lo
scaricamento in LAN né quello dal cloud): `find_sga` interroga un indirizzo alla volta (default
`55000`–`55010`, al massimo 50) e si ferma alla prima `GET_NICKS_REPLY`; la voce con ruolo `PICG` è la
risposta. Non cambia nulla, salvo `apply` (scrive `picg_target`) e/o `apply_sga` (scrive anche
`sga_target`); poi l'integrazione si ricarica.

```yaml
action: vimar_intercom.find_sga
data:
  start: "55000"
  end: "55010"
response_variable: result
```

Se un impianto lascia `GET_NICKS` senza risposta SIP (segnalato su un 40515 in cloud: `Timeout` dove
`GET_INIT_STATUS` riceveva `200`), usa `probe: get_init_status`: dà i tre esiti puliti, e l'indirizzo la
cui sonda provoca la `GET_INIT_STATUS_REPLY` è il PICG; ma all'SGA vero fa comparire «Configurazione
appartamento modificata» sull'app a ogni invio. `sip_timeout` (default 8 s) limita l'attesa della
risposta SIP di ogni sonda; via relay cloud usa 20, perché il suo `202` può arrivare dopo ~15 s. La
risposta elenca ogni sonda con il suo esito: `absent` (404), `exists` (accettata, nessuna reply),
`queued` (`202`: il relay cloud ha accettato il messaggio ma nessun dispositivo l'ha preso, quindi via
cloud a quell'indirizzo non risponde nessuno), `replied`, `no_response`, `error`; più `picg` e i
nickname dichiarati.


---

## Eventi

Il campanello è esposto come **entità `event`** (`event.<...>_doorbell`, event_type `ring`), non come
evento sul bus. Nelle automazioni usa un trigger di stato sull'entità `event` (o sul binary_sensor squillo).

In più, dai `MESSAGE` in arrivo dal Tab, l'integrazione emette sul bus HA:
`vimar_intercom_missed_call`, `vimar_intercom_videomessage`, `vimar_intercom_fuoriporta`,
`vimar_intercom_call_info`, `vimar_intercom_phonebook_changed`. Solo lettura, nessun comando in
uscita. Trigger di esempio:

```yaml
automation:
  - alias: "Citofono - Chiamata persa"
    trigger:
      - platform: event
        event_type: vimar_intercom_missed_call
    action:
      - service: notify.mobile_app_telefono
        data: { message: "Chiamata persa al citofono" }
```


---

## Automazioni di esempio

Il file [`packages/vimar_intercom.yaml`](../packages/vimar_intercom.yaml) (da copiare in `config/packages/`) contiene un'automazione
"squillo → notifica" basata sul cambio di stato dell'entità `event`, più un riaggancio di sicurezza
che chiude una chiamata rimasta aperta per due minuti:

```yaml
automation:
  - alias: "Citofono - Squillo → notifica"
    trigger:
      - platform: state
        entity_id: event.vimar_intercom_doorbell
    action:
      - action: notify.mobile_app_IL_TUO_TELEFONO
        data:
          title: "🔔 Qualcuno al citofono"
          message: "Squillo delle {{ now().strftime('%H:%M:%S') }}."
          data:
            image: "/api/camera_proxy/camera.vimar_intercom_intercom"
```

### Togliere la notifica di squillo dagli altri telefoni

Con la notifica di squillo su più telefoni, dopo che qualcuno ha risposto gli altri continuano a mostrarla.
Mandala con un `tag` fisso e cancellala con lo stesso `tag` quando il sensore binario *Squillo* torna spento:
qualcuno ha risposto (dalla card, da un telefono o dal Tab), ha rifiutato, oppure lo squillo è finito.

```yaml
automation:
  - alias: "Citofono - Squillo → notifica"
    trigger:
      - platform: state
        entity_id: event.vimar_intercom_doorbell
    action:
      - action: notify.TUTTI_I_TELEFONI  # un gruppo notify, oppure un'azione per telefono
        data:
          title: "🔔 Qualcuno al citofono"
          message: "Squillo delle {{ now().strftime('%H:%M:%S') }}."
          data:
            tag: squillo-citofono
            image: "/api/camera_proxy/camera.vimar_intercom_intercom"

  - alias: "Citofono - Fine squillo → togli la notifica"
    trigger:
      - platform: state
        entity_id: binary_sensor.vimar_intercom_intercom_squillo  # il tuo sensore Squillo
        from: "on"
        to: "off"
    action:
      - action: notify.TUTTI_I_TELEFONI
        data:
          message: clear_notification
          data:
            tag: squillo-citofono
```

`tag` e `clear_notification` sono funzioni dell'app Companion di Home Assistant (Android e iOS). L'entity id
del sensore binario dipende dalla lingua che aveva Home Assistant quando hai aggiunto l'integrazione:
controllalo nella pagina del dispositivo. Provala con il pulsante *Squillo di prova*.

`camera.snapshot` funziona durante una chiamata o uno squillo. Per avere la foto di ogni visitatore non
serve un'automazione: imposta **Cartella foto squillo** nelle opzioni. Prova le tue automazioni con
`vimar_intercom.simulate_ring`. Non puntare una `camera: platform: ffmpeg` su `/api/vimar_intercom/av`:
blocca Home Assistant finché la sonda di ffmpeg non scade.

**Scrypted (campanello Alexa, Echo Show), go2rtc, Frigate**: usa
`/api/vimar_intercom/av?autocall=0&idle=image`, uno stream continuo che non chiama mai la targa
(immagine di standby a riposo, video dal vivo durante squilli e chiamate; `?autocall=0` da solo
risponde invece 503 a riposo), e inoltra l'`event` del campanello con un'automazione. Istruzioni
in [[EXTERNAL.md](EXTERNAL.md)](EXTERNAL.md) (in inglese).

In [lovelace_example.yaml](lovelace_example.yaml) c'è una card Lovelace di base con i pulsanti rispondi / apri porta /
riaggancia. Il riquadro del video mostra il video dal vivo durante una chiamata o uno squillo. Per
parlare usa [la card del citofono](CARD.it.md).
