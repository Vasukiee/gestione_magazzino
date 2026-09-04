"""Percorsi e factory di connessione condivisi.

Tutti i moduli del gestionale devono importare DB_PATH e connetti() da qui:
i percorsi sono ancorati alla cartella del progetto, non alla working
directory, cosi' l'app funziona anche se lanciata da un'altra posizione
(prima creava silenziosamente un database vuoto).
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'magazzino.db')
BACKUP_DIR = os.path.join(BASE_DIR, 'backup')
BACKUP_ROLLING_DIR = os.path.join(BACKUP_DIR, 'rolling')
BACKUP_DAILY_DIR = os.path.join(BACKUP_DIR, 'daily')
LOG_PATH = os.path.join(BASE_DIR, 'gestionale.log')

# Timeout di attesa su lock del database, in secondi.
DB_TIMEOUT = 30


def connetti(check_same_thread=True):
    """Apre una connessione configurata: WAL, busy_timeout e foreign key attive.

    WAL permette letture concorrenti mentre si scrive ed e' necessario perche'
    il backup online non blocchi la cassa; busy_timeout evita che un lock
    momentaneo faccia fallire una vendita con 'database is locked'.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=check_same_thread, timeout=DB_TIMEOUT)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT * 1000}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def assicura_cartelle():
    for cartella in (BACKUP_DIR, BACKUP_ROLLING_DIR, BACKUP_DAILY_DIR):
        os.makedirs(cartella, exist_ok=True)
