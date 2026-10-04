# Troubleshooting

🇮🇹 *[Italiano](TROUBLESHOOTING.it.md)* · [← README](../README.md)

## Known limitations

- **Two-way audio only in the intercom card**: HA's own camera player has no microphone, so
  answering from a button, a notification or Alexa picks up the call silently. Talk from
  `custom:vimar-intercom-card`, over HTTPS.
- **Ring preview** needs the plant to send early media (verified on a Tab 5S Up 40515 over the
  cloud). If it doesn't, the preview and the ring photo stay empty until someone answers.
- **Cloud-only plants** (e.g. Tab 5S Up 40515): the Tab answers `503 You're not allowed` to any SIP
  request on the LAN, so local UDP mode can't work there; use cloud TLS. The Tab's local HTTP interface (port 80) refuses the connection on the 40515s reported so far, so there is no phonebook to read over the LAN. Camera, actuators, door opening and the state commands still work over SIP.
- **Voicemail / DND**: these are commanded through the **SGA** (`SYSTEM.MAGIC_APT_INTERCOM` in the
  phonebook, `55001` on the plant used for development). Sent to any other address they are silently
  ignored, so getting the SGA right is what makes them work — set it in the options or let the
  `rubrica.db` import fill it in.
- **Cloud phonebook**: needs a `token`. Plants that answer `GET_INIT_STATUS` with the long form hand it
  over directly, and the phonebook can then be downloaded with a single authenticated request — see
  [RUBRICA.md](RUBRICA.md) §0-bis, verified on a 40515. Since 1.0.12 the options menu does it:
  **"Download the phonebook from the Vimar cloud"** ([#5](https://github.com/ha-vimar/ha-vimar-intercom/issues/5)); the token is read from the
  plant each time and never stored. Plants that answer with the short form (including the development
  one) don't carry a token: there use the download from the intercom on the LAN, or the manual extraction.
- **By-me actuators** (e.g. stair lights on By-me home automation): these may not respond over SIP even
  when they are listed in the phonebook.
- **Lock**: no physical state feedback (optimistic auto-relock after 5 s).
- **A ring during one of our own calls**: while Home Assistant is calling the panel or is in a
  call, an incoming INVITE gets `486 Busy Here` and fires no doorbell event: on the field it can't
  yet be told apart from the PBX echoing our own call. A ring right after the panel's BYE is a
  normal ring.
- **Phonebook**: on cloud-only plants it has to be extracted once (see [RUBRICA.md](RUBRICA.md)); the
  automatic import over the cloud depends on a token provisioned by the account.


---

## Logging

The component keeps its own circular buffer (`log_buffer.py`, the last 3000 lines, `DEBUG`
included), readable by administrators at `/api/vimar_intercom/debug?lines=N` (100 lines by default).

The Home Assistant log receives the component's records from the level set for
`custom_components.vimar_intercom` under `logger:` in `configuration.yaml` (or with the
`logger.set_level` service), and `WARNING` and above when no level is set. To see everything there:

```yaml
logger:
  logs:
    custom_components.vimar_intercom: debug
```

Both destinations mask passwords, digest responses, phonebook tokens and SRTP keys before writing.
The SIP keepalive's expected replies (the periodic OPTIONS) are logged at `DEBUG`, so no `logger:`
filter is needed to keep the log quiet.

