CORREZIONE CONFERME DOPO IL FIX DEI SALUTI
=========================================

Problema: alla domanda «È questo l'appuntamento che vuole spostare?» il
cliente rispondeva «Sì» e riceveva «La ringrazio, buona giornata».

Causa possibile confermata staticamente nel codice: l'interprete poteva
classificare «Sì» come ACKNOWLEDGMENT/THANKS e il controller trattava
THANKS come chiusura prima di considerare una conferma pendente.

Modifica applicativa: app/orchestrator.py
Nuovo test: tests/test_confirmation_social_regression_v3.py

La correzione:
- riconosce solo un «Sì»/«No» isolato in presenza di proposta pendente,
  oppure il pulsante con l'esatto ID di proposta, prima di invocare l'AI;
- verifica sempre la validità della proposta con il controllo già esistente;
- non interpreta ringraziamenti, frasi modificate o «Sì, ma...» come conferme;
- impedisce che un pulsante di conferma non valido diventi un saluto;
- mantiene invariati i repository Supabase, il calendario e le variabili Render.

Installazione: sostituire app/orchestrator.py nel repository in uso,
commit e deploy Render. Nessuna migration SQL o nuova variabile.

Test locali: python -m pytest -q
I test usano simulazioni e NON costituiscono un collaudo Meta/Supabase reale.

Dopo deploy provare un appuntamento DI TEST: chiedere spostamento,
confermare il target con «Sì» e verificare che chieda la nuova preferenza
anziché salutare; provare anche il pulsante, «Grazie», una conferma
scaduta e «Sì, ma...». Nessuna modifica al DB prima della conferma finale.
