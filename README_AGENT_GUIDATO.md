# AI guidata — aggiornamento del progetto originale (fase 1)

**Questa cartella è il progetto originale, non un'applicazione nuova.**
L'integrazione WhatsApp, la dashboard, il repository Supabase, la ricerca
 disponibilità, le migration SQL e il sistema di Render restano quelli esistenti.

## Che cosa è cambiato

- `app/ai/interpreter.py`: il modello interpreta **un elenco di richieste**
  in JSON, non più un unico intent; nessun accesso a database o tool.
- `app/ai/schemas.py`: contratto Pydantic ristretto, campi sconosciuti vietati,
  date e orari validati. Risposta fuori contratto = chiarimento, mai scrittura.
- `app/controller/interpretation_policy.py`: riduce le richieste a un segnale
  consentito ai flussi attuali. Operazioni incompatibili e conferme mescolate
  a modifiche vengono bloccate. Una conferma isolata senza proposta pendente
  viene rifiutata.
- `app/context/context_manager.py`: se l'interprete richiede chiarimento,
  non attiva neppure il matcher degli slot (non modifica la bozza operativa).
- `app/ai/prompts/interpreter.md`: **istruzioni modificabili** per il modello;
  le modifiche richiedono commit e deploy su Render.
- `app/ai/prompts/responder.md`: istruzioni supplementari modificabili per
  l'AI che scrive i messaggi. Le regole preesistenti del responder restano.
- `tests/test_ai_guardrails.py`: test automatizzati locali senza rete.

## Come installare su Render

1. Carica il contenuto di questa cartella nel repository GitHub collegato al
   tuo servizio Render, preferibilmente in un branch di test.
2. **Non eseguire migration SQL**: nessuna tabella è stata cambiata.
3. Conserva **tutte le stesse Environment Variables** già impostate in Render.
   Non sono richieste `TENANT_ID` né altre nuove variabili; la selezione del
   tenant avviene come nell'originale tramite numero WhatsApp Business.
4. Build: `pip install -r requirements.txt`
5. Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
6. Verifica `GET /health` e poi prova un numero WhatsApp di test.
   **Webhook Meta già esistente:** `GET/POST /webhook/whatsapp`.

## Test locali

```bash
pip install -r requirements.txt
pip install pytest
python -m pytest -q tests/test_ai_guardrails.py
python -m compileall -q app
```

I test locali non provano che la rete Meta, Supabase e le credenziali Render
siano raggiungibili o corrette. Esegui un collaudo su ambiente di test prima
che il numero risponda a clienti reali.

## Limiti attuali da conoscere (non nascosti)

- In **questa prima fase** una richiesta multipla con prenotazione + domanda
  informativa viene riconosciuta, ma solo la richiesta operativa principale
  è elaborata. La risposta alla domanda secondaria **non è ancora garantita**.
- Più richieste operative distinte nello stesso messaggio sono fermate con
  una richiesta di chiarimento, anziché eseguite in sequenza.
- Si riutilizzano gli attuali flow e i loro controlli; la migrazione al
  nuovo controller completo e le conferme con digest/versione non sono ancora fatte.
- L'elaborazione webhook e il debounce sono ancora quelli dell'originale;
  in particolare multi-worker/retry e idempotenza persistente richiedono
  un passaggio successivo prima della produzione.
- **Google Calendar esterno:** il repository originale specifica che la
  precedente integrazione n8n/Google Calendar è stata rimossa e che la
  tabella `appointments` Supabase è la fonte di verità. Non esiste in questo
  ZIP un adapter Google Calendar da poter "preservare". La dashboard agenda
  interna legge Supabase. Se hai un'integrazione esterna in un branch più
  recente, va verificata separatamente prima di incorporarla.

## Regola di sicurezza

I prompt sono istruzioni linguistiche, non autorizzazioni. Non cambiare mai
le policy Python modificando solo i `.md`. Il backend possiede e gestisce le
credenziali Supabase e continua a eseguire le operazioni tramite repository,
non tramite il modello. Non condividere le credenziali nei log o nei prompt.
