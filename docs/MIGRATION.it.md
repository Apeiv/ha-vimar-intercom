# Arrivi da noiseheroes-lab/ha-vimar-intercom

🇬🇧 *[English](MIGRATION.md)* · [← README](../README.it.md)

L'integrazione originale [noiseheroes-lab/ha-vimar-intercom](https://github.com/noiseheroes-lab/ha-vimar-intercom)
è archiviata e rimanda qui. Questa pagina serve a spostare un'installazione esistente.

## Stesso dominio, quindi una alla volta

Le due integrazioni usano lo stesso dominio, `vimar_intercom`, e si installano nella stessa cartella,
`custom_components/vimar_intercom`. Non possono convivere: installare questa sopra la vecchia
cartella la sostituisce. Anche la voce di configurazione non passa da una all'altra: quella vecchia
è alla versione 2, questa integrazione è alla versione 1 e non ha una migrazione per lei, quindi se
resta lì si ferma su *Errore di migrazione*. La strada è: togli la vecchia, installa questa,
aggiungila di nuovo.

## Prima di cominciare

Segnati dove usi la vecchia integrazione: automazioni, script e dashboard. Gli entity id, i servizi
e gli eventi qui sono diversi (vedi [Cosa cambia](#cosa-cambia)), quindi andranno ritoccati dopo.

## Passo per passo

1. **Elimina la vecchia voce mentre il vecchio codice è ancora installato.** Impostazioni →
   Dispositivi e servizi → Vimar Intercom → ⋮ → *Elimina*. In questo ordine la vecchia integrazione
   cancella da sola i propri dati salvati (la rubrica dell'impianto e il registro chiamate in
   `.storage/`).
2. **Togli il vecchio codice.** In HACS apri il vecchio *Vimar Intercom* (quello di `noiseheroes-lab`),
   ⋮ → *Rimuovi*, poi in HACS → ⋮ → *Repository personalizzati* elimina
   `https://github.com/noiseheroes-lab/ha-vimar-intercom`. Installata a mano? Cancella
   `config/custom_components/vimar_intercom`.
3. **Riavvia Home Assistant.**
4. **Installa questa integrazione** come nel [README](../README.it.md#installazione) e riavvia di nuovo.
5. **Aggiungila:** Impostazioni → Dispositivi e servizi → *Aggiungi integrazione* → *Vimar Intercom*.
   Puoi riusare lo stesso slot di abbinamento del posto interno: il QR è lo stesso. Questa
   integrazione vuole il **testo** del QR, non una foto, quindi leggi il codice con una qualsiasi app
   per QR e incolla quello che mostra.
6. **Scarica la rubrica:** *Configura* → dal citofono, dal cloud Vimar o da un file `rubrica.db`. La
   vecchia integrazione la prendeva da sola; qui è un passo nelle opzioni, e imposta la porta, gli
   attuatori e dove vanno i comandi.
7. Ricarica la scheda del browser o l'app perché prenda la nuova card, poi sistema automazioni e
   dashboard della tua lista.

## Cosa cambia

- **Entità:** per Home Assistant sono nuove (i loro unique id cominciano con l'id della nuova voce),
  con un nuovo dispositivo. Nomi, aree e icone che avevi impostato sulle vecchie non passano, e
  nemmeno la loro cronologia. I nuovi id sono in [Entità](ENTITIES.it.md).
- **Servizi:** i vecchi `play_video_message`, `mark_video_message_read`, `delete_video_message`,
  `delete_all_video_messages` e `clear_missed_calls` qui non esistono. Questa integrazione ha
  `call`, `answer`, `decline`, `hangup`, `open_door` e qualche altro, elencati in
  [Entità, servizi e automazioni](ENTITIES.it.md#servizi-servicesyaml).
- **Eventi:** l'evento sul bus `vimar_intercom_ring` non c'è. Il campanello è un'entità `event`: usa
  come trigger il suo stato, o il sensore binario dello squillo. `vimar_intercom_missed_call` esiste
  ma con un payload diverso, e i videomessaggi generano `vimar_intercom_videomessage` (senza
  trattino basso fra le due parole). Dettagli in [Eventi](ENTITIES.it.md#eventi).
- **Card della dashboard:** il tipo è sempre `custom:vimar-intercom-card` e si carica ancora da sola,
  senza niente da aggiungere in Risorse. Le opzioni però sono diverse (`title`, `device_id`,
  `show_actuators` e `hidden_entities` vengono ignorate), quindi la cosa più semplice è eliminare la
  vecchia card e aggiungere di nuovo *Citofono Vimar* dal selettore delle card. Vedi
  [La card del citofono](CARD.it.md).

## Cosa resta

Seguendo l'ordine qui sopra, della vecchia integrazione non resta niente. Se il codice è stato
cambiato prima di eliminare la voce, in `config/.storage/` possono restare due file piccoli:
`vimar_intercom.<id vecchia voce>.plant` e `vimar_intercom.<id vecchia voce>.call_log`. Non servono
più e si possono cancellare a Home Assistant fermo. Resta installato anche il pacchetto Python
`pyzbar` che chiedeva la vecchia integrazione; non dà fastidio.
