# v4 – Conferme semantiche controllate

## Obiettivo
Quando il sistema ha una proposta valida in attesa, risposte libere come
"Ok, procediamo", "Sì, va bene" o "Perfetto, facciamolo" sono interpretate
semanticamente dal modello AI. Non si mantiene una lista di frasi accettate.

I pulsanti WhatsApp e le due risposte canoniche `sì` / `no` restano una via
rapida deterministica. Le altre espressioni vengono interpretate mediante
`app/agent/semantic_confirmation.py`, utilizzando le istruzioni modificabili
`app/agent/confirmation_prompt.md`.

## Autorizzazione
La decisione AI può essere `ACCEPT`, `REJECT`, `MODIFY`, `UNCERTAIN` o
`UNRELATED`. Solo `ACCEPT` con `unconditional=true` produce il segnale
`Intent.CONFIRM`. **Non basta per scrivere nel database**: il controller
esistente ricontrolla l'identificativo della proposta, l'impronta, l'identità,
la scadenza e le altre regole di dominio. Le scritture continuano nei flussi
esistenti, mai nel classificatore AI.

- `MODIFY` non conferma: propone di interrompere la conferma corrente prima di
  cambiare le preferenze.
- `UNCERTAIN` o `UNRELATED` non confermano: si richiede una risposta esplicita.
- Risposta malformata, timeout o indisponibilità del modello: nessuna scrittura.
- Proposta scaduta, pulsante estraneo o non pertinente: nessuna scrittura.

## Parametri – file unico
Modificare `app/agent/settings.json`:

- `confirmation_semantic_enabled`: abilita l'interprete per il testo libero.
- `confirmation_timeout_seconds`: timeout di una chiamata di classificazione
  (da 3 a 30 secondi).
- `confirmation_max_output_tokens`: token di risposta massimi (da 60 a 300).
- `model`: se non nullo, modello condiviso dell'agente; altrimenti
  `AI_MODEL_INTENT` già configurato su Render.

Queste impostazioni sono globali. Non sono presenti override tenant per la
classificazione semantica, perché la disattivazione dei controlli di
sicurezza non deve essere delegabile ai tenant.

## Deploy
Sostituire i file indicati nell'archivio `v4-conferme-semantiche-solo-modifiche.zip`
rispettando i percorsi. Non richiede variabili Render nuove o migrazioni SQL.

## Test
Eseguire dalla root del progetto:

```
PYTHONPATH=. python -m pytest -q
```

Sono presenti test che simulano la risposta del modello; il collaudo reale
con WhatsApp e il modello esterno resta necessario prima della messa in
produzione. Non è stato testato un reale commit Supabase in queste prove.
