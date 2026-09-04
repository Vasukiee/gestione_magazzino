
from db import assicura_cartelle, connetti


def inizializza_database(conn=None):
    """Crea lo schema e applica le migrazioni.

    conn permette ai test di lavorare su un database temporaneo; se non viene
    passata si usa quella di produzione e la si chiude a fine lavoro.
    """
    proprietaria = conn is None
    if proprietaria:
        assicura_cartelle()
        conn = connetti()
    cursor = conn.cursor()

    # Anagrafica aggiornata con il doppio prezzo e soglia minima scorta
    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS articoli (
                                                           codice TEXT PRIMARY KEY,
                                                           descrizione TEXT NOT NULL,
                                                           colore TEXT,
                                                           taglia TEXT,
                                                           prezzo_acquisto REAL,
                                                           prezzo_vendita REAL,
                                                           soglia_minima INTEGER DEFAULT 2,
                                                           attivo INTEGER NOT NULL DEFAULT 1
                   )
                   """)

    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS depositi (
                                                           id INTEGER PRIMARY KEY,
                                                           nome_deposito TEXT NOT NULL
                   )
                   """)

    # Tabella Fornitori
    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS fornitori (
                                                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                            ragione_sociale TEXT NOT NULL UNIQUE
                   )
                   """)

    # Tabella Transazioni
    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS transazioni (
                                                              id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                              data_ora DATETIME DEFAULT CURRENT_TIMESTAMP,
                                                              totale REAL NOT NULL,
                                                              metodo_pagamento TEXT
                   )
                   """)

    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS movimenti_magazzino (
                                                                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                                      codice TEXT NOT NULL,
                                                                      quantita INTEGER NOT NULL,
                                                                      id_deposito_origine INTEGER,
                                                                      id_deposito_destinazione INTEGER,
                                                                      tipo INTEGER NOT NULL,
                                                                      data_ora DATETIME DEFAULT CURRENT_TIMESTAMP,
                                                                      id_fornitore INTEGER,
                                                                      riferimento_bolla TEXT,
                                                                      id_transazione INTEGER,
                                                                      storico_passivo INTEGER DEFAULT 0,
                                                                      nome_fornitore_storico TEXT,
                                                                      FOREIGN KEY (codice) REFERENCES articoli (codice),
                       FOREIGN KEY (id_deposito_origine) REFERENCES depositi (id),
                       FOREIGN KEY (id_deposito_destinazione) REFERENCES depositi (id),
                       FOREIGN KEY (id_fornitore) REFERENCES fornitori (id),
                       FOREIGN KEY (id_transazione) REFERENCES transazioni (id)
                       )
                   """)

    cursor.execute("INSERT OR IGNORE INTO depositi (id, nome_deposito) VALUES (1, 'Negozio')")
    cursor.execute("INSERT OR IGNORE INTO depositi (id, nome_deposito) VALUES (2, 'Box')")

    # Popolamento di esempio per i fornitori
    cursor.execute("INSERT OR IGNORE INTO fornitori (ragione_sociale) VALUES ('Fornitore Generico S.p.A.')")
    cursor.execute("INSERT OR IGNORE INTO fornitori (ragione_sociale) VALUES ('Grossista di Quartiere S.r.l.')")

    # Migrazioni
    _migra_prezzi(cursor)
    _migra_soglia_minima(cursor)
    _migra_tracciamento_documentale(cursor)
    _migra_transazioni(cursor)
    _migra_storico_passivo(cursor)
    _migra_nome_fornitore_storico(cursor)
    _migra_articoli_attivo(cursor)
    _ripara_fornitori_orfani(cursor)
    _crea_viste(cursor)

    conn.commit()
    if proprietaria:
        conn.close()
        print("Infrastruttura database aggiornata.")
    return conn


def _migra_prezzi(cursor):
    """Aggiunge le colonne prezzo_acquisto e prezzo_vendita se non esistono."""
    cursor.execute("PRAGMA table_info(articoli)")
    colonne = [col[1] for col in cursor.fetchall()]
    if 'prezzo_acquisto' not in colonne:
        cursor.execute("ALTER TABLE articoli ADD COLUMN prezzo_acquisto REAL DEFAULT 0.0")
        print("Migrazione: colonna 'prezzo_acquisto' aggiunta.")
    if 'prezzo_vendita' not in colonne:
        cursor.execute("ALTER TABLE articoli ADD COLUMN prezzo_vendita REAL DEFAULT 0.0")
        print("Migrazione: colonna 'prezzo_vendita' aggiunta.")


def _migra_soglia_minima(cursor):
    """Aggiunge la colonna soglia_minima alla tabella articoli se non esiste già."""
    cursor.execute("PRAGMA table_info(articoli)")
    colonne_esistenti = [col[1] for col in cursor.fetchall()]
    if 'soglia_minima' not in colonne_esistenti:
        cursor.execute("ALTER TABLE articoli ADD COLUMN soglia_minima INTEGER DEFAULT 2")
        print("Migrazione: colonna 'soglia_minima' aggiunta alla tabella articoli.")

def _migra_tracciamento_documentale(cursor):
    """Aggiunge le colonne per il tracciamento documentale se non esistono."""
    cursor.execute("PRAGMA table_info(movimenti_magazzino)")
    colonne = [col[1] for col in cursor.fetchall()]
    if 'id_fornitore' not in colonne:
        cursor.execute("ALTER TABLE movimenti_magazzino ADD COLUMN id_fornitore INTEGER REFERENCES fornitori(id)")
        print("Migrazione: Aggiunta colonna 'id_fornitore' a movimenti_magazzino.")
    if 'riferimento_bolla' not in colonne:
        cursor.execute("ALTER TABLE movimenti_magazzino ADD COLUMN riferimento_bolla TEXT")
        print("Migrazione: Aggiunta colonna 'riferimento_bolla' a movimenti_magazzino.")

def _migra_transazioni(cursor):
    """Aggiunge la colonna id_transazione a movimenti_magazzino se non esiste."""
    cursor.execute("PRAGMA table_info(movimenti_magazzino)")
    colonne = [col[1] for col in cursor.fetchall()]
    if 'id_transazione' not in colonne:
        cursor.execute("ALTER TABLE movimenti_magazzino ADD COLUMN id_transazione INTEGER REFERENCES transazioni(id)")
        print("Migrazione: Aggiunta colonna 'id_transazione' a movimenti_magazzino.")

def _migra_storico_passivo(cursor):
    """Aggiunge la colonna storico_passivo se non esiste."""
    cursor.execute("PRAGMA table_info(movimenti_magazzino)")
    colonne = [col[1] for col in cursor.fetchall()]
    if 'storico_passivo' not in colonne:
        cursor.execute("ALTER TABLE movimenti_magazzino ADD COLUMN storico_passivo INTEGER DEFAULT 0")
        print("Migrazione: Aggiunta colonna 'storico_passivo' a movimenti_magazzino.")

def _migra_nome_fornitore_storico(cursor):
    """Aggiunge la colonna nome_fornitore_storico se non esiste.
    Serve a preservare il nome del fornitore nei movimenti anche dopo
    l'eliminazione del fornitore stesso dall'anagrafica."""
    cursor.execute("PRAGMA table_info(movimenti_magazzino)")
    colonne = [col[1] for col in cursor.fetchall()]
    if 'nome_fornitore_storico' not in colonne:
        cursor.execute("ALTER TABLE movimenti_magazzino ADD COLUMN nome_fornitore_storico TEXT")
        print("Migrazione: Aggiunta colonna 'nome_fornitore_storico' a movimenti_magazzino.")


def _migra_articoli_attivo(cursor):
    """Aggiunge la colonna attivo alla tabella articoli se non esiste.

    Sostituisce la cancellazione fisica degli articoli: attivo = 0 nasconde
    l'articolo dalle ricerche e dalle statistiche ma conserva tutti i suoi
    movimenti, quindi lo storico e i report restano coerenti."""
    cursor.execute("PRAGMA table_info(articoli)")
    colonne = [col[1] for col in cursor.fetchall()]
    if 'attivo' not in colonne:
        cursor.execute("ALTER TABLE articoli ADD COLUMN attivo INTEGER NOT NULL DEFAULT 1")
        print("Migrazione: Aggiunta colonna 'attivo' ad articoli.")


def _ripara_fornitori_orfani(cursor):
    """Azzera i riferimenti a fornitori non piu' esistenti.

    Le vecchie cancellazioni di fornitori lasciavano movimenti che puntavano a
    un id inesistente. Con PRAGMA foreign_keys attivo quelle righe restano
    leggibili ma qualsiasi UPDATE su di esse fallirebbe, quindi il riferimento
    va sganciato conservando il nome nello storico."""
    orfani = cursor.execute("""
                            SELECT m.id, m.id_fornitore FROM movimenti_magazzino m
                            WHERE m.id_fornitore IS NOT NULL
                              AND NOT EXISTS (SELECT 1 FROM fornitori f WHERE f.id = m.id_fornitore)
                            """).fetchall()
    if not orfani:
        return
    cursor.execute("""
                   UPDATE movimenti_magazzino
                   SET nome_fornitore_storico = COALESCE(nome_fornitore_storico, 'Fornitore eliminato'),
                       id_fornitore = NULL
                   WHERE id_fornitore IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM fornitori f WHERE f.id = id_fornitore)
                   """)
    print(f"Migrazione: sganciati {len(orfani)} movimenti da fornitori inesistenti.")


def _crea_viste(cursor):
    """(Ri)crea la vista delle giacenze.

    Prima la stessa espressione COALESCE(SUM(CASE ...)) era ricopiata in sei
    query diverse con gli id deposito scritti a mano: una sola definizione qui
    elimina la possibilita' che divergano."""
    cursor.execute("DROP VIEW IF EXISTS v_giacenze")
    cursor.execute("""
                   CREATE VIEW v_giacenze AS
                   SELECT a.codice,
                          COALESCE(SUM(CASE WHEN m.storico_passivo = 0 AND m.id_deposito_destinazione = 1
                                            THEN m.quantita ELSE 0 END), 0) -
                          COALESCE(SUM(CASE WHEN m.storico_passivo = 0 AND m.id_deposito_origine = 1
                                            THEN m.quantita ELSE 0 END), 0) AS giac_negozio,
                          COALESCE(SUM(CASE WHEN m.storico_passivo = 0 AND m.id_deposito_destinazione = 2
                                            THEN m.quantita ELSE 0 END), 0) -
                          COALESCE(SUM(CASE WHEN m.storico_passivo = 0 AND m.id_deposito_origine = 2
                                            THEN m.quantita ELSE 0 END), 0) AS giac_box
                   FROM articoli a
                            LEFT JOIN movimenti_magazzino m ON a.codice = m.codice
                   GROUP BY a.codice
                   """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_movimenti_codice ON movimenti_magazzino (codice)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_movimenti_transazione ON movimenti_magazzino (id_transazione)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_movimenti_data ON movimenti_magazzino (data_ora)")


if __name__ == '__main__':
    inizializza_database()