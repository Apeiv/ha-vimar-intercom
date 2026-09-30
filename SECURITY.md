# Security policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private vulnerability reporting instead:
[Report a vulnerability](https://github.com/lollox80/ha-vimar-intercom/security/advisories/new)
(or the **Security** tab → **Report a vulnerability**). Only the maintainer sees the report.

Please include, when you can:

- the integration version and the Home Assistant version;
- the intercom model and whether you use local UDP or the Vimar cloud (TLS);
- what an attacker can do and from where (LAN, internet, an authenticated HA user…);
- steps to reproduce, or a proof of concept.

**Never paste secrets** in the report: SIP password, `ha1`, the QR payload, cloud tokens or real
SIP IDs. Replace them with placeholders (for example SIP id `12345`).

## What to expect

This is a hobby project maintained in spare time, so there are no guaranteed deadlines. The aim is to:

- acknowledge the report within a few days;
- agree on the severity and on a fix, and keep you updated in the private advisory;
- release the fix and then publish the advisory, crediting you unless you prefer otherwise.

## Supported versions

Only the **latest release** receives security fixes. Please update through HACS before reporting.

## Scope

In scope: the code in this repository (`custom_components/vimar_intercom`, the Lovelace card and
the tools), for example the HTTP endpoints and WebSocket it exposes, handling of SIP messages
and media, and how credentials are stored and logged. The current safeguards are listed in the
[Security section of the README](README.md#security).

Out of scope: vulnerabilities in the Vimar/Elvox devices, their firmware, the Vimar cloud or the
official apps (report those to Vimar), and in Home Assistant itself (see
[Home Assistant's security page](https://www.home-assistant.io/security/)).
