"""Test di regressione headless (stdlib unittest, nessuna dipendenza esterna).

Si eseguono con:  python -m unittest test_gestionale -v
Non serve un display: qui non si importa main.py, si esercita la logica di
magazzino e il modulo di aggiornamento.
"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

import aggiornamento
import inventario
from inventario import BOX, NEGOZIO, ValoreNonValido, parse_prezzo, parse_quantita
from setup_db import inizializza_database


class BaseDB(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute("PRAGMA foreign_keys=ON")
        inizializza_database(self.conn)
        self.conn.execute(
            "INSERT INTO articoli (codice, descrizione, prezzo_acquisto, prezzo_vendita) VALUES ('A1', 'Maglia', 5.0, 12.0)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def movimento(self, tipo, quantita, origine=None, destinazione=None, codice='A1', passivo=0):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_origine, id_deposito_destinazione, tipo, storico_passivo) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (codice, quantita, origine, destinazione, tipo, passivo))
        self.conn.commit()
        return cur.lastrowid


class TestParsing(unittest.TestCase):
    def test_prezzo_valido(self):
        self.assertEqual(parse_prezzo("12.50"), 12.50)
        self.assertEqual(parse_prezzo("12,50"), 12.50)
        self.assertEqual(parse_prezzo(" € 12,50 "), 12.50)
        self.assertEqual(parse_prezzo("7"), 7.0)

    def test_prezzo_vuoto_e_zero(self):
        self.assertEqual(parse_prezzo(""), 0.0)
        self.assertEqual(parse_prezzo("   "), 0.0)
        self.assertEqual(parse_prezzo(None), 0.0)

    def test_prezzo_invalido_solleva(self):
        # Prima "12.5.0" o "abc" diventavano silenziosamente 0.00 e l'articolo
        # finiva in vendita gratis: adesso l'operatore viene fermato.
        for cattivo in ("abc", "12.5.0", "12€50", "--3"):
            with self.assertRaises(ValoreNonValido, msg=cattivo):
                parse_prezzo(cattivo, "Prezzo")

    def test_prezzo_negativo_rifiutato(self):
        with self.assertRaises(ValoreNonValido):
            parse_prezzo("-4.00")

    def test_quantita(self):
        self.assertEqual(parse_quantita("3"), 3)
        self.assertEqual(parse_quantita("0"), 0)
        with self.assertRaises(ValoreNonValido):
            parse_quantita("")
        with self.assertRaises(ValoreNonValido):
            parse_quantita("2.5")
        with self.assertRaises(ValoreNonValido):
            parse_quantita("-1", minimo=0)


class TestGiacenze(BaseDB):
    def test_carico_e_scarico(self):
        self.movimento(inventario.CARICO, 10, destinazione=NEGOZIO)
        self.assertEqual(inventario.giacenza(self.conn, 'A1', NEGOZIO), 10)
        self.movimento(inventario.SCARICO, 3, origine=NEGOZIO)
        self.assertEqual(inventario.giacenza(self.conn, 'A1', NEGOZIO), 7)

    def test_depositi_separati(self):
        self.movimento(inventario.CARICO, 10, destinazione=NEGOZIO)
        self.movimento(inventario.CARICO, 4, destinazione=BOX)
        self.assertEqual(inventario.giacenze(self.conn, 'A1'), (10, 4))

    def test_trasferimento_conserva_il_totale(self):
        self.movimento(inventario.CARICO, 10, destinazione=BOX)
        self.movimento(inventario.TRASFERIMENTO, 4, origine=BOX, destinazione=NEGOZIO)
        neg, box = inventario.giacenze(self.conn, 'A1')
        self.assertEqual((neg, box), (4, 6))
        self.assertEqual(neg + box, 10)

    def test_storico_passivo_non_conta(self):
        self.movimento(inventario.CARICO, 10, destinazione=NEGOZIO)
        self.movimento(inventario.SCARICO, 4, origine=NEGOZIO, passivo=1)
        self.assertEqual(inventario.giacenza(self.conn, 'A1', NEGOZIO), 10)

    def test_annullamento_ripristina_la_giacenza(self):
        self.movimento(inventario.CARICO, 10, destinazione=NEGOZIO)
        id_mov = self.movimento(inventario.SCARICO, 4, origine=NEGOZIO)
        self.assertEqual(inventario.giacenza(self.conn, 'A1', NEGOZIO), 6)
        self.conn.execute("UPDATE movimenti_magazzino SET storico_passivo = 1 WHERE id = ?", (id_mov,))
        self.conn.commit()
        self.assertEqual(inventario.giacenza(self.conn, 'A1', NEGOZIO), 10)
        # Il movimento resta nello storico: non e' stato cancellato.
        resto = self.conn.execute("SELECT COUNT(*) FROM movimenti_magazzino WHERE id = ?", (id_mov,)).fetchone()
        self.assertEqual(resto[0], 1)

    def test_articolo_senza_movimenti(self):
        self.assertEqual(inventario.giacenze(self.conn, 'A1'), (0, 0))

    def test_codice_inesistente(self):
        self.assertEqual(inventario.giacenza(self.conn, 'NONESISTE', NEGOZIO), 0)


class TestRettifiche(BaseDB):
    def test_rettifica_positiva(self):
        cur = self.conn.cursor()
        inventario.registra_rettifica(cur, 'A1', NEGOZIO, 5, 'test')
        self.conn.commit()
        self.assertEqual(inventario.giacenza(self.conn, 'A1', NEGOZIO), 5)
        tipo = self.conn.execute("SELECT tipo FROM movimenti_magazzino").fetchone()[0]
        self.assertEqual(tipo, inventario.RETTIFICA_POSITIVA)

    def test_rettifica_negativa(self):
        self.movimento(inventario.CARICO, 10, destinazione=NEGOZIO)
        cur = self.conn.cursor()
        inventario.registra_rettifica(cur, 'A1', NEGOZIO, -4, 'test')
        self.conn.commit()
        self.assertEqual(inventario.giacenza(self.conn, 'A1', NEGOZIO), 6)

    def test_delta_zero_non_scrive(self):
        cur = self.conn.cursor()
        self.assertIsNone(inventario.registra_rettifica(cur, 'A1', NEGOZIO, 0))
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM movimenti_magazzino").fetchone()[0], 0)

    def test_vendita_forzata_senza_pagamento_non_lascia_stock(self):
        """Il bug dello stock fantasma: la rettifica non deve esistere finche'
        la vendita non viene pagata."""
        self.movimento(inventario.CARICO, 2, destinazione=NEGOZIO)
        carrello = [{'codice': 'A1', 'qta': 5, 'origine': NEGOZIO, 'rettifica': 3}]
        # L'operatore svuota il carrello: nessuna scrittura, giacenza invariata.
        carrello.clear()
        self.assertEqual(inventario.giacenza(self.conn, 'A1', NEGOZIO), 2)

    def test_vendita_forzata_pagata_azzera_la_giacenza(self):
        self.movimento(inventario.CARICO, 2, destinazione=NEGOZIO)
        cur = self.conn.cursor()
        inventario.registra_rettifica(cur, 'A1', NEGOZIO, 3, 'forzatura')
        cur.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_origine, tipo) VALUES (?, ?, ?, ?)",
                    ('A1', 5, NEGOZIO, inventario.SCARICO))
        self.conn.commit()
        self.assertEqual(inventario.giacenza(self.conn, 'A1', NEGOZIO), 0)


class TestCarrello(unittest.TestCase):
    def test_quantita_in_carrello(self):
        carrello = [{'codice': 'A1', 'qta': 2}, {'codice': 'A1', 'qta': 3}, {'codice': 'B2', 'qta': 9}]
        self.assertEqual(inventario.quantita_in_carrello(carrello, 'A1'), 5)
        self.assertEqual(inventario.quantita_in_carrello(carrello, 'ZZ'), 0)


class TestSoftDelete(BaseDB):
    def test_archiviazione_conserva_i_movimenti(self):
        self.movimento(inventario.CARICO, 10, destinazione=NEGOZIO)
        self.movimento(inventario.SCARICO, 10, origine=NEGOZIO)
        self.conn.execute("UPDATE articoli SET attivo = 0 WHERE codice = 'A1'")
        self.conn.commit()
        movimenti = self.conn.execute("SELECT COUNT(*) FROM movimenti_magazzino WHERE codice = 'A1'").fetchone()[0]
        self.assertEqual(movimenti, 2)
        attivi = self.conn.execute("SELECT COUNT(*) FROM articoli WHERE attivo = 1").fetchone()[0]
        self.assertEqual(attivi, 0)

    def test_ripristino(self):
        self.conn.execute("UPDATE articoli SET attivo = 0 WHERE codice = 'A1'")
        self.conn.execute("UPDATE articoli SET attivo = 1 WHERE codice = 'A1'")
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT attivo FROM articoli WHERE codice = 'A1'").fetchone()[0], 1)


class TestMigrazioni(unittest.TestCase):
    def test_idempotenza(self):
        conn = sqlite3.connect(':memory:')
        inizializza_database(conn)
        inizializza_database(conn)  # non deve sollevare
        colonne = [r[1] for r in conn.execute("PRAGMA table_info(articoli)")]
        self.assertIn('attivo', colonne)
        self.assertEqual(colonne.count('attivo'), 1)
        viste = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")]
        self.assertIn('v_giacenze', viste)
        conn.close()

    def test_migrazione_da_schema_vecchio(self):
        """Un database preesistente senza la colonna attivo deve migrare."""
        conn = sqlite3.connect(':memory:')
        conn.execute("CREATE TABLE articoli (codice TEXT PRIMARY KEY, descrizione TEXT NOT NULL)")
        conn.commit()
        inizializza_database(conn)
        colonne = [r[1] for r in conn.execute("PRAGMA table_info(articoli)")]
        for attesa in ('attivo', 'prezzo_acquisto', 'prezzo_vendita', 'soglia_minima'):
            self.assertIn(attesa, colonne)
        conn.close()


class TestFornitoriOrfani(unittest.TestCase):
    def test_riferimenti_orfani_vengono_sganciati(self):
        """Dati reali gia' presenti: movimenti che puntano a fornitori cancellati."""
        conn = sqlite3.connect(':memory:')
        inizializza_database(conn)
        conn.execute("INSERT INTO fornitori (id, ragione_sociale) VALUES (99, 'Sparito')")
        conn.execute("INSERT INTO articoli (codice, descrizione) VALUES ('A1', 'Maglia')")
        conn.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_destinazione, tipo, id_fornitore) "
                     "VALUES ('A1', 5, 1, 1, 99)")
        conn.execute("DELETE FROM fornitori WHERE id = 99")
        conn.commit()
        self.assertTrue(conn.execute("PRAGMA foreign_key_check").fetchall())

        inizializza_database(conn)

        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        riga = conn.execute("SELECT id_fornitore, nome_fornitore_storico FROM movimenti_magazzino").fetchone()
        self.assertIsNone(riga[0])
        self.assertEqual(riga[1], 'Fornitore eliminato')
        # La quantita' non viene toccata: la giacenza resta quella.
        self.assertEqual(inventario.giacenza(conn, 'A1', NEGOZIO), 5)
        conn.close()


