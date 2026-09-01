# openFanta-draft — Asta del Fantacalcio

Contesto unico per la gestione di un'asta del Fantacalcio: dal listone
quotidiano alla rosa della propria squadra, con evidenza statistica tenuta
separata dal prezzo di mercato. Qui si parla di giocatori, crediti, squadre
e della lingua condivisa tra chi batte l'asta e chi costruisce gli strumenti.

## Language

### Prezzo e mercato

**Prezzo di mercato**:
Il prezzo a cui un giocatore viene effettivamente comprato all'asta. È un
fatto dell'asta, non una stima: esiste solo dopo l'aggiudicazione.
_Avoid_: quotazione, stima prezzo, fair value

**Prezzo suggerito**:
Il prezzo di partenza indicato dal listone per un giocatore ancora in
carrello. È un riferimento d'asta, mai un prezzo pagato.
_Avoid_: PFC (usare solo come sigla del dato del listone), quotazione

**Valore fantacalcistico**:
Il rendimento atteso di un giocatore in fantapunti, indipendente da quanto
costa. Appartiene al giocatore, non all'offerta.
_Avoid_: prezzo, prezzo giusto, potenziale

**Valore per la rosa**:
Quanto un giocatore vale PER LA MIA squadra, dati slot coperti e alternative
residue: può differire dal prezzo di mercato. Distinto da max offerta.
_Avoid_: valore intrinseco, rating

**Max offerta sostenibile**:
Il tetto oltre il quale comprare danneggerebbe il completamento della rosa.
Non è un prezzo di mercato: è un vincolo della propria squadra.
_Avoid_: maxbid usato nel parlato, budget residuo

**Calibrazione**:
L'aggiustamento del prezzo suggerito imparato dalle vendite reali già
avvenute. È un'indicazione (advisory): non modifica mai da sola il prezzo.
_Avoid_: addestramento live, modello di prezzo

### Qualità ed evidenza

**Gate qualità**:
Il criterio binario che decide se un modello merita promozione, valutato
fuori dal campione con cui è stato costruito. Un gate che non passa produce
sempre i motivi espliciti del non-pass; un gate che passa non attiva nulla
da solo: serve una decisione esplicita.
_Avoid_: test automatico di attivazione, score

**Replay**:
La rigiocata dell'asta evento per evento, nell'ordine in cui è avvenuta:
ogni predizione usa solo ciò che era noto prima di quella vendita.
_Avoid_: simulazione, re-run

**Rettifica**:
La correzione di una vendita già registrata, che non cancella mai lo
storico: annulla l'effetto dell'evento precedente e registra il nuovo.
_Avoid_: modifica, fix, delete

**PID**:
L'identificatore stabile di un giocatore. Sopravvive ai trasferimenti di
squadra e ai refresh giornalieri del listone; l'identità è il giocatore,
non la sua squadra.
_Avoid_: id utente, nome (il nome non è identità: omonimi esistono)

**Invenduto**:
Il giocatore rimasto senza aggiudicazione dopo la chiamata. Non è una
vendita a prezzo zero.
_Avoid_: fallimento, no-buy
