# Come avere la rubrica

🇬🇧 *[English](PHONEBOOK.md)* · [← README](../README.it.md) · [Configurazione](CONFIGURATION.it.md) · [Problemi e log](TROUBLESHOOTING.it.md)

La rubrica (`rubrica.db`) dice all'integrazione quali attuatori ha l'impianto (porta, F1/F2, luci
scala, relè), quale targa apre la porta, quale targa chiama la camera, e l'**SGA** e il **PICG**: gli
indirizzi a cui vanno segreteria, non disturbare e la richiesta di stato. Senza rubrica l'integrazione
usa dei default giusti su alcuni impianti e sbagliati su altri
([#10](https://github.com/ha-vimar/ha-vimar-intercom/issues/10),
[#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14)).

Non c'è una catena automatica: le tre fonti sono tre voci del menu delle opzioni
(Impostazioni → Dispositivi e servizi → Vimar Intercom → **Configura**) e scegli tu. Provale in
quest'ordine e fermati alla prima che funziona.

## Quale via per il tuo impianto

```text
Home Assistant raggiunge il citofono in rete locale (il suo IP, porta 80)?
├─ sì → 1. «Scarica la rubrica dal citofono»
│       ├─ funziona → fatto
│       └─ «non raggiungibile in HTTP» / «ha rifiutato le credenziali SIP» → vai a 2
└─ no (impianto solo cloud, es. Tab 5S Up 40515) → vai a 2

2. «Scarica la rubrica dal cloud Vimar»
   ├─ funziona → fatto
   └─ «non ha mandato il token della rubrica» (risposta corta al GET_INIT_STATUS, come sul 40507)
      o «il cloud Vimar ha rifiutato il token» → vai a 3

3. «Importa attuatori da rubrica.db» con un file estratto da te
   ├─ hai il file → caricalo, conferma → fatto
   └─ non c'è modo di avere il file → vai a 4

4. A mano: «Impostazioni di rete e attuatori» (attuatori JSON, SGA, PICG, targhe),
   più il servizio find_sga per trovare PICG/SGA
```

## 1. Dal citofono (rete locale)

**Configura → Scarica la rubrica dal citofono.** Home Assistant chiede la rubrica all'interfaccia web
locale del citofono, autenticandosi con le credenziali SIP che ha già dal QR: niente token, niente
account Vimar, niente telefono. Nella stessa occasione legge il PICG dichiarato dal citofono.
Verificato su un Tab 7S (40507).

- Serve l'indirizzo del citofono in *Impostazioni di rete e attuatori*.
- «Non raggiungibile in HTTP»: il Tab non risponde sulla porta 80. I 40515 segnalati finora rifiutano
  la connessione ([#5](https://github.com/ha-vimar/ha-vimar-intercom/issues/5)): usa la 2 o la 3.
- «Ha rifiutato le credenziali SIP, oppure non espone questa API»: il citofono risponde `401` in
  tutti e due i casi, quindi non vuol dire per forza credenziali sbagliate.

## 2. Dal cloud Vimar (token)

**Configura → Scarica la rubrica dal cloud Vimar** (dalla 1.0.12). Gli impianti che rispondono alla
richiesta di stato (`GET_INIT_STATUS`) in forma lunga ci mettono un `token`; con quel token la rubrica
si scarica dal cloud Vimar con una sola richiesta autenticata. Il token si rilegge dall'impianto ogni
volta e non viene salvato. Verificato su un Tab 5S Up 40515 (2FV2) ([#5](https://github.com/ha-vimar/ha-vimar-intercom/issues/5)).

- «Non ha mandato il token della rubrica»: l'impianto risponde in forma corta (il 40507 lo fa). Non
  c'è un token da usare: torna alla 1, o passa alla 3.
- Quanto resta valido un token non si sa ancora.

## 3. Da un file `rubrica.db`

**Configura → Importa attuatori da rubrica.db.** Carica il file SQLite e conferma: gli attuatori del
GID del tuo appartamento sostituiscono l'elenco attuale, e SGA, PICG, targa video e targa che apre la
porta vengono compilati. Un file che non dice nulla del PICG lascia com'è quello configurato. Il file
viene solo letto, non conservato.

Come estrarre il file dall'app VIEW (telefono con root, o l'app nel Windows Subsystem for Android):
[RUBRICA.md](RUBRICA.md) §3 e §3-bis.

## 4. A mano

Se nessuna delle vie sopra funziona, inserisci i valori in **Configura → Impostazioni di rete e
attuatori**:

- **Attuatori (JSON)**: l'elenco che `tools/parse_rubrica.py` stampa da una rubrica. Vuoto = nessun
  pulsante attuatore; la porta si apre lo stesso con il comando generico verso la targa che apre la
  porta ([#58](https://github.com/ha-vimar/ha-vimar-intercom/issues/58)).
- **SGA**, **PICG**, **Targa video**, **Targa che apre la porta**: vedi
  [Configurazione](CONFIGURATION.it.md). SGA/PICG vuoti valgono `55001`, giusto sull'impianto di
  sviluppo e sbagliato su altri.
- Non conosci SGA/PICG? Il servizio **`vimar_intercom.find_sga`** (solo amministratori) interroga una
  serie di indirizzi con `GET_NICKS` e riporta come PICG quello che risponde; con *Applica come PICG* lo scrive
  nelle opzioni ([#14](https://github.com/ha-vimar/ha-vimar-intercom/issues/14)).

## Dopo l'importazione

Segreteria e non disturbare che rispondono `200 OK` ma non cambiano nulla, o un comando porta
accettato che non apre niente, vogliono dire quasi sempre SGA o targa della porta sbagliati: vedi
[Problemi e log](TROUBLESHOOTING.it.md).