class TestBackupOnline(unittest.TestCase):
    def test_backup_da_connessione_dedicata(self):
        """Riproduce esegui_backup: sorgente e destinazione sono connessioni
        separate, cosi' una transazione aperta sulla cassa non blocca il backup."""
        cartella = tempfile.mkdtemp()
        try:
            percorso = os.path.join(cartella, 'src.db')
            cassa = sqlite3.connect(percorso)
            cassa.execute("PRAGMA journal_mode=WAL")
            inizializza_database(cassa)
            cassa.execute("INSERT INTO articoli (codice, descrizione) VALUES ('X', 'Prova')")
            cassa.commit()

            # Transazione aperta e non committata sul thread della cassa.
            cassa.execute("INSERT INTO articoli (codice, descrizione) VALUES ('Y', 'Non committato')")

            destinazione_path = os.path.join(cartella, 'backup.db')
            sorgente = sqlite3.connect(percorso, timeout=5)
            destinazione = sqlite3.connect(destinazione_path)
            try:
                sorgente.backup(destinazione)
            finally:
                destinazione.close()
                sorgente.close()

            cassa.rollback()
            cassa.close()

            verifica = sqlite3.connect(destinazione_path)
            self.assertEqual(verifica.execute("PRAGMA integrity_check").fetchone()[0], 'ok')
            codici = [r[0] for r in verifica.execute("SELECT codice FROM articoli ORDER BY codice")]
            # Solo il dato committato: la scrittura in volo non deve comparire.
            self.assertEqual(codici, ['X'])
            verifica.close()
        finally:
            shutil.rmtree(cartella, ignore_errors=True)


