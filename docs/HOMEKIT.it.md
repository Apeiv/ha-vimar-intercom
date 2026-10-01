# Videocitofono HomeKit

🇬🇧 *[English](HOMEKIT.md)* · [← README](../README.it.md)

L'integrazione può pubblicare il citofono nell'app Casa di Apple come videocitofono suo: notifica di
squillo, video dal vivo, audio nei due sensi e il cancello come serratura nella stessa vista. Di serie
è spento. Il bridge HomeKit di Home Assistant non ci riesce: il suo campanello mostra Parla, ma non
ascolta sulla porta che comunica al telefono, e chi è in strada non ti sente. I dettagli sono in
[HOMEKIT.md](HOMEKIT.md).

1. Impostazioni → Dispositivi e servizi → Vimar Intercom → **Configura** → **HomeKit** → accendi
   **Pubblica in HomeKit**.
2. La stessa pagina HomeKit (solo per gli amministratori) mostra ora un QR e un codice di 8 cifre;
   una notifica ricorda che l'abbinamento è in attesa. Nell'app Casa scegli **Aggiungi accessorio** e
   inquadralo; iOS avvisa che l'accessorio non è certificato, scegli **Aggiungi comunque**.
3. Nelle impostazioni dell'accessorio, **Mostra come riquadri separati** porta il cancello nella
   vista dal vivo.

| Opzione | Descrizione |
|---|---|
| **Video più fluido (ricodifica)** | Acceso (di serie): un keyframe al secondo, così un pacchetto perso dal relay è una breve sbavatura invece di un blocco fino a 3 s; l'apertura richiede circa 0,5 s in più. Spento: il flusso della targa così com'è, più rapido ad aprirsi |
| **Durante uno squillo** | *Rispondi quando parli* (di serie): aprire la notifica mostra la strada senza rispondere, e la prima parola risponde. *Rispondi all'apertura*: aprire risponde subito e il Tab smette di suonare |

Chiudere l'ultima vista HomeKit chiude una chiamata fatta o risposta da HomeKit. Chi usa il
campanello nell'app Casa (chi l'ha abbinato e chi condivide la casa) vede il video, parla e apre il
cancello: `allowed_users` non vale per HomeKit. Non abbinare lo
stesso citofono anche tramite un bridge HomeKit di Scrypted: l'app Casa mostrerebbe due campanelli e
ogni squillo arriverebbe due volte.


I dettagli tecnici (come viaggiano audio e video, misure, problemi noti) sono nella pagina in inglese: [HOMEKIT.md](HOMEKIT.md).
