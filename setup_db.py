import sqlite3

def inizializza_database():
    conn = sqlite3.connect('magazzino.db')
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
                                                           soglia_minima INTEGER DEFAULT 2
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

    conn.commit()
    conn.close()
    print("Infrastruttura database aggiornata con storico passivo.")


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


if __name__ == '__main__':
    inizializza_database()