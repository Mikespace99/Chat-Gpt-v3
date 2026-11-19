# Guided AI v3 — correzione spostamento relativo

## Problema riprodotto

Dopo aver identificato e confermato l'appuntamento da spostare, il cliente
scrive «Solo orario, vorrei posticiparlo se possibile». Lo schema JSON
non aveva un campo per esprimere un orario *relativo*; il contesto AI
non passava l'ora originale dell'appuntamento; il motore ricavava
`after_time` solo dagli slot già offerti.

## Cambiamenti

- `app/ai/schemas.py`: aggiunti `time_direction` (LATER/EARLIER) e `keep_same_date`.
- `app/ai/prompts/interpreter.md`: istruzioni con due esempi generali, senza elenco di sinonimi.
- `app/ai/context_summary.py`: ora passa la data e l'ora del target verificato, senza ID o dati personali.
- `app/context/models.py`: `SearchCriteria.after_time` e `before_time`.
- `app/context/context_manager.py`: ancora il giorno e la direzione relativa al target verificato.
- `app/flows/common.py`: passa i limiti temporali all'engine.
- `app/booking/engine.py`: aggiunto filtro rigoroso `before_time` per gli anticipi; `after_time` esisteva già.
- `tests/test_relative_reschedule_v3.py`: test regressivi con date e orari effettivi.

## Sicurezza

L'AI restituisce soltanto dati JSON. La data e l'ora usate per filtrare
provengono dall'appuntamento già verificato nel contesto Python.
La ricerca non modifica l'appuntamento. Per salvare lo spostamento resta
obbligatoria la conferma esplicita e il flusso transazionale esistente.
Nessuna modifica a variabili Render o tabelle Supabase.

## Test locali

`python -m pytest -q` — 58 superati nell'ambiente di preparazione.
Restano da verificare il comportamento linguistico del modello reale e
una conversazione di spostamento via Meta -> Render -> Supabase.

## Prova consigliata

Appuntamento esistente a una data futura alle 14:30; il cliente sceglie
«Spostare», conferma il target e scrive «Solo orario, vorrei posticiparlo».
Atteso: ricerca nello stesso giorno, soltanto dopo le 14:30; nessuna
scrittura fino alla conferma del nuovo slot. Provare anche «anticiparlo».
