# AI guidata v2 — retrofit sul progetto originale

## Cosa include davvero

- App FastAPI + webhook Meta + dashboard + repository e calendario Supabase ORIGINALI.
- Interprete AI senza tool, risposta JSON rigorosamente validata da Pydantic.
- Istruzioni linguistiche modificabili in `app/ai/prompts/interpreter.md` e `responder.md`.
- Policy Python che blocca intent operativi in conflitto e conferme mescolate con modifiche.
- Controller `app/controller/workflow.py` al posto del vecchio router:
  il controller sceglie servizi di dominio con guardrail, non regole sulle frasi.
- Cancellazione sicura distinta da abbandono di una bozza: conferma esplicita,
  controllo del cliente/tenant e cancellazione soft-deletion.
- Conferme con UUID, SHA256 del contesto operativo e scadenza 15 minuti.
  I pulsanti Meta usano l'ID della proposta, così un vecchio pulsante
  non conferma una proposta nuova.
  Sono emesse dal backend; l'AI non può generarne una valida.
- Domande informative secondarie registrate e, se presenti dati conoscitivi,
  elaborate come messaggi separati, senza alterare la prenotazione.
- Parser Meta che itera sulle entry e messages, deduplica ripetizioni
  NEL SINGOLO payload e rifiuta webhook senza firma verificabile.
- Simulatore offline dei contratti AI, nessuna connessione a Meta/Supabase.

## Come partire su Render (servizio di TEST)

1. Usa un branch GitHub di test, con il codice dello ZIP estratto nella root del repo.
2. Conserva le variabili del vecchio progetto: OPENAI_API_KEY, AI_MODEL_INTENT,
   AI_MODEL_RESPONSE, SUPABASE_URL, SUPABASE_SERVICE_KEY, WHATSAPP_TOKEN,
   WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET,
   MESSAGE_DEBOUNCE_SECONDS, DEFAULT_SLOT_SEARCH_DAYS,
   CONVERSATION_TIMEOUT_MINUTES, MAX_RECENT_MESSAGES.
3. WHATSAPP_APP_SECRET diventa OBBLIGATORIO sul webhook POST. Non serve TENANT_ID.
4. Build: `pip install -r requirements.txt`.
5. Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.
6. Verifica GET `/health`, poi GET/POST `/webhook/whatsapp` con Meta test.
7. Non eseguire 001_schema_completo.sql: e' una migration distruttiva.
   Nessuna nuova migration richiesta da questa versione.

## Test locali

    python -m pip install -r requirements.txt
    python -m pip install pytest
    python -m pytest -q tests
    python -m compileall -q app
    python -m app.simulator.cli examples/conversation_simulation.json

Il simulatore non usa l'LLM reale: riceve nel file JSON esempi di output dell'AI
che possono essere validati e riprodotti senza credenziali. Non e' una simulazione
end-to-end di prenotazione.

## Sicurezza e limiti importanti

- AI non ha tool di lettura/scrittura su Supabase. Python accede ai repository
  esistenti e invia al modello solo i fatti contestuali necessari.
- Conferme legate a una proposta, ma NON e' ancora disponibile un commit
  transazionale unico tra messaggio webhook, contesto e scrittura appuntamento.
- Il buffer dei messaggi e' ancora in RAM: retry dopo crash o due istanze
  Render possono causare una doppia elaborazione; usare un solo worker
  per i test, NON considerare il sistema idempotente in produzione.
- Il vecchio routing_table.py resta nello ZIP per riferimento, ma la pipeline
  ora chiama `app/controller/workflow.py`. Rimangono invece adattati e
  riutilizzati i servizi di prenotazione/spostamento originali.
- Più operazioni mutative distinte nello stesso messaggio vengono bloccate
  e chiarite, non eseguite automaticamente una dietro l'altra.
- La compatibilità di Meta e Supabase REALI non può essere verificata
  in questi test locali senza connessione e credenziali.
- Il progetto originale ha rimosso n8n / Google Calendar esterno:
  la fonte calendario è `appointments` su Supabase, usata dalla dashboard.

## Lavoro necessario prima della produzione

1. Inbox/outbox persistente e deduplicazione transazionale WhatsApp.
2. Unicità delle operazioni di scrittura su Supabase + gestione del recupero
   in caso di crash fra commit e risposta al cliente.
3. Versionamento atomico del contesto per evitare lost-update concorrenti.
4. Rifattorizzazione interna degli ultimi flow legacy e test di integrazione
   con Supabase reale, inclusa cancellazione e spostamento.
5. Collaudo end-to-end con Render e un progetto Supabase di prova.

Non pubblicare sul numero WhatsApp dei clienti senza questi controlli.
