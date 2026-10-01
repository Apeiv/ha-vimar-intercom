# Problemi e log

🇬🇧 *[English](TROUBLESHOOTING.md)* · [← README](../README.it.md)

## Limiti noti

- **Audio bidirezionale solo dalla card del citofono**: il lettore video di HA non ha microfono, quindi
  rispondere da un pulsante, da una notifica o da Alexa prende la chiamata in silenzio. Per parlare
  usa `custom:vimar-intercom-card`, in HTTPS.
- **Anteprima allo squillo**: serve che l'impianto mandi early media (verificato su un Tab 5S Up
  40515 via cloud); altrimenti anteprima e foto dello squillo restano vuote finché qualcuno non
  risponde.
- **Impianti solo‑cloud** (es. Tab 5S Up 40515): il Tab risponde `503 You're not allowed` a qualsiasi
  richiesta SIP in LAN, quindi la modalità UDP locale lì non può funzionare; usa il cloud TLS.
  L'interfaccia HTTP locale del Tab (porta 80) rifiuta la connessione sui 40515 segnalati finora, quindi non c'è rubrica da leggere in LAN; camera, attuatori, apri‑porta e i comandi di stato funzionano lo
  stesso via SIP.
- **Segreteria/DND**: si comandano attraverso l'**SGA** (`SYSTEM.MAGIC_APT_INTERCOM` della rubrica,
  `55001` sull'impianto di sviluppo). Inviati a qualunque altro indirizzo vengono ignorati in
  silenzio: azzeccare l'SGA è ciò che li fa funzionare — impostalo in Options o lascialo riempire
  dall'import di `rubrica.db`.
- **Rubrica cloud**: serve un `token`. Gli impianti che rispondono al `GET_INIT_STATUS` in forma lunga
  lo consegnano direttamente, e a quel punto la rubrica si scarica con una sola richiesta autenticata —
  vedi [RUBRICA.md](RUBRICA.md) §0-bis, verificato su un 40515. Dalla 1.0.12 lo fa il menu delle opzioni:
  **"Scarica la rubrica dal cloud Vimar"** ([#5](https://github.com/lollox80/ha-vimar-intercom/issues/5)); il token si rilegge dall'impianto ogni
  volta e non viene salvato. Gli impianti che rispondono in forma corta (compreso quello di sviluppo) non
  hanno il token: lì si usa lo scaricamento dal citofono in LAN, o l'estrazione manuale.
- **Attuatori By‑me** (es. luci scala di domotica By‑me): potrebbero non rispondere via SIP anche se elencati in rubrica.
- **Lock**: nessun feedback fisico di stato (auto‑relock ottimistico dopo 5 s).
- **Squillo durante una nostra chiamata**: mentre Home Assistant chiama la targa o è in chiamata,
  un INVITE in arrivo riceve `486 Busy Here` e non genera l'evento campanello: sul campo non si
  distingue ancora dall'eco della nostra chiamata fatto dal PBX. Uno squillo subito dopo il BYE
  della targa è uno squillo normale.
- **Rubrica**: su impianti solo‑cloud va estratta una tantum (vedi [RUBRICA.md](RUBRICA.md)); l'import automatico via cloud dipende da un token provisionato dall'account.


---

## Logging

Il componente tiene un proprio buffer circolare (`log_buffer.py`, le ultime 3000 righe, `DEBUG`
compreso), leggibile dagli amministratori su `/api/vimar_intercom/debug?lines=N` (100 righe di
default).

Il log di Home Assistant riceve i record del componente dal livello impostato per
`custom_components.vimar_intercom` sotto `logger:` in `configuration.yaml` (o con il servizio
`logger.set_level`) in su, e da `WARNING` in su se nessun livello è impostato. Per vedere tutto:

```yaml
logger:
  logs:
    custom_components.vimar_intercom: debug
```

Entrambe le destinazioni oscurano password, risposte digest, token della rubrica e chiavi SRTP
prima di scrivere. Le risposte attese al keepalive SIP (l'OPTIONS periodico) sono a `DEBUG`: non
serve alcun filtro `logger:` per tenere pulito il log.