class TestAggiornamento(unittest.TestCase):
    """Esercita il modulo aggiornamento su repository git temporanei."""

    def setUp(self):
        self.cartella = tempfile.mkdtemp()
        self.remoto = os.path.join(self.cartella, 'remoto')
        self.locale = os.path.join(self.cartella, 'locale')
        self._git_init()
        # aggiornamento usa BASE_DIR a livello di modulo: lo puntiamo al clone.
        self._base_originale = aggiornamento.BASE_DIR
        aggiornamento.BASE_DIR = self.locale

    def tearDown(self):
        aggiornamento.BASE_DIR = self._base_originale
        shutil.rmtree(self.cartella, ignore_errors=True)

    def _run(self, *args, cwd):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)

    def _git_init(self):
        os.makedirs(self.remoto)
        self._run('git', 'init', '--quiet', '--initial-branch=main', cwd=self.remoto)
        self._run('git', 'config', 'user.email', 'test@example.invalid', cwd=self.remoto)
        self._run('git', 'config', 'user.name', 'Test', cwd=self.remoto)
        with open(os.path.join(self.remoto, 'main.py'), 'w') as f:
            f.write("versione = 1\n")
        self._run('git', 'add', '.', cwd=self.remoto)
        self._run('git', 'commit', '--quiet', '-m', 'primo commit', cwd=self.remoto)
        self._run('git', 'clone', '--quiet', self.remoto, self.locale, cwd=self.cartella)
        self._run('git', 'config', 'user.email', 'test@example.invalid', cwd=self.locale)
        self._run('git', 'config', 'user.name', 'Test', cwd=self.locale)

    def _commit_su_remoto(self, testo, messaggio):
        with open(os.path.join(self.remoto, 'main.py'), 'w') as f:
            f.write(testo)
        self._run('git', 'commit', '--quiet', '-am', messaggio, cwd=self.remoto)

    def test_gia_aggiornato(self):
        esito = aggiornamento.controlla()
        self.assertEqual(esito.stato, aggiornamento.AGGIORNATO)
        self.assertFalse(esito.aggiornabile)

    def test_rileva_nuovi_commit(self):
        self._commit_su_remoto("versione = 2\n", 'seconda versione')
        esito = aggiornamento.controlla()
        self.assertEqual(esito.stato, aggiornamento.DISPONIBILE)
        self.assertEqual(len(esito.commits), 1)
        self.assertIn('seconda versione', esito.commits[0])
        self.assertTrue(esito.aggiornabile)

    def test_applica_fa_fast_forward(self):
        self._commit_su_remoto("versione = 2\n", 'seconda versione')
        aggiornamento.controlla()
        ok, messaggio, precedente = aggiornamento.applica()
        self.assertTrue(ok, messaggio)
        with open(os.path.join(self.locale, 'main.py')) as f:
            self.assertEqual(f.read(), "versione = 2\n")
        self.assertTrue(precedente)

    def test_rollback_riporta_indietro(self):
        self._commit_su_remoto("versione = 2\n", 'seconda versione')
        aggiornamento.controlla()
        ok, _, precedente = aggiornamento.applica()
        self.assertTrue(ok)
        ok_rb, _ = aggiornamento.rollback(precedente)
        self.assertTrue(ok_rb)
        with open(os.path.join(self.locale, 'main.py')) as f:
            self.assertEqual(f.read(), "versione = 1\n")

    def test_modifiche_locali_bloccano_aggiornamento(self):
        """Il caso che protegge il lavoro non committato dell'utente."""
        self._commit_su_remoto("versione = 2\n", 'seconda versione')
        with open(os.path.join(self.locale, 'main.py'), 'w') as f:
            f.write("versione = 1  # modifica locale\n")
        esito = aggiornamento.controlla()
        self.assertEqual(esito.stato, aggiornamento.DISPONIBILE)
        self.assertTrue(esito.modifiche_locali)
        self.assertFalse(esito.aggiornabile)

        ok, messaggio, _ = aggiornamento.applica()
        self.assertFalse(ok)
        self.assertIn('modifiche locali', messaggio.lower())
        with open(os.path.join(self.locale, 'main.py')) as f:
            self.assertIn('modifica locale', f.read())

    def test_history_divergente_non_aggiorna(self):
        self._commit_su_remoto("versione = 2\n", 'remoto')
        with open(os.path.join(self.locale, 'main.py'), 'w') as f:
            f.write("versione = 99\n")
        self._run('git', 'commit', '--quiet', '-am', 'locale divergente', cwd=self.locale)
        esito = aggiornamento.controlla()
        self.assertEqual(esito.stato, aggiornamento.DIVERGENTE)
        ok, _, _ = aggiornamento.applica()
        self.assertFalse(ok)

    def test_commit_locali_non_pubblicati(self):
        with open(os.path.join(self.locale, 'main.py'), 'w') as f:
            f.write("versione = 1.5\n")
        self._run('git', 'commit', '--quiet', '-am', 'solo locale', cwd=self.locale)
        esito = aggiornamento.controlla()
        self.assertEqual(esito.stato, aggiornamento.AVANTI)

    def test_cartella_non_git(self):
        aggiornamento.BASE_DIR = self.cartella + '/vuota'
        os.makedirs(aggiornamento.BASE_DIR, exist_ok=True)
        esito = aggiornamento.controlla()
        self.assertEqual(esito.stato, aggiornamento.NON_DISPONIBILE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
