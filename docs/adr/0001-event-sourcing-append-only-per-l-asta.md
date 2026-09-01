# Event store append-only per l'asta

Lo storico dell'asta è un log di eventi append-only in SQLite: le vendite e
gli invenduti non si cancellano mai. Una correzione (rettifica) appende un
evento compensativo che rende inattivo l'evento target, mantenendo lo
storico integro; il replay ricostruisce lo stato applicando solo gli eventi
attivi, in ordine, verificando le invarianti a ogni passo.

Abbiamo scelto questo modello perché l'asta è una sequenza irrevocabile di
fatti: la forza dello storico è poter riprodurre qualunque stato passato
(il backtest del prezzo lo usa per ripetere l'asta prequenzialmente senza
leakage). L'alternativa — mutare le righe vendite — avrebbe reso i backtest
non riproducibili e le rettifiche non distinguibili dai dati originali.

## Consequences

- Ogni consumer (web, CLI, backtest) legge lo stesso log: una sola fonte
  di verità.
- Il backup è un dump del log: import/restore sono deterministici.
- Cancella-re-non-si-fa: qualsiasi "pulizia" dello storico è un errore di
  progetto, non un'ottimizzazione.
