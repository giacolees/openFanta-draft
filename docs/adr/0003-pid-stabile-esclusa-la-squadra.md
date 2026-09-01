# Identità giocatore = PID stabile derivato da nome normalizzato + ruolo, senza squadra

Il listone sorgente non ha un ID. L'identità di un giocatore è un PID
deterministico derivato da nome normalizzato + ruolo — la squadra è volutamente
esclusa: i trasferimenti non devono spezzare l'identità, e il libro viene
ricalcolato ogni giorno.

Le alternative erano: usare il nome come identità (rotto con gli omonimi)
o includere la squadra (rotta a ogni trasferimento, con doppioni nel pool).
La collisione di omonimi reali nello stesso ruolo è un errore bloccante
esplicito: mai unione automatica né suffissi inventati.

## Consequences

- Il join tra listone, vendite e rendimenti stagionali usa il PID
  preferenzialmente; il nome resta accettato come fallback legacy solo se
  risolve un giocatore unico.
- Un cambiamento dell'algoritmo di PID romperebbe il join con lo storico:
  è versionato nei metadati del listone.
