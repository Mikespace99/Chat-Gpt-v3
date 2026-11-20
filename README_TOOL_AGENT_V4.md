# Chat-Gpt v4 – Tool calling controllato (prima integrazione)

Questa versione parte dall'ultimo progetto v3 con correzione spostamenti, dashboard e privacy.

## Scopo della fase

Durante la navigazione delle disponibilità (prenotazione o spostamento in corso),
l'AI può usare **strumenti Python di sola lettura** più volte nello stesso turno:

- `search_slots`: cerca sul motore delle disponibilità effettivo, applicando
  data, fascia oraria, limiti orari, scorrimento e filtri di rifiuto.
- `get_business_info`: mostra soltanto dati pubblici dei servizi.
- `delegate_to_workflow`: rimanda al controller v3, senza eseguire la modifica.

**Nessun tool dell'agente può creare, spostare o cancellare appuntamenti.**
Scelta degli slot, conferme, cancellazioni e scritture Supabase passano sempre
per i controller e le RPC già esistenti. Nessuna nuova migration richiesta.

Nella fase iniziale di una prenotazione, nella selezione di appuntamenti e nelle
conferme resta attivo il controller v3. La navigazione tra gli slot è invece
agentica. È una migrazione graduale intenzionale, non un'autonomia completa.

## Un solo file per i parametri modificabili

`app/agent/settings.json`

| Campo | Significato |
| --- | --- |
| `enabled` | Abilita/disabilita la navigazione agentica |
| `model` | `null` = modello già usato in `AI_MODEL_INTENT`; oppure nome di modello compatibile col tool calling |
| `max_tool_calls_per_turn` | Numero massimo di tool effettivamente eseguiti per messaggio (predefinito 4) |
| `max_tool_calls_hard_cap` | Soglia massima, fino a 6 |
| `max_search_days` | Massima distanza nel futuro delle date ricercabili (predefinito 30) |
| `max_search_days_hard_cap` | Limite assoluto, fino a 90 |
| `max_slots_per_page` | Fino a 3, per i bottoni WhatsApp |
| `max_remembered_slots` | Quanti orari mostrati ricordare nella sessione |
| `max_recent_user_messages` | Quanti messaggi del cliente includere nel contesto LLM |
| `request_timeout_seconds` | Timeout della singola chiamata AI |
| `max_output_tokens` | Budget massimo output del modello |
| `allow_tenant_overrides` | Elenco ristretto dei parametri che potranno essere configurati via dashboard tenant in futuro |

Le impostazioni vengono validate con Pydantic. Credenziali, bypass della
verifica Meta e autorizzazioni di scrittura **non** sono parametri di tenant.
Il codice contiene anche massimali non aggirabili dal JSON, per esempio fino a
6 tool call e massimo 3 bottoni WhatsApp.

Le istruzioni di comportamento linguistico sono in `app/agent/prompts.md`.
Cambiare questi file su GitHub richiede un nuovo deploy su Render; non serve
aggiungere variabili Environment.

## Installazione

1. Aggiorna il repository GitHub con lo ZIP completo, oppure copia il pacchetto
   dei soli file modificati conservando i percorsi `app/...`.
2. Mantieni tutte le attuali variabili Environment di Render.
3. Fai deploy su servizio di test, senza traffico di clienti reali.
4. Verifica: "Altri orari", "Dopo le 10", "Solo pomeriggio", "Un'altra settimana",
   selezione pulsante slot, conferma e saluto.
5. Controlla agenda e registrazione effettiva nel calendario Supabase.

## Test

Esegui nella root del progetto: `python -m pytest -q`

I nuovi test in `tests/test_tool_agent_v4.py` usano uno strumento disponibilità
simulato e un client AI fittizio: non provano la qualità delle decisioni del
modello in produzione. Non è ancora stato eseguito un test WhatsApp/Supabase
end-to-end con credenziali reali.

## Avvertenze

- L'AI può scegliere un tool in modo non corretto; in caso di indisponibilità
  dell'agente la pipeline tenta il controller v3 precedente. Non esegue scritture
  senza il normale percorso di conferma e autorizzazione.
- "Altri orari" esclude dalla pagina successiva gli slot già mostrati; una
  `new_search` permette di ripartire applicando nuovi filtri. Un rifiuto esplicito
  (`reject_current`) rimane escluso nella stessa sessione.
- Le disponibilità sono ricalcolate dal motore Supabase a ogni ricerca e
  ricontrollate dal flusso esistente prima di operazioni definitive.
- Le preferenze modificabili dal tenant sono **predisposte nel codice** ma non
  ancora esposte nella dashboard; l'unico file attivo è `settings.json`.
