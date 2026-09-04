import tkinter as tk
from tkinter import messagebox, filedialog
import ttkbootstrap as ttk
import sqlite3
import datetime
import csv
import threading
import os
import glob
import logging
import signal
import sys
from contextlib import contextmanager

import aggiornamento
import inventario
from db import (BACKUP_DAILY_DIR, BACKUP_DIR, BACKUP_ROLLING_DIR, DB_PATH,
                DB_TIMEOUT, LOG_PATH, assicura_cartelle, connetti)
from inventario import (BOX, NEGOZIO, ValoreNonValido, parse_prezzo,
                        parse_quantita)
from setup_db import inizializza_database
from stampa_etichetta_brother import stampa_etichetta_articolo

logger = logging.getLogger('gestionale')

# Quanti backup rolling conservare e ogni quanti secondi farli.
MAX_BACKUP_ROLLING = 16
INTERVALLO_BACKUP = 1800


def configura_logging():
    """Log su file: in una GUI un print() su stderr non lo legge nessuno."""
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )


class TerminaleMagazzino:
    def __init__(self, root):
        self.root = root
        self.root.title("Gestione Magazzino")
        self.root.geometry("1300x750")

        self.conn = connetti(check_same_thread=False)
        # La connessione e' condivisa con il thread di backup: ogni scrittura e
        # ogni backup passano da questo lock.
        self.db_lock = threading.RLock()
        self.tipo_movimento = tk.IntVar(value=1)
        self.var_qta = tk.IntVar(value=1)
        self.var_qta_trasf = tk.IntVar(value=1)
        self.lista_fornitori = []

        self.carrello = []
        self.totale_carrello_str = tk.StringVar(value="€ 0.00")
        self.totale_carrello_val = 0.0
        self.movimenti_log = {}  # id riga tree_log -> id movimento in movimenti_magazzino (per annullamento)
        self.mostra_archiviati = tk.BooleanVar(value=False)
        self.stato_backup = tk.StringVar(value="Backup: nessuno ancora in questa sessione.")
        self.stato_aggiornamento = tk.StringVar(value="Aggiornamenti: controllo non ancora eseguito.")
        self._stop_backup = threading.Event()
        self._in_chiusura = False

        assicura_cartelle()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        signal.signal(signal.SIGTERM, self.handle_sigterm)

        self.setup_ui()

        self.backup_thread = threading.Thread(target=self.worker_backup_rolling, daemon=True)
        self.backup_thread.start()

        # Controllo aggiornamenti in background: la rete non deve mai ritardare
        # l'apertura della cassa.
        self.controlla_aggiornamenti(silenzioso=True)

    # ------------------------------------------------------------------
    # Accesso al database
    # ------------------------------------------------------------------

    @contextmanager
    def transazione(self):
        """Esegue un blocco di scritture come una sola transazione.

        Il commit avviene solo se il blocco termina senza eccezioni; in caso
        contrario si fa rollback. Prima gli except mostravano l'errore ma non
        annullavano nulla, e le scritture gia' riuscite restavano pendenti sulla
        connessione condivisa finche' un commit successivo, di tutt'altra
        operazione, le portava a bordo.
        """
        with self.db_lock:
            cursor = self.conn.cursor()
            try:
                yield cursor
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def scrivi(self, azione, titolo_errore="Errore DB"):
        """Esegue azione(cursor) in transazione, mostrando gli errori DB all'operatore.

        Ritorna (ok, risultato): risultato e' il valore restituito da azione.
        """
        try:
            with self.transazione() as cursor:
                risultato = azione(cursor)
            return True, risultato
        except ValoreNonValido as err:
            messagebox.showerror("Valore non valido", str(err))
        except sqlite3.Error as err:
            logger.exception("Errore database durante %s", getattr(azione, '__name__', azione))
            messagebox.showerror(titolo_errore, str(err))
        return False, None

    def leggi(self, sql, parametri=()):
        with self.db_lock:
            return self.conn.execute(sql, parametri).fetchall()

    def filtro_attivi(self, alias='a'):
        """Clausola per escludere gli articoli archiviati, se non richiesti."""
        return '' if self.mostra_archiviati.get() else f' AND {alias}.attivo = 1'

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    def esegui_backup(self, destinazione):
        """Copia coerente del database tramite l'API di backup online di SQLite.

        shutil.copy2 su un file SQLite aperto puo' produrre una copia lacerata
        (scrittura in corso sul thread principale) e nessuno se ne accorge
        finche' non serve ripristinarla. sqlite3.Connection.backup() gestisce il
        locking; os.replace rende atomica la sostituzione del file finale.
        """
        temporaneo = destinazione + '.tmp'
        # Il backup parte da una connessione dedicata, non da self.conn: in WAL
        # un secondo lettore vede uno snapshot coerente dei dati committati e
        # non entra in conflitto con una transazione aperta sul thread della
        # cassa (backup() sulla stessa connessione occupata riprova all'infinito).
        sorgente = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
        dest_conn = sqlite3.connect(temporaneo)
        try:
            sorgente.backup(dest_conn)
        finally:
            dest_conn.close()
            sorgente.close()
        os.replace(temporaneo, destinazione)
        return destinazione

    def worker_backup_rolling(self):
        # Event.wait invece di time.sleep: alla chiusura il thread esce subito
        # invece di restare fermo fino a mezz'ora.
        while not self._stop_backup.wait(INTERVALLO_BACKUP):
            try:
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                self.esegui_backup(os.path.join(BACKUP_ROLLING_DIR, f'backup_{timestamp}.db'))

                lista_file = sorted(glob.glob(os.path.join(BACKUP_ROLLING_DIR, 'backup_*.db')),
                                    key=os.path.getmtime)
                while len(lista_file) > MAX_BACKUP_ROLLING:
                    os.remove(lista_file.pop(0))

                ora = datetime.datetime.now().strftime('%H:%M')
                self.stato_backup.set(f"Backup: ultimo eseguito alle {ora}.")
                logger.info("Backup rolling eseguito alle %s", ora)
            except (sqlite3.Error, OSError) as e:
                self.stato_backup.set(f"Backup: ERRORE ({e}). Controllare {os.path.basename(LOG_PATH)}.")
                logger.exception("Errore backup rolling")

    def on_closing(self):
        if self._in_chiusura:
            return
        self._in_chiusura = True
        self._stop_backup.set()
        try:
            self.esegui_backup(os.path.join(BACKUP_DIR, 'latest_backup.db'))
            data_odierna = datetime.datetime.now().strftime("%Y%m%d")
            self.esegui_backup(os.path.join(BACKUP_DAILY_DIR, f'backup_{data_odierna}.db'))
        except (sqlite3.Error, OSError) as e:
            logger.exception("Errore backup di chiusura")
            print(f"Errore backup chiusura: {e}")
        finally:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.root.destroy()
            sys.exit(0)

    def handle_sigterm(self, signum, frame):
        self.on_closing()

    def setup_ui(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.tab_movimenti = ttk.Frame(self.notebook)
        self.tab_trasferimenti = ttk.Frame(self.notebook)
        self.tab_ricerca = ttk.Frame(self.notebook)
        self.tab_statistiche = ttk.Frame(self.notebook)
        self.tab_fornitori = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_movimenti, text=" Operatività e Movimenti ")
        self.notebook.add(self.tab_trasferimenti, text=" Trasferimenti Interni ")
        self.notebook.add(self.tab_ricerca, text=" Ricerca e Storico ")
        self.notebook.add(self.tab_statistiche, text=" Statistiche ")
        self.notebook.add(self.tab_fornitori, text=" Fornitori ")

        self.setup_tab_movimenti()
        self.setup_tab_trasferimenti()
        self.setup_tab_ricerca()
        self.setup_tab_statistiche()
        self.setup_tab_fornitori()

        self.notebook.bind('<<NotebookTabChanged>>', self.on_tab_changed)

    def setup_tab_movimenti(self):
        paned = ttk.Panedwindow(self.tab_movimenti, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, pady=(0, 0))

        left_frame = ttk.Frame(paned)
        paned.add(left_frame, weight=3)

        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=2)

        mod_frame = ttk.LabelFrame(left_frame, text="Seleziona Operazione")
        mod_frame.pack(fill=tk.X, pady=(0, 20), ipadx=15, ipady=15)

        ttk.Radiobutton(mod_frame, text="Scarico (Vendita)", variable=self.tipo_movimento, value=2, command=self.on_tipo_movimento_changed).pack(side=tk.LEFT, padx=15)
        ttk.Radiobutton(mod_frame, text="Carico (Arrivo)", variable=self.tipo_movimento, value=1, command=self.on_tipo_movimento_changed).pack(side=tk.LEFT, padx=15)
        ttk.Radiobutton(mod_frame, text="Reso da Cliente", variable=self.tipo_movimento, value=3, command=self.on_tipo_movimento_changed).pack(side=tk.LEFT, padx=15)
        ttk.Radiobutton(mod_frame, text="Reso a Fornitore", variable=self.tipo_movimento, value=4, command=self.on_tipo_movimento_changed).pack(side=tk.LEFT, padx=15)

        self.doc_frame = ttk.Frame(left_frame)
        ttk.Label(self.doc_frame, text="Fornitore:", font=('Helvetica', 12)).pack(side=tk.LEFT, padx=(0, 10))
        self.combo_fornitore = ttk.Combobox(self.doc_frame, state="readonly", font=('Helvetica', 12), width=25)
        self.combo_fornitore.pack(side=tk.LEFT, padx=(0, 20))

        ttk.Label(self.doc_frame, text="N. Bolla:", font=('Helvetica', 12)).pack(side=tk.LEFT, padx=(0, 10))
        self.entry_bolla = ttk.Entry(self.doc_frame, font=('Helvetica', 12), width=20)
        self.entry_bolla.pack(side=tk.LEFT)

        self.scan_frame = ttk.Frame(left_frame)
        self.scan_frame.pack(fill=tk.X, pady=10)

        ttk.Label(self.scan_frame, text="Codice:", font=('Helvetica', 16, 'bold')).pack(side=tk.LEFT, padx=(0, 10))
        self.entry_codice = ttk.Entry(self.scan_frame, font=('Helvetica', 18), width=20)
        self.entry_codice.pack(side=tk.LEFT)

        ttk.Label(self.scan_frame, text="Q.tà:", font=('Helvetica', 16, 'bold')).pack(side=tk.LEFT, padx=(20, 10))
        spin_qta = ttk.Spinbox(self.scan_frame, from_=1, to=9999, textvariable=self.var_qta, width=5, font=('Helvetica', 18))
        spin_qta.pack(side=tk.LEFT)

        self.entry_codice.focus()
        self.entry_codice.bind('<Return>', self.avvia_registrazione)
        self.entry_codice.bind('<FocusOut>', self.mantieni_focus_scanner)

        log_frame = ttk.LabelFrame(left_frame, text="Ultimi Movimenti Registrati")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(20, 0), ipadx=10, ipady=10)

        colonne = ('ora', 'operazione', 'qta', 'codice', 'esito')
        self.tree_log = ttk.Treeview(log_frame, columns=colonne, show='headings', height=10)
        self.tree_log.heading('ora', text='Ora')
        self.tree_log.heading('operazione', text='Operazione')
        self.tree_log.heading('qta', text='Q.tà')
        self.tree_log.heading('codice', text='Codice Letto')
        self.tree_log.heading('esito', text='Esito / Descrizione')

        self.tree_log.column('ora', width=90, anchor=tk.CENTER)
        self.tree_log.column('operazione', width=130, anchor=tk.CENTER)
        self.tree_log.column('qta', width=60, anchor=tk.CENTER)
        self.tree_log.column('codice', width=150, anchor=tk.CENTER)
        self.tree_log.column('esito', width=250, anchor=tk.W)
        self.tree_log.tag_configure('rimosso', foreground='red')
        self.tree_log.tag_configure('annullato', foreground='red')
        self.tree_log.pack(fill=tk.BOTH, expand=True)
        self.tree_log.bind('<Double-1>', self.annulla_movimento_log)

        cart_frame = ttk.LabelFrame(right_frame, text="Carrello Attuale (Solo Scarico/Vendita)")
        cart_frame.pack(fill=tk.BOTH, expand=True, padx=(10, 0), pady=(0, 0), ipadx=10, ipady=10)

        colonne_cart = ('desc', 'qta', 'prezzo', 'totale')
        self.tree_cart = ttk.Treeview(cart_frame, columns=colonne_cart, show='headings', height=10)
        self.tree_cart.heading('desc', text='Articolo')
        self.tree_cart.heading('qta', text='Q.tà')
        self.tree_cart.heading('prezzo', text='Prezzo Unit.')
        self.tree_cart.heading('totale', text='Totale')

        self.tree_cart.column('desc', width=150, anchor=tk.W)
        self.tree_cart.column('qta', width=50, anchor=tk.CENTER)
        self.tree_cart.column('prezzo', width=80, anchor=tk.E)
        self.tree_cart.column('totale', width=80, anchor=tk.E)
        self.tree_cart.pack(fill=tk.BOTH, expand=True)

        self.tree_cart.bind('<Double-1>', self.rimuovi_articolo_carrello)
        self.tree_cart.bind('<Delete>', self.rimuovi_articolo_carrello)

        lbl_totale_text = ttk.Label(cart_frame, text="Totale Carrello:", font=('Helvetica', 16))
        lbl_totale_text.pack(pady=(10, 0))

        lbl_totale = ttk.Label(cart_frame, textvariable=self.totale_carrello_str, font=('Helvetica', 36, 'bold'), foreground='green')
        lbl_totale.pack(pady=(0, 20))

        btn_frame = ttk.Frame(cart_frame)
        btn_frame.pack(fill=tk.X)

        btn_contanti = ttk.Button(btn_frame, text="Contanti", command=lambda: self.esegui_pagamento("Contanti"), bootstyle="success-lg")
        btn_contanti.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

        btn_pos = ttk.Button(btn_frame, text="POS", command=lambda: self.esegui_pagamento("POS"), bootstyle="info-lg")
        btn_pos.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

        btn_rimuovi = ttk.Button(btn_frame, text="Rimuovi", command=self.rimuovi_articolo_carrello, bootstyle="warning-lg")
        btn_rimuovi.pack(side=tk.LEFT, padx=5)

        btn_svuota = ttk.Button(btn_frame, text="Svuota", command=self.svuota_carrello, bootstyle="danger-lg")
        btn_svuota.pack(side=tk.LEFT, padx=5)

        self.on_tipo_movimento_changed()

    def on_tipo_movimento_changed(self):
        if self.tipo_movimento.get() == 1:
            self.doc_frame.pack(fill=tk.X, pady=(0, 10), before=self.scan_frame)
            self.carica_fornitori()
        else:
            self.doc_frame.pack_forget()

    def carica_fornitori(self):
        try:
            self.lista_fornitori = self.leggi("SELECT id, ragione_sociale FROM fornitori ORDER BY ragione_sociale")
            self.combo_fornitore['values'] = [f[1] for f in self.lista_fornitori]
        except sqlite3.Error:
            logger.exception("Caricamento fornitori fallito")
            self.lista_fornitori = []

    def setup_tab_trasferimenti(self):
        ctrl_frame = ttk.Frame(self.tab_trasferimenti)
        ctrl_frame.pack(fill=tk.X, pady=20)

        ttk.Label(ctrl_frame, text="Da:", font=('Helvetica', 14, 'bold')).pack(side=tk.LEFT, padx=5)
        self.combo_orig = ttk.Combobox(ctrl_frame, values=["Negozio", "Box"], state="readonly", font=('Helvetica', 14), width=10)
        self.combo_orig.set("Box")
        self.combo_orig.pack(side=tk.LEFT, padx=10)

        ttk.Label(ctrl_frame, text="Verso:", font=('Helvetica', 14, 'bold')).pack(side=tk.LEFT, padx=5)
        self.combo_dest = ttk.Combobox(ctrl_frame, values=["Negozio", "Box"], state="readonly", font=('Helvetica', 14), width=10)
        self.combo_dest.set("Negozio")
        self.combo_dest.pack(side=tk.LEFT, padx=10)

        scan_frame = ttk.Frame(self.tab_trasferimenti)
        scan_frame.pack(fill=tk.X, pady=10)

        ttk.Label(scan_frame, text="Codice:", font=('Helvetica', 16, 'bold')).pack(side=tk.LEFT, padx=(0, 10))
        self.entry_codice_trasf = ttk.Entry(scan_frame, font=('Helvetica', 18), width=25)
        self.entry_codice_trasf.pack(side=tk.LEFT)

        ttk.Label(scan_frame, text="Q.tà:", font=('Helvetica', 16, 'bold')).pack(side=tk.LEFT, padx=(20, 10))
        spin_qta_trasf = ttk.Spinbox(scan_frame, from_=1, to=9999, textvariable=self.var_qta_trasf, width=5, font=('Helvetica', 18))
        spin_qta_trasf.pack(side=tk.LEFT)

        self.entry_codice_trasf.bind('<Return>', self.esegui_trasferimento)

    def setup_tab_ricerca(self):
        search_top = ttk.Frame(self.tab_ricerca)
        search_top.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(search_top, text="Cerca:", font=('Helvetica', 12, 'bold')).pack(side=tk.LEFT, padx=(0, 10))
        self.entry_ricerca = ttk.Entry(search_top, font=('Helvetica', 14), width=25)
        self.entry_ricerca.pack(side=tk.LEFT, padx=(0, 10))

        btn_cerca = ttk.Button(search_top, text="Filtra", command=self.esegui_ricerca, bootstyle="primary")
        btn_cerca.pack(side=tk.LEFT, padx=(0, 15))

        btn_esaurimento = ttk.Button(search_top, text="Mostra Articoli in Esaurimento", command=self.mostra_esaurimento, bootstyle="warning")
        btn_esaurimento.pack(side=tk.LEFT, padx=(0, 15))

        btn_esporta_st = ttk.Button(search_top, text="Esporta Storico CSV", command=self.esporta_storico_csv, bootstyle="info")
        btn_esporta_st.pack(side=tk.RIGHT)

        btn_esporta_giac = ttk.Button(search_top, text="Esporta Giacenze CSV", command=self.esporta_giacenze_csv, bootstyle="success")
        btn_esporta_giac.pack(side=tk.RIGHT, padx=(0, 10))

        btn_pulisci = ttk.Button(search_top, text="Archivia Giacenze Zero", command=self.pulizia_zero, bootstyle="danger")
        btn_pulisci.pack(side=tk.RIGHT, padx=(0, 10))

        chk_archiviati = ttk.Checkbutton(search_top, text="Mostra archiviati",
                                         variable=self.mostra_archiviati,
                                         command=self.esegui_ricerca,
                                         bootstyle="round-toggle")
        chk_archiviati.pack(side=tk.RIGHT, padx=(0, 15))

        self.entry_ricerca.bind('<Return>', lambda e: self.esegui_ricerca())

        frame_risultati = ttk.Frame(self.tab_ricerca)
        frame_risultati.pack(fill=tk.BOTH, expand=True)

        colonne = ('codice', 'desc', 'colore', 'taglia', 'prezzo_acq', 'prezzo_ven', 'giac_neg', 'giac_box')
        self.tree_ricerca = ttk.Treeview(frame_risultati, columns=colonne, show='headings', bootstyle="primary")

        self.tree_ricerca.heading('codice', text='Codice')
        self.tree_ricerca.heading('desc', text='Descrizione')
        self.tree_ricerca.heading('colore', text='Colore')
        self.tree_ricerca.heading('taglia', text='Taglia')
        self.tree_ricerca.heading('prezzo_acq', text='Costo Acq.')
        self.tree_ricerca.heading('prezzo_ven', text='Prezzo Vend.')
        self.tree_ricerca.heading('giac_neg', text='Giac. Negozio')
        self.tree_ricerca.heading('giac_box', text='Giac. Box')

        self.tree_ricerca.column('codice', width=110)
        self.tree_ricerca.column('desc', width=220)
        self.tree_ricerca.column('colore', width=90)
        self.tree_ricerca.column('taglia', width=70, anchor=tk.CENTER)
        self.tree_ricerca.column('prezzo_acq', width=80, anchor=tk.E)
        self.tree_ricerca.column('prezzo_ven', width=90, anchor=tk.E)
        self.tree_ricerca.column('giac_neg', width=90, anchor=tk.CENTER)
        self.tree_ricerca.column('giac_box', width=90, anchor=tk.CENTER)
        self.tree_ricerca.tag_configure('archiviato', foreground='grey')
        self.tree_ricerca.pack(fill=tk.BOTH, expand=True)

        self.menu_contestuale = tk.Menu(self.root, tearoff=0)
        self.menu_contestuale.add_command(label="Modifica / Rettifica", command=self.apri_modifica)
        self.menu_contestuale.add_command(label="Archivia Articolo", command=self.elimina_selezionato)
        self.menu_contestuale.add_command(label="Ripristina Articolo", command=self.ripristina_selezionato)
        self.menu_contestuale.add_separator()
        self.menu_contestuale.add_command(label="Stampa Etichetta Barcode", command=self.stampa_etichetta_selezionata)

        self.tree_ricerca.bind("<ButtonRelease-3>", self.mostra_menu_contestuale)
        self.tree_ricerca.bind("<Double-1>", self.on_ricerca_double_click)

    def mostra_menu_contestuale(self, event):
        item = self.tree_ricerca.identify_row(event.y)
        if item:
            self.tree_ricerca.selection_set(item)
            self.tree_ricerca.focus(item)
            self.menu_contestuale.tk_popup(event.x_root, event.y_root)

    def stampa_etichetta_selezionata(self):
        selected = self.tree_ricerca.focus()
        if not selected:
            return
        valori = self.tree_ricerca.item(selected)['values']
        codice = str(valori[0])
        self._chiedi_stampa_etichetta(codice)

    def _fine_stampa_etichetta(self, successo, messaggio):
        if successo:
            messagebox.showinfo("Stampa completata", messaggio)
        else:
            messagebox.showerror("Errore di stampa", messaggio)

    def _chiedi_stampa_etichetta(self, codice: str):
        def esegui():
            ok, msg = stampa_etichetta_articolo(codice)
            self.root.after(0, lambda: self._fine_stampa_etichetta(ok, msg))
        threading.Thread(target=esegui, daemon=True).start()

    def on_ricerca_double_click(self, event):
        item = self.tree_ricerca.identify_row(event.y)
        if not item: return

        self.tree_ricerca.selection_set(item)
        self.tree_ricerca.focus(item)
        self.apri_modifica()

    def apri_modifica(self):
        selected = self.tree_ricerca.focus()
        if not selected: return
        codice = self.tree_ricerca.item(selected)['values'][0]

        # I valori si rileggono dal database, non dalla riga del Treeview:
        # quella e' una fotografia dell'ultima ricerca e una vendita fatta nel
        # frattempo la rende obsoleta, facendo calcolare rettifiche sbagliate.
        riga = self.leggi("""
                          SELECT a.descrizione, a.colore, a.taglia, a.prezzo_acquisto,
                                 a.prezzo_vendita, g.giac_negozio, g.giac_box, a.attivo
                          FROM articoli a JOIN v_giacenze g ON g.codice = a.codice
                          WHERE a.codice = ?
                          """, (codice,))
        if not riga:
            messagebox.showerror("Errore", f"L'articolo {codice} non esiste piu' in anagrafica.")
            self.esegui_ricerca()
            return
        desc_db, col_db, tag_db, acq_db, ven_db, giac_neg_db, giac_box_db, attivo_db = riga[0]

        popup = tk.Toplevel(self.root)
        popup.title(f"Modifica & Rettifica Inventario - {codice}")
        popup.geometry("500x460")
        popup.grab_set()

        form = ttk.Frame(popup)
        form.pack(fill=tk.BOTH, expand=True, ipadx=10, ipady=10)

        campi = ["Descrizione:", "Colore:", "Taglia:", "Costo Acq (€):", "Prezzo Ven (€):", "Giacenza Negozio:", "Giacenza Box:"]
        valori_attuali = [
            desc_db or '',
            col_db or '',
            tag_db or '',
            f"{acq_db or 0:.2f}",
            f"{ven_db or 0:.2f}",
            str(giac_neg_db),
            str(giac_box_db),
        ]
        entries = {}

        for i, label in enumerate(campi):
            ttk.Label(form, text=label, font=('Helvetica', 11, 'bold' if 'Giacenza' in label else 'normal')).grid(row=i, column=0, sticky=tk.E, pady=7)
            ent = ttk.Entry(form, width=30)
            ent.grid(row=i, column=1, pady=7)
            ent.insert(0, valori_attuali[i])
            entries[label] = ent

        if not attivo_db:
            ttk.Label(form, text="Articolo archiviato", bootstyle="warning").grid(row=len(campi), column=1, sticky=tk.W)

        def salva(e=None):
            desc = entries["Descrizione:"].get().strip()
            if not desc:
                messagebox.showwarning("Attenzione", "La descrizione non puo' essere vuota.")
                return

            try:
                p_acq = parse_prezzo(entries["Costo Acq (€):"].get(), "Costo Acquisto")
                p_ven = parse_prezzo(entries["Prezzo Ven (€):"].get(), "Prezzo Vendita")
                nuova_giac_neg = parse_quantita(entries["Giacenza Negozio:"].get(), "Giacenza Negozio")
                nuova_giac_box = parse_quantita(entries["Giacenza Box:"].get(), "Giacenza Box")
            except ValoreNonValido as err:
                messagebox.showerror("Valore non valido", str(err))
                return

            def azione(cursor):
                # Rileggo dentro la transazione: fra l'apertura del popup e il
                # salvataggio possono essere passati minuti e altre vendite.
                corrente = cursor.execute("""
                                          SELECT a.descrizione, a.prezzo_vendita, g.giac_negozio, g.giac_box
                                          FROM articoli a JOIN v_giacenze g ON g.codice = a.codice
                                          WHERE a.codice = ?
                                          """, (codice,)).fetchone()
                if corrente is None:
                    raise sqlite3.Error(f"L'articolo {codice} non esiste piu'.")
                desc_corr, ven_corr, giac_neg_corr, giac_box_corr = corrente

                variazioni = []
                if desc != desc_corr:
                    variazioni.append("Desc")
                if abs(p_ven - (ven_corr or 0.0)) >= 0.005:
                    variazioni.append("Prezzo")

                cursor.execute("""
                               UPDATE articoli SET descrizione = ?, colore = ?, taglia = ?, prezzo_acquisto = ?, prezzo_vendita = ?
                               WHERE codice = ?
                               """, (desc, entries["Colore:"].get().strip(), entries["Taglia:"].get().strip(),
                                     p_acq, p_ven, codice))

                inventario.registra_rettifica(cursor, codice, NEGOZIO,
                                              nuova_giac_neg - giac_neg_corr,
                                              "Rettifica manuale da scheda articolo")
                inventario.registra_rettifica(cursor, codice, BOX,
                                              nuova_giac_box - giac_box_corr,
                                              "Rettifica manuale da scheda articolo")

                if variazioni:
                    nota = "Modificato manualmente: " + ", ".join(variazioni)
                    cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, tipo, riferimento_bolla) VALUES (?, 0, ?, ?)",
                                   (codice, inventario.MODIFICA_ANAGRAFICA, nota))

            ok, _ = self.scrivi(azione)
            if ok:
                popup.destroy()
                self.esegui_ricerca()

        ttk.Button(popup, text="Salva Modifiche", command=salva, bootstyle="success").pack(pady=10)
        popup.bind('<Return>', salva)

    def elimina_selezionato(self):
        """Archivia l'articolo (soft delete) conservandone lo storico.

        La cancellazione fisica eliminava anche tutti i movimenti: lo storico
        vendite spariva, i report cambiavano a posteriori e le righe di
        transazioni_ restavano orfane. Con attivo = 0 l'articolo sparisce da
        ricerche e statistiche ma i movimenti restano.
        """
        selected = self.tree_ricerca.focus()
        if not selected: return
        codice = self.tree_ricerca.item(selected)['values'][0]

        if not messagebox.askyesno(
                "Archivia Articolo",
                f"Archiviare l'articolo {codice}?\n\n"
                "Sparira' da ricerche, giacenze e statistiche, ma i suoi movimenti "
                "restano nello storico e nei report.\n"
                "Puoi rivederlo spuntando 'Mostra archiviati' e ripristinarlo dal menu destro."):
            return

        ok, _ = self.scrivi(lambda cur: cur.execute(
            "UPDATE articoli SET attivo = 0 WHERE codice = ?", (codice,)))
        if ok:
            self.esegui_ricerca()

    def ripristina_selezionato(self):
        selected = self.tree_ricerca.focus()
        if not selected: return
        codice = self.tree_ricerca.item(selected)['values'][0]
        ok, _ = self.scrivi(lambda cur: cur.execute(
            "UPDATE articoli SET attivo = 1 WHERE codice = ?", (codice,)))
        if ok:
            self.esegui_ricerca()

    def pulizia_zero(self):
        """Archivia in blocco gli articoli con giacenza totale zero."""
        candidati = self.leggi("""
                               SELECT a.codice FROM articoli a
                                                        JOIN v_giacenze g ON g.codice = a.codice
                               WHERE a.attivo = 1 AND (g.giac_negozio + g.giac_box) = 0
                               """)
        if not candidati:
            messagebox.showinfo("Info", "Nessun articolo con giacenza zero da archiviare.")
            return

        if not messagebox.askyesno(
                "Archivia giacenze zero",
                f"Verranno archiviati {len(candidati)} articoli con giacenza totale pari a 0.\n\n"
                "I movimenti e lo storico vendite NON vengono cancellati: gli articoli "
                "spariscono solo da ricerche e statistiche.\n\nProcedere?"):
            return

        def azione(cursor):
            cursor.execute("""
                           UPDATE articoli SET attivo = 0
                           WHERE attivo = 1 AND codice IN (
                               SELECT g.codice FROM v_giacenze g
                               WHERE (g.giac_negozio + g.giac_box) = 0
                           )
                           """)
            return cursor.rowcount

        ok, quanti = self.scrivi(azione)
        if ok:
            messagebox.showinfo("Completato", f"Archiviati {quanti} articoli.")
            self.esegui_ricerca()

    def setup_tab_statistiche(self):
        self.frame_stat = ttk.Frame(self.tab_statistiche)
        self.frame_stat.pack(fill=tk.BOTH, expand=True, ipadx=20, ipady=20)

        self.lbl_tot_pz = ttk.Label(self.frame_stat, text="Caricamento...", font=('Helvetica', 16))
        self.lbl_tot_pz.grid(row=0, column=0, sticky=tk.W, pady=10)

        self.lbl_val_acq = ttk.Label(self.frame_stat, text="", font=('Helvetica', 16))
        self.lbl_val_acq.grid(row=1, column=0, sticky=tk.W, pady=10)

        self.lbl_val_ven = ttk.Label(self.frame_stat, text="", font=('Helvetica', 16))
        self.lbl_val_ven.grid(row=2, column=0, sticky=tk.W, pady=10)

        ttk.Separator(self.frame_stat, orient=tk.HORIZONTAL).grid(row=3, column=0, sticky="ew", pady=20)

        self.lbl_dettaglio = ttk.Label(self.frame_stat, text="", font=('Helvetica', 14))
        self.lbl_dettaglio.grid(row=4, column=0, sticky=tk.W)

        btn_report_anomalie = ttk.Button(self.frame_stat, text="Esporta Report Anomalie CSV", command=self.esporta_report_anomalie_csv, bootstyle="danger")
        btn_report_anomalie.grid(row=5, column=0, sticky=tk.W, pady=(20, 0))

        ttk.Separator(self.frame_stat, orient=tk.HORIZONTAL).grid(row=6, column=0, sticky="ew", pady=20)

        # Stato di backup e aggiornamenti: in una GUI un print() su stderr non
        # lo legge nessuno, e un backup che fallisce da giorni deve vedersi.
        ttk.Label(self.frame_stat, textvariable=self.stato_backup, font=('Helvetica', 11)).grid(row=7, column=0, sticky=tk.W, pady=4)
        ttk.Label(self.frame_stat, textvariable=self.stato_aggiornamento, font=('Helvetica', 11)).grid(row=8, column=0, sticky=tk.W, pady=4)

        btn_frame_manutenzione = ttk.Frame(self.frame_stat)
        btn_frame_manutenzione.grid(row=9, column=0, sticky=tk.W, pady=(15, 0))

        ttk.Button(btn_frame_manutenzione, text="Backup adesso",
                   command=self.backup_manuale, bootstyle="secondary").pack(side=tk.LEFT, padx=(0, 10))
        self.btn_aggiornamenti = ttk.Button(btn_frame_manutenzione, text="Controlla aggiornamenti",
                                            command=lambda: self.controlla_aggiornamenti(silenzioso=False),
                                            bootstyle="info")
        self.btn_aggiornamenti.pack(side=tk.LEFT)

    def backup_manuale(self):
        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
            percorso = self.esegui_backup(os.path.join(BACKUP_ROLLING_DIR, f'backup_{timestamp}.db'))
            self.stato_backup.set(f"Backup: ultimo eseguito alle {datetime.datetime.now().strftime('%H:%M')}.")
            messagebox.showinfo("Backup", f"Backup completato:\n{percorso}")
        except (sqlite3.Error, OSError) as e:
            logger.exception("Backup manuale fallito")
            messagebox.showerror("Backup", f"Backup fallito: {e}")

    # ------------------------------------------------------------------
    # Aggiornamenti da GitHub
    # ------------------------------------------------------------------

    def controlla_aggiornamenti(self, silenzioso=True):
        """Avvia il controllo in un thread: la rete non deve bloccare la cassa."""
        self.stato_aggiornamento.set("Aggiornamenti: controllo in corso...")

        def lavoro():
            esito = aggiornamento.controlla()
            self.root.after(0, lambda: self._mostra_esito_aggiornamento(esito, silenzioso))

        threading.Thread(target=lavoro, daemon=True).start()

    def _mostra_esito_aggiornamento(self, esito, silenzioso):
        etichette = {
            aggiornamento.AGGIORNATO: "Aggiornamenti: nessuno, sei all'ultima versione.",
            aggiornamento.DISPONIBILE: f"Aggiornamenti: {len(esito.commits)} disponibili.",
            aggiornamento.AVANTI: "Aggiornamenti: hai commit locali non pubblicati.",
            aggiornamento.DIVERGENTE: "Aggiornamenti: versione divergente, serve intervento manuale.",
            aggiornamento.NON_DISPONIBILE: "Aggiornamenti: non disponibili (git assente).",
            aggiornamento.ERRORE: "Aggiornamenti: controllo fallito (rete?).",
        }
        self.stato_aggiornamento.set(etichette.get(esito.stato, "Aggiornamenti: stato sconosciuto."))
        logger.info("Controllo aggiornamenti: %s - %s", esito.stato, esito.messaggio)

        if esito.stato == aggiornamento.DISPONIBILE:
            self._popup_aggiornamento(esito)
            return

        # All'avvio si parla solo se c'e' qualcosa da fare: nessun popup da
        # chiudere ogni mattina prima di aprire la cassa.
        if not silenzioso:
            messagebox.showinfo("Aggiornamenti", esito.messaggio or "Nessun aggiornamento disponibile.")

    def _popup_aggiornamento(self, esito):
        popup = tk.Toplevel(self.root)
        popup.title("Aggiornamento disponibile")
        popup.geometry("640x460")
        popup.grab_set()

        ttk.Label(popup, text=f"Sono disponibili {len(esito.commits)} aggiornamenti",
                  font=('Helvetica', 15, 'bold')).pack(pady=(15, 5))
        ttk.Label(popup, text=f"Versione installata: {esito.commit_locale[:8]}  →  nuova: {esito.commit_remoto[:8]}",
                  font=('Helvetica', 10)).pack(pady=(0, 10))

        cornice = ttk.Frame(popup)
        cornice.pack(fill=tk.BOTH, expand=True, padx=15)
        testo = tk.Text(cornice, wrap=tk.WORD, height=12, font=('TkFixedFont', 9))
        scroll = ttk.Scrollbar(cornice, orient=tk.VERTICAL, command=testo.yview)
        testo.configure(yscrollcommand=scroll.set)
        testo.insert('1.0', '\n'.join(esito.commits) or 'Nessun dettaglio disponibile.')
        testo.configure(state=tk.DISABLED)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        testo.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        if esito.modifiche_locali:
            elenco = ', '.join(esito.modifiche_locali[:5])
            ttk.Label(popup,
                      text=("Modifiche locali non committate: l'aggiornamento le sovrascriverebbe.\n"
                            f"{elenco}\nCommittale o annullale prima di aggiornare."),
                      bootstyle="danger", justify=tk.LEFT).pack(pady=10, padx=15, anchor=tk.W)

        btn_frame = ttk.Frame(popup)
        btn_frame.pack(pady=15)

        ttk.Button(btn_frame, text="Più tardi", command=popup.destroy,
                   bootstyle="secondary").pack(side=tk.LEFT, padx=10)
        btn_aggiorna = ttk.Button(btn_frame, text="Aggiorna e riavvia",
                                  command=lambda: self._applica_aggiornamento(popup, btn_aggiorna),
                                  bootstyle="success")
        btn_aggiorna.pack(side=tk.LEFT, padx=10)
        if esito.modifiche_locali:
            btn_aggiorna.configure(state=tk.DISABLED)

    def _applica_aggiornamento(self, popup, bottone):
        bottone.configure(state=tk.DISABLED, text="Aggiornamento in corso...")

        # Backup prima di toccare i file: se il nuovo codice ha una migrazione
        # sbagliata, il database di ieri sera e' ancora recuperabile.
        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
            self.esegui_backup(os.path.join(BACKUP_DAILY_DIR, f'pre_aggiornamento_{timestamp}.db'))
        except (sqlite3.Error, OSError) as e:
            logger.exception("Backup pre-aggiornamento fallito")
            messagebox.showerror("Aggiornamento",
                                 f"Backup preventivo fallito, aggiornamento annullato:\n{e}")
            bottone.configure(state=tk.NORMAL, text="Aggiorna e riavvia")
            return

        ok, messaggio, precedente = aggiornamento.applica()
        logger.info("Aggiornamento applicato=%s: %s", ok, messaggio)

        if not ok:
            messagebox.showerror("Aggiornamento", messaggio)
            bottone.configure(state=tk.NORMAL, text="Aggiorna e riavvia")
            return

        popup.destroy()
        messagebox.showinfo(
            "Aggiornamento completato",
            f"{messaggio}\n\nIl gestionale verrà riavviato.\n\n"
            f"Per tornare indietro: git reset --hard {precedente[:8]}")
        self.riavvia()

    def riavvia(self):
        """Chiude ordinatamente e rilancia il processo con lo stesso interprete."""
        self._stop_backup.set()
        try:
            self.esegui_backup(os.path.join(BACKUP_DIR, 'latest_backup.db'))
        except (sqlite3.Error, OSError):
            logger.exception("Backup pre-riavvio fallito")
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        self._in_chiusura = True
        self.root.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def setup_tab_fornitori(self):
        paned = ttk.Panedwindow(self.tab_fornitori, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, pady=10)

        left_frame = ttk.LabelFrame(paned, text="Anagrafica Fornitori")
        paned.add(left_frame, weight=3)

        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=2)

        self.tree_fornitori = ttk.Treeview(left_frame, columns=('ragione_sociale',), show='headings', bootstyle="primary")
        self.tree_fornitori.heading('ragione_sociale', text='Ragione Sociale')
        self.tree_fornitori.column('ragione_sociale', width=300)
        self.tree_fornitori.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.tree_fornitori.bind('<<TreeviewSelect>>', self.on_seleziona_fornitore)

        form_frame = ttk.LabelFrame(right_frame, text="Nuovo / Modifica Fornitore")
        form_frame.pack(fill=tk.X, padx=(10, 0), pady=10, ipadx=10, ipady=10)

        ttk.Label(form_frame, text="Ragione Sociale:", font=('Helvetica', 12)).pack(anchor=tk.W, pady=(0, 5))
        self.entry_nome_fornitore = ttk.Entry(form_frame, font=('Helvetica', 14), width=30)
        self.entry_nome_fornitore.pack(fill=tk.X, pady=(0, 15))
        self.entry_nome_fornitore.bind('<Return>', lambda e: self.salva_fornitore())

        btn_frame_fornitori = ttk.Frame(form_frame)
        btn_frame_fornitori.pack(fill=tk.X)

        self.btn_salva_fornitore = ttk.Button(btn_frame_fornitori, text="Aggiungi", command=self.salva_fornitore, bootstyle="success")
        self.btn_salva_fornitore.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))

        btn_nuovo_fornitore = ttk.Button(btn_frame_fornitori, text="Annulla / Nuovo", command=self.reset_form_fornitore, bootstyle="secondary")
        btn_nuovo_fornitore.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(5, 0))

        btn_elimina_fornitore = ttk.Button(right_frame, text="Elimina Fornitore Selezionato", command=self.elimina_fornitore, bootstyle="danger")
        btn_elimina_fornitore.pack(fill=tk.X, padx=(10, 0), pady=(0, 10))

        self.fornitore_in_modifica = None
        self.aggiorna_lista_fornitori()

    def aggiorna_lista_fornitori(self):
        for item in self.tree_fornitori.get_children():
            self.tree_fornitori.delete(item)
        for id_forn, nome in self.leggi("SELECT id, ragione_sociale FROM fornitori ORDER BY ragione_sociale"):
            self.tree_fornitori.insert('', tk.END, iid=str(id_forn), values=(nome,))
        # Aggiorna anche il combobox usato nel Carico
        self.carica_fornitori()

    def on_seleziona_fornitore(self, event=None):
        selezione = self.tree_fornitori.selection()
        if not selezione:
            return
        id_forn = int(selezione[0])
        nome = self.tree_fornitori.item(selezione[0], 'values')[0]
        self.fornitore_in_modifica = id_forn
        self.entry_nome_fornitore.delete(0, tk.END)
        self.entry_nome_fornitore.insert(0, nome)
        self.btn_salva_fornitore.config(text="Salva Modifiche")

    def reset_form_fornitore(self):
        self.fornitore_in_modifica = None
        self.entry_nome_fornitore.delete(0, tk.END)
        self.btn_salva_fornitore.config(text="Aggiungi")
        self.tree_fornitori.selection_remove(self.tree_fornitori.selection())

    def salva_fornitore(self):
        nome = self.entry_nome_fornitore.get().strip()
        if not nome:
            messagebox.showwarning("Attenzione", "Inserire la ragione sociale del fornitore.")
            return

        def azione(cursor):
            try:
                if self.fornitore_in_modifica is not None:
                    cursor.execute("UPDATE fornitori SET ragione_sociale = ? WHERE id = ?", (nome, self.fornitore_in_modifica))
                else:
                    cursor.execute("INSERT INTO fornitori (ragione_sociale) VALUES (?)", (nome,))
            except sqlite3.IntegrityError:
                raise sqlite3.Error(f"Esiste già un fornitore con ragione sociale '{nome}'.")

        ok, _ = self.scrivi(azione)
        if not ok:
            return

        self.reset_form_fornitore()
        self.aggiorna_lista_fornitori()

    def elimina_fornitore(self):
        selezione = self.tree_fornitori.selection()
        if not selezione:
            messagebox.showinfo("Info", "Seleziona un fornitore dalla lista da eliminare.")
            return
        id_forn = int(selezione[0])
        nome = self.tree_fornitori.item(selezione[0], 'values')[0]

        righe = self.leggi("SELECT COUNT(*) FROM movimenti_magazzino WHERE id_fornitore = ?", (id_forn,))
        n_movimenti = righe[0][0] if righe else 0

        messaggio = f"Eliminare il fornitore '{nome}'?"
        if n_movimenti > 0:
            messaggio += (f"\n\nQuesto fornitore è collegato a {n_movimenti} movimento/i di carico già registrati. "
                          f"Il nome '{nome}' resterà comunque visibile nello storico, ma non sarà più possibile selezionarlo per nuovi carichi.")

        if not messagebox.askyesno("Elimina Fornitore", messaggio):
            return

        def azione(cursor):
            # Preserva il nome nello storico e libera la foreign key: con
            # PRAGMA foreign_keys attivo la DELETE fallirebbe finché i
            # movimenti puntano ancora al fornitore.
            cursor.execute("UPDATE movimenti_magazzino SET nome_fornitore_storico = ?, id_fornitore = NULL WHERE id_fornitore = ?", (nome, id_forn))
            cursor.execute("DELETE FROM fornitori WHERE id = ?", (id_forn,))

        ok, _ = self.scrivi(azione)
        if not ok:
            return

        self.reset_form_fornitore()
        self.aggiorna_lista_fornitori()

    def mantieni_focus_scanner(self, event=None):
        # Riporta il focus sul campo codice scanner dopo un breve istante,
        # ma solo se siamo sulla tab Operatività e nessun popup (Toplevel) è aperto.
        def ripristina():
            popup_aperto = any(isinstance(w, tk.Toplevel) and w.winfo_exists() for w in self.root.winfo_children())
            if popup_aperto:
                return
            tab_corrente = self.notebook.tab('current')['text']
            if "Operatività" not in tab_corrente:
                return
            try:
                focus_attuale = self.root.focus_get()
            except KeyError:
                # focus_get() può puntare a widget interni effimeri (es. il popdown
                # di un Combobox) non risolvibili da nametowidget: in quel caso
                # consideriamo il focus "altrove" e non lo forziamo.
                return
            # Non strappare il focus da altri campi di input legittimi della stessa tab
            if focus_attuale in (self.entry_bolla, self.combo_fornitore, self.entry_ricerca):
                return
            self.entry_codice.focus_set()
        self.root.after(150, ripristina)

    def on_tab_changed(self, event):
        tab = event.widget.tab('current')['text']
        if "Statistiche" in tab:
            self.aggiorna_statistiche()
        elif "Trasferimenti" in tab:
            self.entry_codice_trasf.focus()
        elif "Operatività" in tab:
            self.entry_codice.focus()

    def esegui_trasferimento(self, event=None):
        codice = self.entry_codice_trasf.get().strip()
        self.entry_codice_trasf.delete(0, tk.END)

        if not codice: return

        try:
            qta = self.var_qta_trasf.get()
            if qta <= 0:
                qta = 1
        except (tk.TclError, ValueError):
            qta = 1

        orig_str = self.combo_orig.get()
        dest_str = self.combo_dest.get()

        if orig_str == dest_str:
            messagebox.showerror("Errore", "Magazzino di origine e destinazione coincidono.")
            return

        orig_id = NEGOZIO if orig_str == "Negozio" else BOX
        dest_id = NEGOZIO if dest_str == "Negozio" else BOX

        righe = self.leggi("SELECT descrizione, colore, taglia, prezzo_acquisto, prezzo_vendita FROM articoli WHERE codice = ?", (codice,))
        if not righe:
            messagebox.showerror("Errore", "Articolo inesistente in anagrafica. Effettuare prima il carico.")
            return
        articolo = righe[0]

        with self.db_lock:
            giac_attuale = inventario.giacenza(self.conn, codice, orig_id)

        ora_attuale = datetime.datetime.now().strftime("%H:%M:%S")
        nome_op = f"Trasferimento a {dest_str}"

        if giac_attuale < qta:
            self.mostra_popup_sottoscorta(codice, orig_id, dest_id, inventario.TRASFERIMENTO,
                                          nome_op, ora_attuale, qta, giac_attuale, articolo)
            return

        self.esegui_query_movimento(codice, orig_id, dest_id, inventario.TRASFERIMENTO, nome_op,
                                    ora_attuale, articolo[0], qta, articolo[1], articolo[2], None, None)

    def aggiorna_statistiche(self):
        sql = f"""
              SELECT SUM(g.giac_negozio), SUM(g.giac_negozio * COALESCE(a.prezzo_acquisto, 0)),
                     SUM(g.giac_negozio * COALESCE(a.prezzo_vendita, 0)),
                     SUM(g.giac_box), SUM(g.giac_box * COALESCE(a.prezzo_acquisto, 0)),
                     SUM(g.giac_box * COALESCE(a.prezzo_vendita, 0))
              FROM articoli a JOIN v_giacenze g ON g.codice = a.codice
              WHERE 1 = 1 {self.filtro_attivi('a')}
              """
        righe = self.leggi(sql)
        row = righe[0] if righe else None

        if not row or row[0] is None:
            self.lbl_tot_pz.config(text="Nessun dato disponibile.")
            self.lbl_val_acq.config(text="")
            self.lbl_val_ven.config(text="")
            self.lbl_dettaglio.config(text="")
            return

        pz_neg, acq_neg, ven_neg, pz_box, acq_box, ven_box = row

        pz_tot = (pz_neg or 0) + (pz_box or 0)
        acq_tot = (acq_neg or 0) + (acq_box or 0)
        ven_tot = (ven_neg or 0) + (ven_box or 0)

        self.lbl_tot_pz.config(text=f"Totale Articoli in Giacenza: {pz_tot}")
        self.lbl_val_acq.config(text=f"Valore Totale d'Acquisto: € {acq_tot:,.2f}")
        self.lbl_val_ven.config(text=f"Valore Totale di Vendita al Pubblico: € {ven_tot:,.2f}")

        dettaglio = (
            f"Dettaglio Negozio:\n"
            f"  - Pezzi: {pz_neg or 0}\n"
            f"  - Valore Acquisto: € {acq_neg or 0:,.2f}\n"
            f"  - Valore Vendita: € {ven_neg or 0:,.2f}\n\n"
            f"Dettaglio Box:\n"
            f"  - Pezzi: {pz_box or 0}\n"
            f"  - Valore Acquisto: € {acq_box or 0:,.2f}\n"
            f"  - Valore Vendita: € {ven_box or 0:,.2f}"
        )
        self.lbl_dettaglio.config(text=dettaglio)

    def esegui_ricerca(self):
        termine = self.entry_ricerca.get().strip()
        # % e _ sono metacaratteri LIKE: senza escape una ricerca di "50%"
        # restituirebbe mezzo catalogo.
        termine = termine.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        query_text = f"%{termine}%"
        sql = f"""
              SELECT a.codice, a.descrizione, a.colore, a.taglia, a.prezzo_acquisto, a.prezzo_vendita,
                     g.giac_negozio, g.giac_box, a.attivo
              FROM articoli a
                       JOIN v_giacenze g ON g.codice = a.codice
              WHERE (a.codice LIKE ? ESCAPE '\\' OR a.descrizione LIKE ? ESCAPE '\\' OR a.colore LIKE ? ESCAPE '\\')
                    {self.filtro_attivi('a')}
              ORDER BY a.descrizione
              """
        righe = self.leggi(sql, (query_text, query_text, query_text))
        self.popola_tree_ricerca(righe)

    def popola_tree_ricerca(self, righe, tag_extra=None):
        for item in self.tree_ricerca.get_children():
            self.tree_ricerca.delete(item)
        for r in righe:
            tags = []
            if tag_extra:
                tags.append(tag_extra)
            if len(r) > 8 and not r[8]:
                tags.append('archiviato')
            self.tree_ricerca.insert('', tk.END,
                                     values=(r[0], r[1], r[2], r[3],
                                             f"€ {r[4] or 0:.2f}", f"€ {r[5] or 0:.2f}", r[6], r[7]),
                                     tags=tuple(tags))

    def mostra_esaurimento(self):
        sql = f"""
              SELECT a.codice, a.descrizione, a.colore, a.taglia, a.prezzo_acquisto, a.prezzo_vendita,
                     g.giac_negozio, g.giac_box, a.attivo
              FROM articoli a
                       JOIN v_giacenze g ON g.codice = a.codice
              WHERE (g.giac_negozio + g.giac_box) <= COALESCE(a.soglia_minima, 0)
                    {self.filtro_attivi('a')}
              ORDER BY a.descrizione
              """
        self.popola_tree_ricerca(self.leggi(sql), tag_extra='danger')

    def esporta_storico_csv(self):
        # I fallback su OperationalError sono stati rimossi: lo schema e'
        # garantito dalle migrazioni in setup_db. Prima un banale
        # 'database is locked' veniva scambiato per uno schema vecchio e la
        # riesecuzione buttava via fornitore e numero bolla.
        sql = """
              SELECT m.data_ora,
                     CASE m.tipo WHEN 1 THEN 'Carico' WHEN 2 THEN 'Scarico' WHEN 3 THEN 'Reso Cliente' WHEN 4 THEN 'Reso Fornitore' WHEN 5 THEN 'Trasferimento Interno' WHEN 6 THEN 'Rettifica Positiva' WHEN 7 THEN 'Rettifica Negativa' WHEN 8 THEN 'Modifica Anagrafica' ELSE 'Altro' END,
                     m.codice, a.descrizione, a.colore, a.taglia, a.prezzo_acquisto, a.prezzo_vendita,
                     COALESCE(d_orig.nome_deposito, '-'), COALESCE(d_dest.nome_deposito, '-'), m.quantita,
                     COALESCE(f.ragione_sociale, m.nome_fornitore_storico, ''), COALESCE(m.riferimento_bolla, ''),
                     CASE WHEN m.storico_passivo = 1 THEN 'Si' ELSE 'No' END
              FROM movimenti_magazzino m
                       LEFT JOIN articoli a ON m.codice = a.codice
                       LEFT JOIN depositi d_orig ON m.id_deposito_origine = d_orig.id
                       LEFT JOIN depositi d_dest ON m.id_deposito_destinazione = d_dest.id
                       LEFT JOIN fornitori f ON m.id_fornitore = f.id
              ORDER BY m.data_ora DESC
              """
        righe = self.leggi(sql)
        if not righe: return messagebox.showinfo("Info", "Nessun movimento.")
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"storico_{datetime.datetime.now().strftime('%Y%m%d')}.csv")
        if path:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Data Ora", "Operazione", "Codice", "Descrizione", "Colore", "Taglia", "Costo", "Prezzo", "Origine", "Destinazione", "Quantità", "Fornitore", "Note / Bolla", "Annullato"])
                writer.writerows(righe)
            messagebox.showinfo("Esportazione", f"Storico esportato: {len(righe)} righe.")

    def esporta_giacenze_csv(self):
        sql = f"""
              SELECT a.codice, a.descrizione, a.colore, a.taglia, a.prezzo_acquisto, a.prezzo_vendita,
                     g.giac_negozio, g.giac_box,
                     CASE WHEN a.attivo = 1 THEN 'No' ELSE 'Si' END
              FROM articoli a JOIN v_giacenze g ON g.codice = a.codice
              WHERE 1 = 1 {self.filtro_attivi('a')}
              ORDER BY a.descrizione
              """
        righe = self.leggi(sql)
        if not righe: return messagebox.showinfo("Info", "Nessun articolo in anagrafica.")
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"inventario_giacenze_{datetime.datetime.now().strftime('%Y%m%d')}.csv")
        if path:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Codice", "Descrizione", "Colore", "Taglia", "Costo Acquisto", "Prezzo Vendita", "Giacenza Negozio", "Giacenza Box", "Giacenza Totale", "Archiviato"])
                for r in righe:
                    writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[6] + r[7], r[8]])
            messagebox.showinfo("Esportazione", f"Giacenze esportate: {len(righe)} righe.")

    def esporta_report_anomalie_csv(self):
        sql = """
              SELECT m.data_ora,
                     CASE m.tipo WHEN 6 THEN 'Rettifica Positiva' WHEN 7 THEN 'Rettifica Negativa' WHEN 8 THEN 'Modifica Anagrafica' END AS operazione,
                     m.codice, a.descrizione, a.colore, a.taglia,
                     COALESCE(d_orig.nome_deposito, '-') AS origine,
                     COALESCE(d_dest.nome_deposito, '-') AS destinazione,
                     m.quantita,
                     COALESCE(m.riferimento_bolla, '') AS note,
                     CASE WHEN m.storico_passivo = 1 THEN 'Si' ELSE 'No' END AS annullato
              FROM movimenti_magazzino m
                       LEFT JOIN articoli a ON m.codice = a.codice
                       LEFT JOIN depositi d_orig ON m.id_deposito_origine = d_orig.id
                       LEFT JOIN depositi d_dest ON m.id_deposito_destinazione = d_dest.id
              WHERE m.tipo IN (6, 7, 8)
              ORDER BY m.data_ora DESC
              """
        righe = self.leggi(sql)
        if not righe:
            return messagebox.showinfo("Info", "Nessuna rettifica o modifica registrata.")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"report_anomalie_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
        )
        if path:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Data Ora", "Operazione", "Codice", "Descrizione", "Colore", "Taglia", "Deposito Origine", "Deposito Destinazione", "Quantità", "Note Aggiuntive", "Annullato"])
                writer.writerows(righe)
            messagebox.showinfo("Esportazione", f"Report anomalie esportato con {len(righe)} righe.")

    def aggiorna_ui_carrello(self):
        for item in self.tree_cart.get_children():
            self.tree_cart.delete(item)

        self.totale_carrello_val = 0.0
        for item in self.carrello:
            totale_riga = item['prezzo'] * item['qta']
            self.totale_carrello_val += totale_riga

            desc_completa = item['desc']
            if item['colore'] or item['taglia']:
                desc_completa += f" ({item['colore'] or ''} {item['taglia'] or ''})".strip()

            self.tree_cart.insert('', tk.END, values=(desc_completa, item['qta'], f"€ {item['prezzo']:.2f}", f"€ {totale_riga:.2f}"))

        self.totale_carrello_str.set(f"€ {self.totale_carrello_val:.2f}")

    def svuota_carrello(self):
        self.carrello = []
        self.aggiorna_ui_carrello()

    def rimuovi_articolo_carrello(self, event=None):
        selezione = self.tree_cart.selection()
        if not selezione:
            return
        indice = self.tree_cart.index(selezione[0])
        if 0 <= indice < len(self.carrello):
            item = self.carrello[indice]
            id_riga_log = item.get('id_riga_log')
            if id_riga_log and self.tree_log.exists(id_riga_log):
                valori = list(self.tree_log.item(id_riga_log, 'values'))
                valori[4] = ''.join(c + '\u0336' for c in valori[4])
                self.tree_log.item(id_riga_log, values=valori, tags=('rimosso',))
            # Nessuna scrittura sul database: le eventuali rettifiche di una
            # vendita forzata non sono ancora state registrate, quindi togliere
            # la riga dal carrello non lascia stock fantasma.
            del self.carrello[indice]
            self.aggiorna_ui_carrello()

    def annulla_movimento_log(self, event=None):
        selezione = self.tree_log.selection()
        if not selezione:
            return
        id_riga = selezione[0]
        valori = list(self.tree_log.item(id_riga, 'values'))
        esito = valori[4]

        # Le righe "AGGIUNTO AL CARRELLO" si gestiscono dal carrello (pulsante Rimuovi), non da qui
        if 'AGGIUNTO AL CARRELLO' in esito:
            return

        # Le righe già barrate (rimosse o annullate) non sono annullabili di nuovo
        tags_riga = self.tree_log.item(id_riga, 'tags') or ()
        if 'rimosso' in tags_riga or 'annullato' in tags_riga:
            return

        id_movimento = self.movimenti_log.get(id_riga)
        if id_movimento is None:
            messagebox.showinfo("Info", "Questa riga non corrisponde a un movimento registrabile e non può essere annullata da qui.")
            return

        righe = self.leggi("SELECT id_transazione, quantita, codice FROM movimenti_magazzino WHERE id = ? AND storico_passivo = 0", (id_movimento,))
        if not righe:
            messagebox.showinfo("Info", "Il movimento risulta già annullato o non esiste più.")
            return
        id_transazione, qta_mov, codice_mov = righe[0]

        avviso = f"Annullare questa operazione?\n\n{valori[1]} - Cod. {valori[3]} - Q.tà {valori[2]}\n\nLa giacenza verrà ricalcolata di conseguenza."
        if id_transazione is not None:
            avviso += "\n\nIl movimento fa parte di una vendita registrata: il totale della transazione verrà ridotto di conseguenza."

        if not messagebox.askyesno("Annulla Operazione", avviso):
            return

        def azione(cursor):
            # storico_passivo = 1 invece di DELETE: il movimento smette di
            # contare nelle giacenze ma resta nello storico, e le righe di
            # transazioni non restano orfane.
            cursor.execute("UPDATE movimenti_magazzino SET storico_passivo = 1 WHERE id = ?", (id_movimento,))
            if id_transazione is not None:
                prezzo = cursor.execute("SELECT COALESCE(prezzo_vendita, 0) FROM articoli WHERE codice = ?", (codice_mov,)).fetchone()
                importo = (prezzo[0] if prezzo else 0.0) * qta_mov
                cursor.execute("UPDATE transazioni SET totale = ROUND(MAX(totale - ?, 0), 2) WHERE id = ?", (importo, id_transazione))

        ok, _ = self.scrivi(azione)
        if not ok:
            return

        valori[4] = ''.join(c + '\u0336' for c in esito) + ' [ANNULLATO]'
        self.tree_log.item(id_riga, values=valori, tags=('annullato',))
        self.movimenti_log.pop(id_riga, None)

    def esegui_pagamento(self, metodo):
        if not self.carrello:
            messagebox.showwarning("Attenzione", "Il carrello è vuoto.")
            return

        carrello = list(self.carrello)
        totale = round(self.totale_carrello_val, 2)
        ora_attuale = datetime.datetime.now().strftime("%H:%M:%S")

        def azione(cursor):
            cursor.execute("INSERT INTO transazioni (totale, metodo_pagamento) VALUES (?, ?)", (totale, metodo))
            id_transazione = cursor.lastrowid

            eventi = []
            for item in carrello:
                # Le rettifiche delle vendite forzate si scrivono QUI, nella
                # stessa transazione della vendita. Prima venivano committate
                # subito all'apertura del popup: se poi la riga usciva dal
                # carrello o il pagamento non arrivava, lo stock restava gonfiato.
                inventario.registra_rettifica(
                    cursor, item['codice'], item['origine'], item.get('rettifica', 0),
                    "Allineamento forzato per vendita sottoscorta")

                cursor.execute("""
                               INSERT INTO movimenti_magazzino
                               (codice, quantita, id_deposito_origine, id_deposito_destinazione, tipo, id_transazione)
                               VALUES (?, ?, ?, ?, ?, ?)
                               """, (item['codice'], item['qta'], item['origine'], item['destinazione'],
                                     inventario.SCARICO, id_transazione))
                id_movimento = cursor.lastrowid

                extra = f" ({item['colore'] or ''} {item['taglia'] or ''})".strip()
                eventi.append((ora_attuale, "Scarico (Vendita)", item['qta'], item['codice'],
                               f"OK - {item['desc']}{extra if extra != '()' else ''} [{metodo}]", id_movimento))
            return eventi

        ok, eventi = self.scrivi(azione, titolo_errore="Errore durante la transazione")
        if not ok:
            return

        # Il log si aggiorna solo dopo il commit: prima le righe comparivano
        # anche quando la transazione veniva poi annullata.
        for ora, op, qta, codice, esito, id_movimento in eventi:
            self.aggiorna_log(ora, op, qta, codice, esito, id_movimento=id_movimento)

        messagebox.showinfo("Successo", f"Transazione completata con successo.\nTotale: € {totale:.2f}\nMetodo: {metodo}")
        self.svuota_carrello()

    def avvia_registrazione(self, event):
        codice = self.entry_codice.get().strip()
        self.entry_codice.delete(0, tk.END)
        if not codice: return
        try:
            qta = self.var_qta.get()
            if qta <= 0:
                qta = 1
        except (tk.TclError, ValueError):
            qta = 1
        tipo = self.tipo_movimento.get()
        nome_op = {inventario.CARICO: "Carico", inventario.SCARICO: "Scarico",
                   inventario.RESO_CLIENTE: "Reso Cliente",
                   inventario.RESO_FORNITORE: "Reso Fornitore"}.get(tipo)
        ora_attuale = datetime.datetime.now().strftime("%H:%M:%S")

        id_fornitore = None
        bolla = None
        if tipo == inventario.CARICO:
            fornitore_nome = self.combo_fornitore.get()
            if fornitore_nome:
                for f in self.lista_fornitori:
                    if f[1] == fornitore_nome:
                        id_fornitore = f[0]
                        break
            bolla = self.entry_bolla.get().strip() or None

        righe = self.leggi("SELECT descrizione, colore, taglia, prezzo_acquisto, prezzo_vendita FROM articoli WHERE codice = ?", (codice,))
        articolo = righe[0] if righe else None
        origine, destinazione = (NEGOZIO, None) if tipo in (inventario.SCARICO, inventario.RESO_FORNITORE) else (None, NEGOZIO)

        if not articolo:
            self.mostra_popup_nuovo_articolo(codice, tipo, origine, destinazione, nome_op, ora_attuale, qta, id_fornitore, bolla)
            return

        if tipo == inventario.SCARICO:
            with self.db_lock:
                giac = inventario.giacenza(self.conn, codice, origine)

            # Tiene conto di eventuali quantità dello stesso articolo già presenti nel carrello,
            # non ancora scaricate dal DB, per non far passare due vendite che insieme superano la giacenza
            qta_gia_in_carrello = inventario.quantita_in_carrello(self.carrello, codice)

            if giac < (qta + qta_gia_in_carrello):
                disponibile_reale = max(giac - qta_gia_in_carrello, 0)
                self.mostra_popup_sottoscorta(codice, origine, destinazione, tipo, nome_op, ora_attuale, qta, disponibile_reale, articolo, id_fornitore, bolla)
                return

            self.aggiungi_al_carrello(codice, articolo, qta, origine, destinazione, nome_op, ora_attuale)
            return

        if origine is not None:
            with self.db_lock:
                giac = inventario.giacenza(self.conn, codice, origine)
            if giac < qta:
                self.mostra_popup_sottoscorta(codice, origine, destinazione, tipo, nome_op, ora_attuale, qta, giac, articolo, id_fornitore, bolla)
                return
        self.esegui_query_movimento(codice, origine, destinazione, tipo, nome_op, ora_attuale, articolo[0], qta, articolo[1], articolo[2], id_fornitore, bolla)

    def aggiungi_al_carrello(self, codice, articolo, qta, origine, destinazione, nome_op, ora_attuale,
                             rettifica=0, forzato=False):
        """Mette una riga nel carrello. rettifica = pezzi da allineare al pagamento."""
        item = {
            'codice': codice,
            'desc': articolo[0],
            'colore': articolo[1],
            'taglia': articolo[2],
            'prezzo': (articolo[4] if len(articolo) > 4 else 0.0) or 0.0,
            'qta': qta,
            'origine': origine,
            'destinazione': destinazione,
            'rettifica': rettifica,
        }
        self.carrello.append(item)
        self.aggiorna_ui_carrello()
        self.var_qta.set(1)
        etichetta = "AGGIUNTO AL CARRELLO (forzato)" if forzato else "AGGIUNTO AL CARRELLO"
        item['id_riga_log'] = self.aggiorna_log(ora_attuale, nome_op, qta, codice, f"{etichetta} - {articolo[0]}")
        return item

    def mostra_popup_sottoscorta(self, codice, origine, destinazione, tipo, nome_op, ora_attuale, qta_req, giac, articolo, id_fornitore=None, bolla=None):
        popup = tk.Toplevel(self.root)
        popup.title("Avviso")
        popup.geometry("560x260")
        popup.grab_set()
        ttk.Label(popup, text="ATTENZIONE: DISCREPANZA INVENTARIO", font=('Helvetica', 14, 'bold'), bootstyle="danger").pack(pady=15)
        ttk.Label(popup, text=f"Richiesti {qta_req} di {articolo[0]}.\nDisponibili: {giac}", justify=tk.CENTER).pack(pady=10)
        mancanti = qta_req - giac
        btn_frame = ttk.Frame(popup)
        btn_frame.pack(pady=10)

        def annulla():
            self.aggiorna_log(ora_attuale, nome_op, qta_req, codice, f"ANNULLATO - Giacenza: {giac}")
            popup.destroy()

        def forza():
            popup.destroy()
            if tipo == inventario.SCARICO:
                # La rettifica viaggia con la riga di carrello e viene scritta
                # solo al pagamento, insieme allo scarico.
                self.aggiungi_al_carrello(codice, articolo, qta_req, origine, destinazione,
                                          nome_op, ora_attuale, rettifica=mancanti, forzato=True)
                return

            def azione(cursor):
                inventario.registra_rettifica(cursor, codice, origine, mancanti,
                                              "Allineamento forzato da discrepanza inventario")
                cursor.execute("""
                               INSERT INTO movimenti_magazzino
                               (codice, quantita, id_deposito_origine, id_deposito_destinazione, tipo, id_fornitore, riferimento_bolla)
                               VALUES (?, ?, ?, ?, ?, ?, ?)
                               """, (codice, qta_req, origine, destinazione, tipo, id_fornitore, bolla))
                return cursor.lastrowid

            ok, id_movimento = self.scrivi(azione)
            if not ok:
                return
            extra = f" ({articolo[1] or ''} {articolo[2] or ''})".strip()
            self.aggiorna_log(ora_attuale, nome_op, qta_req, codice,
                              f"OK (forzato) - {articolo[0]}{extra if extra != '()' else ''}",
                              id_movimento=id_movimento)
            self.var_qta.set(1)

        ttk.Button(btn_frame, text="Annulla", command=annulla, bootstyle="secondary").pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text=f"Forza Allineamento (+{mancanti})", command=forza, bootstyle="warning").pack(side=tk.LEFT, padx=10)
        popup.bind('<Escape>', lambda e: annulla())

    def mostra_popup_nuovo_articolo(self, codice, tipo, origine, destinazione, nome_op, ora_attuale, qta, id_fornitore=None, bolla=None):
        popup = tk.Toplevel(self.root)
        popup.title("Nuovo Articolo")
        popup.geometry("450x330")
        popup.grab_set()
        form = ttk.Frame(popup)
        form.pack(fill=tk.BOTH, expand=True, ipadx=10, ipady=10)
        entries = {}
        var_stampa_etichetta = tk.BooleanVar(value=False)
        for i, (label, key) in enumerate([("Descrizione:", "desc"), ("Colore:", "col"), ("Taglia:", "tag"), ("Costo Acq:", "acq"), ("Prezzo Ven:", "ven")]):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky=tk.E, pady=5)
            ent = ttk.Entry(form, width=30)
            ent.grid(row=i, column=1, pady=5)
            entries[key] = ent
        if tipo == inventario.CARICO:
            chk_stampa = ttk.Checkbutton(
                form,
                text="Stampa etichetta barcode",
                variable=var_stampa_etichetta,
                bootstyle="round-toggle"
            )
            chk_stampa.grid(row=len(entries), column=1, sticky=tk.W, pady=(10, 0))
        entries["desc"].focus()

        def salva(e=None):
            desc = entries["desc"].get().strip()
            if not desc:
                messagebox.showwarning("Attenzione", "La descrizione è obbligatoria.")
                return

            colore_val = entries["col"].get().strip()
            taglia_val = entries["tag"].get().strip()

            try:
                acq = parse_prezzo(entries["acq"].get(), "Costo Acquisto")
                ven = parse_prezzo(entries["ven"].get(), "Prezzo Vendita")
            except ValoreNonValido as err:
                messagebox.showerror("Valore non valido", str(err))
                return

            def azione(cursor):
                # INSERT protetto: il codice arriva da uno scanner e i duplicati
                # sono normali. Prima l'IntegrityError usciva come traceback su
                # stderr e lasciava la connessione in transazione aperta.
                try:
                    cursor.execute("INSERT INTO articoli (codice, descrizione, colore, taglia, prezzo_acquisto, prezzo_vendita) VALUES (?, ?, ?, ?, ?, ?)",
                                   (codice, desc, colore_val, taglia_val, acq, ven))
                except sqlite3.IntegrityError:
                    raise sqlite3.Error(f"L'articolo {codice} esiste già in anagrafica. Chiudi questa finestra e rileggi il codice.")

                if tipo == inventario.SCARICO:
                    # Vendita di un articolo mai censito: prima lo si carica,
                    # poi la riga va nel carrello e lo scarico avviene al pagamento.
                    cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_destinazione, tipo, riferimento_bolla) VALUES (?, ?, ?, ?, ?)",
                                   (codice, qta, origine, inventario.CARICO, "Carico implicito da vendita di articolo nuovo"))
                    return None

                cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_origine, id_deposito_destinazione, tipo, id_fornitore, riferimento_bolla) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (codice, qta, origine, destinazione, tipo, id_fornitore, bolla))
                return cursor.lastrowid

            ok, id_movimento = self.scrivi(azione)
            if not ok:
                return

            popup.destroy()
            articolo = (desc, colore_val, taglia_val, acq, ven)

            if tipo == inventario.SCARICO:
                self.aggiungi_al_carrello(codice, articolo, qta, origine, destinazione, nome_op, ora_attuale)
                return

            extra = f" ({colore_val or ''} {taglia_val or ''})".strip()
            self.aggiorna_log(ora_attuale, nome_op, qta, codice,
                              f"OK - {desc}{extra if extra != '()' else ''}", id_movimento=id_movimento)
            self.var_qta.set(1)

            if tipo == inventario.CARICO and var_stampa_etichetta.get():
                self._chiedi_stampa_etichetta(codice)

        ttk.Button(popup, text="Salva", command=salva, bootstyle="success").pack(pady=10)
        popup.bind('<Return>', salva)

    def esegui_query_movimento(self, codice, origine, destinazione, tipo, nome_op, ora, desc, qta, colore, taglia, id_fornitore=None, bolla=None):
        def azione(cursor):
            cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_origine, id_deposito_destinazione, tipo, id_fornitore, riferimento_bolla) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (codice, qta, origine, destinazione, tipo, id_fornitore, bolla))
            return cursor.lastrowid

        ok, id_movimento = self.scrivi(azione)
        if not ok:
            return
        extra = f" ({colore or ''} {taglia or ''})".strip()
        self.aggiorna_log(ora, nome_op, qta, codice, f"OK - {desc}{extra if extra != '()' else ''}", id_movimento=id_movimento)
        self.var_qta.set(1)

    def aggiorna_log(self, ora, op, qta, codice, esito, id_movimento=None):
        id_riga = self.tree_log.insert('', 0, values=(ora, op, qta, codice, esito))
        if id_movimento is not None:
            self.movimenti_log[id_riga] = id_movimento
        if len(self.tree_log.get_children()) > 15:
            id_eliminata = self.tree_log.get_children()[-1]
            self.tree_log.delete(id_eliminata)
            self.movimenti_log.pop(id_eliminata, None)
        return id_riga

if __name__ == "__main__":
    configura_logging()
    inizializza_database()
    root = ttk.Window(themename="minty")
    app = TerminaleMagazzino(root)
    root.mainloop()
