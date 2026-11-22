# WhatsApp appuntamenti — agente conversazionale unico (prototipo)

L'utente scrive a Meta WhatsApp → FastAPI riceve il webhook → coda Supabase
(`whatsapp_inbox`) → `app/messaging/worker.py` → `app/webhook.py` →
`app/simple_agent/service.py`. L'AI interpreta testo, mantiene 6 scambi
(12 messaggi) e chiama **solo quattro tool Python**:

- `list_appointments` — legge solo appuntamenti del cliente del numero WhatsApp;
- `business_info` — informazioni e servizi del tenant;
- `search_slots` — consulta il motore `app/booking/engine.py`;
- `propose_operation` — crea **solo una proposta**, mai una prenotazione.

La proposta viene salvata in `conversations.context_v2` con UUID generato in Python.
La conferma può arrivare come risposta naturale (es. «sì, confermo») o tramite pulsante WhatsApp. In entrambi i casi Python verifica la proposta pendente, scadenza e autorizzazione prima della scrittura. Il backend
usa `app/operations/idempotency.py`, la RPC `agent_execute_operation` di
`migrations/004_reliable_messaging.sql`, controllo tenant/cliente, vincolo PostgreSQL
anti-overlap e coda inbox/outbox. Nessun accesso diretto dell'AI a Supabase.

## Prima di provare

1. Installare `requirements.txt` e impostare le variabili nel sistema Render:
   `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `OPENAI_API_KEY`,
   `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`,
   `WHATSAPP_APP_SECRET`. Consultare `app/config.py` per le altre opzioni.
2. Applicare le migrazioni originali nell'ordine corretto su un database di test
   (in particolare `004_reliable_messaging.sql` e `005_simple_agent_activity.sql`). Non rilanciare `001` su DB
   già inizializzato.
3. Configurare su Meta il webhook HTTP previsto da `app/main.py`.
4. Test di base: `python -m pytest -q tests/test_simple_agent.py`.

La nuova pipeline salva nel JSON `context_v2` lo schema `simple-agent-1`.
Le precedenti conversazioni di test vengono ricominciate automaticamente.

## Limitazioni del prototipo e verifiche necessarie

- Non testato qui end-to-end contro API reali Meta, OpenAI e Supabase.
- Se un invio di conferma WhatsApp fallisce dopo la creazione della proposta,
  il worker segna l'invio come incerto: controllare outbox prima del retry.
- Testare su un DB di prova creazione, cancellazione, spostamento, conflitti
  concorrenti, ricerca multi-servizio, risposte vaghe e messaggi ravvicinati.
- Quando una ricerca non trova risultati il motore applica i suoi limiti attuali;
  la ricerca di nuove alternative va verificata sul calendario di prova.
- Per mostrare anche i pulsanti WhatsApp (facoltativi) impostare `WHATSAPP_CONFIRM_BUTTONS=true` su Render; di default sono disattivati. Il testo funziona sempre.
- Risposte dubbie non confermano nulla; un cambio di preferenza invalida la vecchia proposta e riprende la conversazione. Se la classificazione AI fallisce, non viene eseguita alcuna scrittura.
- L’interpretazione del testo non è infallibile: provare conferme, negazioni e frasi condizionali reali prima dell’uso con clienti.
- L'agente gestisce al massimo quattro chiamate a tool per turno.
- Non usare ancora per pazienti/clienti reali finché non sono stati superati
  test di integrazione, privacy, autorizzazioni e carico.
