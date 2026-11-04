# Gestione Magazzino

Gestionale da banco per un piccolo negozio al dettaglio: cassa, carichi e scarichi,
resi, trasferimenti tra negozio e box, ricerca e storico, statistiche e anagrafica
fornitori. Applicazione desktop in Python/tkinter con database SQLite locale.

## Funzionalità

- **Movimenti di magazzino**: carico da fornitore con riferimento bolla, vendita con
  carrello e metodo di pagamento, resi cliente e resi fornitore, rettifiche inventariali.
- **Due depositi**: giacenze separate per *Negozio* e *Box*, con trasferimenti interni.
- **Ricerca e storico**: ricerca articoli, cronologia dei movimenti, annullamento di un
  movimento con doppio clic, archiviazione e ripristino degli articoli.
- **Statistiche**: pezzi e valore del magazzino a prezzo di acquisto e di vendita,
  articoli sotto la soglia minima.
- **Esportazione CSV**: storico movimenti, giacenze e report anomalie.
- **Etichette barcode**: stampa Code128 su Brother QL-700 tramite CUPS.
- **Backup automatici**: un backup ogni 30 minuti (ne vengono conservati 16) e uno
  giornaliero alla chiusura.
- **Aggiornamento da GitHub**: all'avvio il gestionale controlla se ci sono novità e,
  su conferma, si aggiorna facendo prima un backup del database.
- **Spegnimento serale**: un timer systemd spegne il computer del negozio alle 21:00.

## Requisiti

- Linux con Python 3
- `git` (per gli aggiornamenti automatici)
- CUPS e driver Brother QL-700 (solo per la stampa etichette)

## Installazione

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install ttkbootstrap python-barcode pillow
python setup_db.py
```

Per caricare il catalogo iniziale da `dati.csv` e lo storico da
`storico_carico.csv` / `storico_scarico.csv`:

```bash
python importa_dati.py
python importa_dati.py --storico
```

## Avvio

```bash
./avvia_gestionale.sh
```

## Test

```bash
python -m unittest test_gestionale -v
```

I test girano senza display su un database in memoria.

## Documentazione

- `Istruzioni.txt` — configurazione della stampante Brother, del timer di spegnimento
  e calibrazione delle etichette.
- `CLAUDE.md` / `AGENTS.md` — descrizione tecnica del codice (struttura, schema del
  database, convenzioni) usata anche dagli assistenti AI che collaborano al progetto.

## Autore

Sviluppato da Giovanni Poggi.
