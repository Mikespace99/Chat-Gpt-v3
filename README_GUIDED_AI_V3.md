# Guided AI v3 — affidabilità webhook / operazioni (base: LLM-Deepseek originale)

## Che cosa cambia

- **Meta POST `/webhook/whatsapp`** verifica la firma e salva ogni messaggio in `whatsapp_inbox` (ID `wamid` unico), **prima** di rispondere 200. Se il DB non risponde, restituisce 503.
- **Worker integrato** in `app/main.py` elabora i messaggi dalla inbox PostgreSQL usando claim atomici e lease. Il contesto `conversations.context_v2`, lo stato del messaggio e i messaggi in uscita vengono salvati da **una singola RPC transazionale** (`agent_finish_inbox`).
- **Outbox persistente** conserva il testo e i pulsanti WhatsApp. Le risposte vengono inviate dal worker, non dalla richiesta HTTP di Meta. Gli invii con esito incerto non vengono ritentati automaticamente: vanno riconciliati dall'operatore.
- **Creazione, spostamento e cancellazione da WhatsApp** usano la RPC `agent_execute_operation`, con un `operation_id` creato dal controller (UUID della proposta di conferma), impronta del payload e registrazione atomica del risultato. Retry con stessa chiave restituisce il risultato originale; chiave riusata con dati diversi è rifiutata.
- **Rimane il progetto originale:** FastAPI, dashboard, repository Supabase, calendario, AI JSON + prompt aggiornabili. Nessun accesso diretto dell'AI al DB.

## Installazione / Render — NECESSARIA MIGRATION

1. **Salva un backup** del progetto Supabase di test. Controlla prima che lo schema originale e `migrations/003_add_context_v2.sql` siano stati installati: `conversations.context_v2` deve esistere.
2. In Supabase **SQL Editor**, esegui solo `migrations/004_reliable_messaging.sql` (additiva). **Non rieseguire `001_schema_completo.sql`**, che ricrea lo schema.
3. Aggiorna il repository GitHub con il progetto contenuto in questo ZIP; usa inizialmente un Web Service Render **di test**.
4. Conserva **tutte le variabili Environment esistenti**, senza aggiungere `TENANT_ID`. Sono necessarie `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (service-role server-side), `OPENAI_API_KEY`, `WHATSAPP_APP_SECRET` e le credenziali WhatsApp originali. Non pubblicare le chiavi nel repository.
5. Build: `pip install -r requirements.txt`; start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.
6. GET `/health`, poi test webhook Meta firmato e numero WhatsApp di test. Non collegare clienti reali finché la migration e i test di integrazione non sono verificati.

Non serve né Redis né un worker Render separato per il test iniziale. Il singolo worker gira nella lifespan FastAPI; un Render **con istanze multiple** richiede ulteriori test di concorrenza/lease prima del deploy.

## Verifiche locali

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q tests
python -m compileall -q app
```

I test usano mock/stub Supabase: **non eseguono realmente PostgreSQL né Meta**. La migration non può essere considerata certificata senza SQL Editor e prove di transazione su un progetto Supabase di prova.

## Limiti da verificare prima della produzione

- Non sono ancora stati eseguiti test integrati su database Supabase vero, né test d'invio/ricezione Meta su Render. Serve provarli.
- La SQL RPC verifica proprietà cliente, scadenza appuntamento, stato, numero massimo di appuntamenti futuri e vincolo anti-sovrapposizione del DB. Il motore Python determina slot, durata e regole di calendario. **Orari di apertura, festività ed eccezioni NON sono ricontrollati integralmente dentro la transazione SQL**: prima della produzione verificare un controllo finale delle regole con semantica coerente all'engine, e le modifiche amministrative concorrenti.
- L'idempotenza riguarda le operazioni **confermate via flusso WhatsApp v3**; gli strumenti manuali della dashboard usano gli endpoint esistenti e richiedono audit separato.
- Le chiamate di creazione a `customers`/`conversations` nel repository originale sono ancora fuori dalla transazione inbox e richiedono verifica dei vincoli univoci di identità, prima di usare più istanze.
- Una lease scaduta sulla inbox rende il messaggio nuovamente elaborabile; l'operazione business è protetta da `operation_id`, ma una modifica fuori workflow non lo è automaticamente.
- Le risposte WhatsApp con esito `uncertain` **non vengono reinviate** per evitare duplicati; è necessaria una procedura operatore di riconciliazione. Controllare anche `dead` per inbox.
- Il codice non introduce un calendario Google/n8n: nel progetto originale la fonte di verità è Supabase.
- Gli ID dei messaggi e le conversazioni contengono dati personali: servono retention, log minimizzati, permessi e informative coerenti con GDPR.

## Query di diagnosi (solo SQL Editor, account autorizzato)

```sql
select status, count(*) from public.whatsapp_inbox group by status;
select status, count(*) from public.whatsapp_outbox group by status;
select * from public.whatsapp_inbox where status='dead' order by created_at desc limit 20;
select id,created_at,last_error from public.whatsapp_outbox where status='uncertain' order by created_at desc limit 20;
```

**Non** cambiare `uncertain` in `pending` alla cieca: un invio potrebbe essere stato ricevuto da Meta anche se Render non ne ha ricevuto la conferma HTTP.
