# Calibrazione e modello fantacalcistico fuori dal prezzo finché gate OOS

Il prezzo suggerito resta la formula esplicita del mercato (quotazione ×
inflazione × scarsità × sconto invenduto). La calibrazione imparata dalle
vendite reali e qualsiasi modello del rendimento fantacalcistico NON entrano
mai nel prezzo suggerito né nella max offerta sostenibile, finché un gate di
qualità valutato out-of-sample non li promuove — e anche allora l'attivazione
richiede una decisione esplicita, non è automatica.

Il trade-off: potenziale accuratezza in più contro il rischio di fondare il
prezzo su evidenza debole (campioni piccoli, in-sample, proxy). Fintanto che
il gate non passa, i modelli producono solo indicazioni advisory etichettate
come tali e `use_calibration_in_price` resta False di default.

## Consequences

- I report dei backtest sono l'unico canale con cui un modello può essere
  promosso: il criterio è binario e ispezionabile.
- L'advisory è sempre esplicitamente marcata (`applied=False`): mai un
 'influenza silenziosa sul prezzo.
- Chi vuole "collegare il modello al prezzo" deve prima far passare il gate
  su dati out-of-sample e poi prendere la decisione esplicita.
