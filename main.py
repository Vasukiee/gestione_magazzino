import tkinter as tk
from tkinter import messagebox, filedialog
import ttkbootstrap as ttk
import sqlite3
import datetime
import csv
import threading
import time
import os
import shutil
import glob
import signal
import sys
from setup_db import inizializza_database

class TerminaleMagazzino:
    def __init__(self, root):
        self.root = root
        self.root.title("Gestione Magazzino")
        self.root.geometry("1300x750")

        self.conn = sqlite3.connect('magazzino.db', check_same_thread=False)
        self.tipo_movimento = tk.IntVar(value=1)
        self.var_qta = tk.IntVar(value=1)
        self.var_qta_trasf = tk.IntVar(value=1)
        self.lista_fornitori = []

        self.carrello = []
        self.totale_carrello_str = tk.StringVar(value="€ 0.00")
        self.totale_carrello_val = 0.0
        self.movimenti_log = {}  # id riga tree_log -> id movimento in movimenti_magazzino (per annullamento)

        self.setup_cartelle_backup()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        signal.signal(signal.SIGTERM, self.handle_sigterm)

        self.setup_ui()

        threading.Thread(target=self.worker_backup_rolling, daemon=True).start()

    def setup_cartelle_backup(self):
        os.makedirs("backup/rolling", exist_ok=True)
        os.makedirs("backup/daily", exist_ok=True)

    def worker_backup_rolling(self):
        while True:
            time.sleep(1800)
            try:
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                shutil.copy2('magazzino.db', f'backup/rolling/backup_{timestamp}.db')

                lista_file = sorted(glob.glob('backup/rolling/backup_*.db'), key=os.path.getmtime)
                while len(lista_file) > 16:
                    file_da_rimuovere = lista_file.pop(0)
                    os.remove(file_da_rimuovere)
            except Exception as e:
                print(f"Errore backup rolling: {e}")

    def on_closing(self):
        try:
            shutil.copy2('magazzino.db', 'backup/latest_backup.db')
            data_odierna = datetime.datetime.now().strftime("%Y%m%d")
            shutil.copy2('magazzino.db', f'backup/daily/backup_{data_odierna}.db')
        except Exception as e:
            print(f"Errore backup chiusura: {e}")
        finally:
            self.conn.close()
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
            cursor = self.conn.cursor()
            cursor.execute("SELECT id, ragione_sociale FROM fornitori ORDER BY ragione_sociale")
            self.lista_fornitori = cursor.fetchall()
            self.combo_fornitore['values'] = [f[1] for f in self.lista_fornitori]
        except sqlite3.OperationalError:
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

        btn_pulisci = ttk.Button(search_top, text="Elimina Giacenze Zero", command=self.pulizia_zero, bootstyle="danger")
        btn_pulisci.pack(side=tk.RIGHT, padx=(0, 10))

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
        self.tree_ricerca.pack(fill=tk.BOTH, expand=True)

        self.menu_contestuale = tk.Menu(self.root, tearoff=0)
        self.menu_contestuale.add_command(label="Modifica / Rettifica", command=self.apri_modifica)
        self.menu_contestuale.add_command(label="Elimina Intero Record", command=self.elimina_selezionato)

        self.tree_ricerca.bind("<Button-3>", self.mostra_menu_contestuale)

    def mostra_menu_contestuale(self, event):
        item = self.tree_ricerca.identify_row(event.y)
        if item:
            self.tree_ricerca.selection_set(item)
            self.tree_ricerca.focus(item)
            self.menu_contestuale.tk_popup(event.x_root, event.y_root)

    def apri_modifica(self):
        selected = self.tree_ricerca.focus()
        if not selected: return
        valori = self.tree_ricerca.item(selected)['values']
        codice = valori[0]

        popup = tk.Toplevel(self.root)
        popup.title(f"Modifica & Rettifica Inventario - {codice}")
        popup.geometry("500x420")
        popup.grab_set()

        form = ttk.Frame(popup)
        form.pack(fill=tk.BOTH, expand=True, ipadx=10, ipady=10)

        campi = ["Descrizione:", "Colore:", "Taglia:", "Costo Acq (€):", "Prezzo Ven (€):", "Giacenza Negozio:", "Giacenza Box:"]
        valori_attuali = [
            valori[1],
            valori[2],
            valori[3],
            str(valori[4]).replace('€ ', ''),
            str(valori[5]).replace('€ ', ''),
            str(valori[6]),
            str(valori[7])
        ]
        entries = {}

        for i, label in enumerate(campi):
            ttk.Label(form, text=label, font=('Helvetica', 11, 'bold' if 'Giacenza' in label else 'normal')).grid(row=i, column=0, sticky=tk.E, pady=7)
            ent = ttk.Entry(form, width=30)
            ent.grid(row=i, column=1, pady=7)
            val = valori_attuali[i] if str(valori_attuali[i]) != 'None' else ''
            ent.insert(0, val)
            entries[label] = ent

        def salva(e=None):
            desc = entries["Descrizione:"].get().strip()
            if not desc: return

            p_acq_str = entries["Costo Acq (€):"].get().replace(',', '.')
            p_ven_str = entries["Prezzo Ven (€):"].get().replace(',', '.')

            p_acq = float(p_acq_str) if p_acq_str.replace('.','',1).isdigit() else 0.0
            p_ven = float(p_ven_str) if p_ven_str.replace('.','',1).isdigit() else 0.0

            try:
                nuova_giac_neg = int(entries["Giacenza Negozio:"].get().strip())
                nuova_giac_box = int(entries["Giacenza Box:"].get().strip())
            except ValueError:
                nuova_giac_neg = int(valori[6])
                nuova_giac_box = int(valori[7])

            cursor = self.conn.cursor()
            try:
                variazioni = []
                if desc != str(valori[1]): variazioni.append("Desc")
                if p_ven != float(str(valori[5]).replace('€ ', '')): variazioni.append("Prezzo")

                cursor.execute("""
                               UPDATE articoli SET descrizione = ?, colore = ?, taglia = ?, prezzo_acquisto = ?, prezzo_vendita = ?
                               WHERE codice = ?
                               """, (desc, entries["Colore:"].get(), entries["Taglia:"].get(), p_acq, p_ven, codice))

                # Inserimenti anomalie stock (Tipi 6 e 7)
                delta_neg = nuova_giac_neg - int(valori[6])
                if delta_neg > 0:
                    cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_destinazione, tipo) VALUES (?, ?, 1, 6)", (codice, delta_neg))
                elif delta_neg < 0:
                    cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_origine, tipo) VALUES (?, ?, 1, 7)", (codice, abs(delta_neg)))

                delta_box = nuova_giac_box - int(valori[7])
                if delta_box > 0:
                    cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_destinazione, tipo) VALUES (?, ?, 2, 6)", (codice, delta_box))
                elif delta_box < 0:
                    cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_origine, tipo) VALUES (?, ?, 2, 7)", (codice, abs(delta_box)))

                # Inserimento anomalia anagrafica (Tipo 8)
                if variazioni:
                    nota = "Modificato manualmente: " + ", ".join(variazioni)
                    try:
                        cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, tipo, riferimento_bolla) VALUES (?, 0, 8, ?)", (codice, nota))
                    except sqlite3.OperationalError:
                        cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, tipo) VALUES (?, 0, 8)", (codice,))

                self.conn.commit()
                popup.destroy()
                self.esegui_ricerca()
            except sqlite3.Error as err:
                messagebox.showerror("Errore DB", str(err))

        ttk.Button(popup, text="Salva Modifiche", command=salva, bootstyle="success").pack(pady=10)

    def elimina_selezionato(self):
        selected = self.tree_ricerca.focus()
        if not selected: return
        codice = self.tree_ricerca.item(selected)['values'][0]

        if messagebox.askyesno("Eliminazione", f"Sei sicuro di voler eliminare l'articolo {codice} e tutti i suoi movimenti?"):
            cursor = self.conn.cursor()
            try:
                cursor.execute("DELETE FROM movimenti_magazzino WHERE codice = ?", (codice,))
                cursor.execute("DELETE FROM articoli WHERE codice = ?", (codice,))
                self.conn.commit()
                self.esegui_ricerca()
            except sqlite3.Error as e:
                messagebox.showerror("Errore DB", str(e))

    def pulizia_zero(self):
        if not messagebox.askyesno("Avviso Critico", "Questa operazione eliminerà permanentemente tutti gli articoli (e i relativi movimenti) la cui giacenza totale tra i depositi è uguale a 0.\n\nProcedere?"):
            return

        sql_delete_articoli = """
                              DELETE FROM articoli WHERE codice IN (
                                  SELECT a.codice FROM articoli a
                                                           LEFT JOIN movimenti_magazzino m ON a.codice = m.codice
                                  GROUP BY a.codice
                                  HAVING (
                                             COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                                             COALESCE(SUM(CASE WHEN m.id_deposito_origine = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) +
                                             COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                                             COALESCE(SUM(CASE WHEN m.id_deposito_origine = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0)
                                             ) = 0
                              ) \
                              """

        sql_delete_movimenti = """
                               DELETE FROM movimenti_magazzino WHERE codice IN (
                                   SELECT a.codice FROM articoli a
                                                            LEFT JOIN movimenti_magazzino m ON a.codice = m.codice
                                   GROUP BY a.codice
                                   HAVING (
                                              COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                                              COALESCE(SUM(CASE WHEN id_deposito_origine = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) +
                                              COALESCE(SUM(CASE WHEN id_deposito_destinazione = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                                              COALESCE(SUM(CASE WHEN id_deposito_origine = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0)
                                              ) = 0
                               ) \
                               """

        cursor = self.conn.cursor()
        try:
            cursor.execute(sql_delete_movimenti)
            cursor.execute(sql_delete_articoli)
            self.conn.commit()
            messagebox.showinfo("Completato", "Inventario pulito correttamente.")
            self.esegui_ricerca()
        except sqlite3.Error as e:
            messagebox.showerror("Errore DB", str(e))

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
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, ragione_sociale FROM fornitori ORDER BY ragione_sociale")
        for id_forn, nome in cursor.fetchall():
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

        cursor = self.conn.cursor()
        try:
            if self.fornitore_in_modifica is not None:
                cursor.execute("UPDATE fornitori SET ragione_sociale = ? WHERE id = ?", (nome, self.fornitore_in_modifica))
            else:
                cursor.execute("INSERT INTO fornitori (ragione_sociale) VALUES (?)", (nome,))
            self.conn.commit()
        except sqlite3.IntegrityError:
            messagebox.showerror("Errore", f"Esiste già un fornitore con ragione sociale '{nome}'.")
            return
        except sqlite3.Error as e:
            messagebox.showerror("Errore DB", str(e))
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

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM movimenti_magazzino WHERE id_fornitore = ?", (id_forn,))
        n_movimenti = cursor.fetchone()[0]

        messaggio = f"Eliminare il fornitore '{nome}'?"
        if n_movimenti > 0:
            messaggio += (f"\n\nQuesto fornitore è collegato a {n_movimenti} movimento/i di carico già registrati. "
                          f"Il nome '{nome}' resterà comunque visibile nello storico, ma non sarà più possibile selezionarlo per nuovi carichi.")

        if not messagebox.askyesno("Elimina Fornitore", messaggio):
            return

        try:
            # Preserva il nome nello storico prima di eliminare il fornitore dall'anagrafica
            cursor.execute("UPDATE movimenti_magazzino SET nome_fornitore_storico = ? WHERE id_fornitore = ?", (nome, id_forn))
            cursor.execute("DELETE FROM fornitori WHERE id = ?", (id_forn,))
            self.conn.commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            messagebox.showerror("Errore DB", str(e))
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
            focus_attuale = self.root.focus_get()
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
            if qta <= 0: raise ValueError
        except:
            qta = 1

        orig_str = self.combo_orig.get()
        dest_str = self.combo_dest.get()

        if orig_str == dest_str:
            messagebox.showerror("Errore", "Magazzino di origine e destinazione coincidono.")
            return

        orig_id = 1 if orig_str == "Negozio" else 2
        dest_id = 1 if dest_str == "Negozio" else 2

        cursor = self.conn.cursor()
        cursor.execute("SELECT descrizione, colore, taglia FROM articoli WHERE codice = ?", (codice,))
        articolo = cursor.fetchone()
        if not articolo:
            messagebox.showerror("Errore", "Articolo inesistente in anagrafica. Effettuare prima il carico.")
            return

        cursor.execute("""
                       SELECT
                           COALESCE(SUM(CASE WHEN id_deposito_destinazione = ? THEN (CASE WHEN storico_passivo = 0 THEN quantita ELSE 0 END) ELSE 0 END), 0) -
                           COALESCE(SUM(CASE WHEN id_deposito_origine = ? THEN (CASE WHEN storico_passivo = 0 THEN quantita ELSE 0 END) ELSE 0 END), 0)
                       FROM movimenti_magazzino WHERE codice = ?
                       """, (orig_id, orig_id, codice))

        giac_attuale = cursor.fetchone()[0]

        if giac_attuale < qta:
            ora_attuale = datetime.datetime.now().strftime("%H:%M:%S")
            self.mostra_popup_sottoscorta(codice, orig_id, dest_id, 5, f"Trasferimento a {dest_str}", ora_attuale, qta, giac_attuale, articolo)
            return

        self.esegui_query_movimento(codice, orig_id, dest_id, 5, f"Trasferimento a {dest_str}", datetime.datetime.now().strftime("%H:%M:%S"), articolo[0], qta, articolo[1], articolo[2], None, None)

    def aggiorna_statistiche(self):
        sql = """
              SELECT
                  SUM(giac_negozio), SUM(giac_negozio * prezzo_acquisto), SUM(giac_negozio * prezzo_vendita),
                  SUM(giac_box), SUM(giac_box * prezzo_acquisto), SUM(giac_box * prezzo_vendita)
              FROM (
                       SELECT a.prezzo_acquisto, a.prezzo_vendita,
                              COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                              COALESCE(SUM(CASE WHEN m.id_deposito_origine = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) AS giac_negozio,
                              COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                              COALESCE(SUM(CASE WHEN m.id_deposito_origine = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) AS giac_box
                       FROM articoli a
                                LEFT JOIN movimenti_magazzino m ON a.codice = m.codice
                       GROUP BY a.codice
                   ) \
              """
        cursor = self.conn.cursor()
        cursor.execute(sql)
        row = cursor.fetchone()

        if not row or row[0] is None:
            self.lbl_tot_pz.config(text="Nessun dato disponibile.")
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
        query_text = f"%{self.entry_ricerca.get().strip()}%"
        sql = """
              SELECT a.codice, a.descrizione, a.colore, a.taglia, a.prezzo_acquisto, a.prezzo_vendita,
                     COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                     COALESCE(SUM(CASE WHEN m.id_deposito_origine = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) AS giacenza_negozio,
                     COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                     COALESCE(SUM(CASE WHEN m.id_deposito_origine = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) AS giacenza_box
              FROM articoli a
                       LEFT JOIN movimenti_magazzino m ON a.codice = m.codice
              WHERE a.codice LIKE ? OR a.descrizione LIKE ? OR a.colore LIKE ?
              GROUP BY a.codice \
              """
        cursor = self.conn.cursor()
        cursor.execute(sql, (query_text, query_text, query_text))
        for item in self.tree_ricerca.get_children(): self.tree_ricerca.delete(item)
        for r in cursor.fetchall():
            self.tree_ricerca.insert('', tk.END, values=(r[0], r[1], r[2], r[3], f"€ {r[4]:.2f}", f"€ {r[5]:.2f}", r[6], r[7]))

    def mostra_esaurimento(self):
        sql = """
              SELECT a.codice, a.descrizione, a.colore, a.taglia, a.prezzo_acquisto, a.prezzo_vendita,
                     COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                     COALESCE(SUM(CASE WHEN m.id_deposito_origine = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) AS giacenza_negozio,
                     COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                     COALESCE(SUM(CASE WHEN m.id_deposito_origine = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) AS giacenza_box
              FROM articoli a
                       LEFT JOIN movimenti_magazzino m ON a.codice = m.codice
              GROUP BY a.codice, a.soglia_minima
              HAVING (giacenza_negozio + giacenza_box) <= a.soglia_minima \
              """
        cursor = self.conn.cursor()
        cursor.execute(sql)
        for item in self.tree_ricerca.get_children(): self.tree_ricerca.delete(item)
        for r in cursor.fetchall():
            self.tree_ricerca.insert('', tk.END, values=(r[0], r[1], r[2], r[3], f"€ {r[4]:.2f}", f"€ {r[5]:.2f}", r[6], r[7]), tags=('danger',))

    def esporta_storico_csv(self):
        sql = """
              SELECT m.data_ora,
                     CASE m.tipo WHEN 1 THEN 'Carico' WHEN 2 THEN 'Scarico' WHEN 3 THEN 'Reso Cliente' WHEN 4 THEN 'Reso Fornitore' WHEN 5 THEN 'Trasferimento Interno' WHEN 6 THEN 'Rettifica Positiva' WHEN 7 THEN 'Rettifica Negativa' WHEN 8 THEN 'Modifica Anagrafica' ELSE 'Altro' END,
                     m.codice, a.descrizione, a.colore, a.taglia, a.prezzo_acquisto, a.prezzo_vendita,
                     COALESCE(d_orig.nome_deposito, '-'), COALESCE(d_dest.nome_deposito, '-'), m.quantita,
                     COALESCE(f.ragione_sociale, m.nome_fornitore_storico, ''), COALESCE(m.riferimento_bolla, '')
              FROM movimenti_magazzino m
                       LEFT JOIN articoli a ON m.codice = a.codice
                       LEFT JOIN depositi d_orig ON m.id_deposito_origine = d_orig.id
                       LEFT JOIN depositi d_dest ON m.id_deposito_destinazione = d_dest.id
                       LEFT JOIN fornitori f ON m.id_fornitore = f.id
              ORDER BY m.data_ora DESC \
              """
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError:
            sql_fallback = """
                           SELECT m.data_ora,
                                  CASE m.tipo WHEN 1 THEN 'Carico' WHEN 2 THEN 'Scarico' WHEN 3 THEN 'Reso Cliente' WHEN 4 THEN 'Reso Fornitore' WHEN 5 THEN 'Trasferimento Interno' WHEN 6 THEN 'Rettifica Positiva' WHEN 7 THEN 'Rettifica Negativa' WHEN 8 THEN 'Modifica Anagrafica' ELSE 'Altro' END,
                                  m.codice, a.descrizione, a.colore, a.taglia, a.prezzo_acquisto, a.prezzo_vendita,
                                  COALESCE(d_orig.nome_deposito, '-'), COALESCE(d_dest.nome_deposito, '-'), m.quantita,
                                  '', ''
                           FROM movimenti_magazzino m
                                    LEFT JOIN articoli a ON m.codice = a.codice
                                    LEFT JOIN depositi d_orig ON m.id_deposito_origine = d_orig.id
                                    LEFT JOIN depositi d_dest ON m.id_deposito_destinazione = d_dest.id
                           ORDER BY m.data_ora DESC \
                           """
            cursor.execute(sql_fallback)

        righe = cursor.fetchall()
        if not righe: return messagebox.showinfo("Info", "Nessun movimento.")
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"storico_{datetime.datetime.now().strftime('%Y%m%d')}.csv")
        if path:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Data Ora", "Operazione", "Codice", "Descrizione", "Colore", "Taglia", "Costo", "Prezzo", "Origine", "Destinazione", "Quantità", "Fornitore", "Note / Bolla"])
                writer.writerows(righe)

    def esporta_giacenze_csv(self):
        sql = """
              SELECT a.codice, a.descrizione, a.colore, a.taglia, a.prezzo_acquisto, a.prezzo_vendita,
                     COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                     COALESCE(SUM(CASE WHEN m.id_deposito_origine = 1 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) AS giac_neg,
                     COALESCE(SUM(CASE WHEN m.id_deposito_destinazione = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) -
                     COALESCE(SUM(CASE WHEN m.id_deposito_origine = 2 THEN (CASE WHEN m.storico_passivo = 0 THEN m.quantita ELSE 0 END) ELSE 0 END), 0) AS giac_box
              FROM articoli a
                       LEFT JOIN movimenti_magazzino m ON a.codice = m.codice
              GROUP BY a.codice
              ORDER BY a.descrizione \
              """
        cursor = self.conn.cursor()
        cursor.execute(sql)
        righe = cursor.fetchall()
        if not righe: return messagebox.showinfo("Info", "Nessun articolo in anagrafica.")
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"inventario_giacenze_{datetime.datetime.now().strftime('%Y%m%d')}.csv")
        if path:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Codice", "Descrizione", "Colore", "Taglia", "Costo Acquisto", "Prezzo Vendita", "Giacenza Negozio", "Giacenza Box", "Giacenza Totale"])
                for r in righe:
                    totale = r[6] + r[7]
                    writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], totale])

    def esporta_report_anomalie_csv(self):
        sql = """
              SELECT m.data_ora,
                     CASE m.tipo WHEN 6 THEN 'Rettifica Positiva' WHEN 7 THEN 'Rettifica Negativa' WHEN 8 THEN 'Modifica Anagrafica' END AS operazione,
                     m.codice, a.descrizione, a.colore, a.taglia,
                     COALESCE(d_orig.nome_deposito, '-') AS origine,
                     COALESCE(d_dest.nome_deposito, '-') AS destinazione,
                     m.quantita,
                     COALESCE(m.riferimento_bolla, '') AS note
              FROM movimenti_magazzino m
                       LEFT JOIN articoli a ON m.codice = a.codice
                       LEFT JOIN depositi d_orig ON m.id_deposito_origine = d_orig.id
                       LEFT JOIN depositi d_dest ON m.id_deposito_destinazione = d_dest.id
              WHERE m.tipo IN (6, 7, 8)
              ORDER BY m.data_ora DESC \
              """
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError:
            sql_fallback = """
                           SELECT m.data_ora,
                                  CASE m.tipo WHEN 6 THEN 'Rettifica Positiva' WHEN 7 THEN 'Rettifica Negativa' WHEN 8 THEN 'Modifica Anagrafica' END AS operazione,
                                  m.codice, a.descrizione, a.colore, a.taglia,
                                  COALESCE(d_orig.nome_deposito, '-') AS origine,
                                  COALESCE(d_dest.nome_deposito, '-') AS destinazione,
                                  m.quantita,
                                  '' AS note
                           FROM movimenti_magazzino m
                                    LEFT JOIN articoli a ON m.codice = a.codice
                                    LEFT JOIN depositi d_orig ON m.id_deposito_origine = d_orig.id
                                    LEFT JOIN depositi d_dest ON m.id_deposito_destinazione = d_dest.id
                           WHERE m.tipo IN (6, 7, 8)
                           ORDER BY m.data_ora DESC \
                           """
            cursor.execute(sql_fallback)

        righe = cursor.fetchall()
        if not righe:
            return messagebox.showinfo("Info", "Nessuna rettifica o modifica registrata.")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"report_anomalie_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
        )
        if path:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Data Ora", "Operazione", "Codice", "Descrizione", "Colore", "Taglia", "Deposito Origine", "Deposito Destinazione", "Quantità", "Note Aggiuntive"])
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
        if 'tags' in self.tree_log.item(id_riga) and ('rimosso' in self.tree_log.item(id_riga, 'tags') or 'annullato' in self.tree_log.item(id_riga, 'tags')):
            return

        id_movimento = self.movimenti_log.get(id_riga)
        if id_movimento is None:
            # Riga senza movimento DB associato (es. "ANNULLATO - Giacenza: ...", o riga storica precedente all'aggiornamento)
            messagebox.showinfo("Info", "Questa riga non corrisponde a un movimento registrabile e non può essere annullata da qui.")
            return

        if not messagebox.askyesno("Annulla Operazione", f"Annullare questa operazione?\n\n{valori[1]} - Cod. {valori[3]} - Q.tà {valori[2]}\n\nIl movimento verrà eliminato dal database e la giacenza ricalcolata di conseguenza."):
            return

        cursor = self.conn.cursor()
        try:
            cursor.execute("DELETE FROM movimenti_magazzino WHERE id = ?", (id_movimento,))
            self.conn.commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            messagebox.showerror("Errore DB", str(e))
            return

        valori[4] = ''.join(c + '\u0336' for c in esito) + ' [ANNULLATO]'
        self.tree_log.item(id_riga, values=valori, tags=('annullato',))
        self.movimenti_log.pop(id_riga, None)

    def esegui_pagamento(self, metodo):
        if not self.carrello:
            messagebox.showwarning("Attenzione", "Il carrello è vuoto.")
            return

        try:
            cursor = self.conn.cursor()
            cursor.execute("INSERT INTO transazioni (totale, metodo_pagamento) VALUES (?, ?)", (self.totale_carrello_val, metodo))
            id_transazione = cursor.lastrowid

            ora_attuale = datetime.datetime.now().strftime("%H:%M:%S")

            for item in self.carrello:
                try:
                    cursor.execute("""
                                   INSERT INTO movimenti_magazzino
                                   (codice, quantita, id_deposito_origine, id_deposito_destinazione, tipo, id_transazione)
                                   VALUES (?, ?, ?, ?, ?, ?)
                                   """, (item['codice'], item['qta'], item['origine'], item['destinazione'], 2, id_transazione))
                except sqlite3.OperationalError:
                    cursor.execute("""
                                   INSERT INTO movimenti_magazzino
                                       (codice, quantita, id_deposito_origine, id_deposito_destinazione, tipo)
                                   VALUES (?, ?, ?, ?, ?)
                                   """, (item['codice'], item['qta'], item['origine'], item['destinazione'], 2))
                id_movimento = cursor.lastrowid

                extra = f" ({item['colore'] or ''} {item['taglia'] or ''})".strip()
                self.aggiorna_log(ora_attuale, "Scarico (Vendita)", item['qta'], item['codice'], f"OK - {item['desc']}{extra if extra != '()' else ''} [{metodo}]", id_movimento=id_movimento)

            self.conn.commit()
            messagebox.showinfo("Successo", f"Transazione completata con successo.\nTotale: € {self.totale_carrello_val:.2f}\nMetodo: {metodo}")
            self.svuota_carrello()

        except Exception as e:
            self.conn.rollback()
            messagebox.showerror("Errore", f"Errore durante la transazione:\n{e}")

    def avvia_registrazione(self, event):
        codice = self.entry_codice.get().strip()
        self.entry_codice.delete(0, tk.END)
        if not codice: return
        try:
            qta = self.var_qta.get()
            if qta <= 0: raise ValueError
        except: qta = 1
        tipo = self.tipo_movimento.get()
        nome_op = {1: "Carico", 2: "Scarico", 3: "Reso Cliente", 4: "Reso Fornitore"}.get(tipo)
        ora_attuale = datetime.datetime.now().strftime("%H:%M:%S")

        id_fornitore = None
        bolla = None
        if tipo == 1:
            fornitore_nome = self.combo_fornitore.get()
            if fornitore_nome:
                for f in self.lista_fornitori:
                    if f[1] == fornitore_nome:
                        id_fornitore = f[0]
                        break
            bolla = self.entry_bolla.get().strip()

        cursor = self.conn.cursor()
        cursor.execute("SELECT descrizione, colore, taglia, prezzo_acquisto, prezzo_vendita FROM articoli WHERE codice = ?", (codice,))
        articolo = cursor.fetchone()
        origine, destinazione = (1, None) if tipo in (2, 4) else (None, 1)

        if not articolo:
            self.mostra_popup_nuovo_articolo(codice, tipo, origine, destinazione, nome_op, ora_attuale, qta, id_fornitore, bolla)
            return

        if tipo == 2:
            cursor.execute("""
                           SELECT COALESCE(SUM(CASE WHEN id_deposito_destinazione = ? THEN (CASE WHEN storico_passivo = 0 THEN quantita ELSE 0 END) ELSE 0 END), 0) -
                                  COALESCE(SUM(CASE WHEN id_deposito_origine = ? THEN (CASE WHEN storico_passivo = 0 THEN quantita ELSE 0 END) ELSE 0 END), 0)
                           FROM movimenti_magazzino WHERE codice = ?
                           """, (origine, origine, codice))
            giac = cursor.fetchone()[0]

            # Tiene conto di eventuali quantità dello stesso articolo già presenti nel carrello,
            # non ancora scaricate dal DB, per non far passare due vendite che insieme superano la giacenza
            qta_gia_in_carrello = sum(i['qta'] for i in self.carrello if i['codice'] == codice)

            if giac < (qta + qta_gia_in_carrello):
                disponibile_reale = max(giac - qta_gia_in_carrello, 0)
                self.mostra_popup_sottoscorta(codice, origine, destinazione, tipo, nome_op, ora_attuale, qta, disponibile_reale, articolo, id_fornitore, bolla)
                return

            item = {
                'codice': codice,
                'desc': articolo[0],
                'colore': articolo[1],
                'taglia': articolo[2],
                'prezzo': articolo[4] or 0.0,
                'qta': qta,
                'origine': origine,
                'destinazione': destinazione
            }
            self.carrello.append(item)
            self.aggiorna_ui_carrello()
            self.var_qta.set(1)
            item['id_riga_log'] = self.aggiorna_log(ora_attuale, nome_op, qta, codice, f"AGGIUNTO AL CARRELLO - {articolo[0]}")
            return

        if origine is not None:
            cursor.execute("""
                           SELECT COALESCE(SUM(CASE WHEN id_deposito_destinazione = ? THEN (CASE WHEN storico_passivo = 0 THEN quantita ELSE 0 END) ELSE 0 END), 0) -
                                  COALESCE(SUM(CASE WHEN id_deposito_origine = ? THEN (CASE WHEN storico_passivo = 0 THEN quantita ELSE 0 END) ELSE 0 END), 0)
                           FROM movimenti_magazzino WHERE codice = ?
                           """, (origine, origine, codice))
            giac = cursor.fetchone()[0]
            if giac < qta:
                self.mostra_popup_sottoscorta(codice, origine, destinazione, tipo, nome_op, ora_attuale, qta, giac, articolo, id_fornitore, bolla)
                return
        self.esegui_query_movimento(codice, origine, destinazione, tipo, nome_op, ora_attuale, articolo[0], qta, articolo[1], articolo[2], id_fornitore, bolla)

    def mostra_popup_sottoscorta(self, codice, origine, destinazione, tipo, nome_op, ora_attuale, qta_req, giac, articolo, id_fornitore=None, bolla=None):
        popup = tk.Toplevel(self.root)
        popup.title("Avviso")
        popup.geometry("550x250")
        popup.grab_set()
        ttk.Label(popup, text="ATTENZIONE: DISCREPANZA INVENTARIO", font=('Helvetica', 14, 'bold'), bootstyle="danger").pack(pady=15)
        ttk.Label(popup, text=f"Richiesti {qta_req} di {articolo[0]}.\nDisponibili: {giac}", justify=tk.CENTER).pack(pady=10)
        btn_frame = ttk.Frame(popup)
        btn_frame.pack(pady=10)
        def annulla():
            self.aggiorna_log(ora_attuale, nome_op, qta_req, codice, f"ANNULLATO - Giacenza: {giac}")
            popup.destroy()
        def forza():
            try:
                self.conn.cursor().execute("""
                                           INSERT INTO movimenti_magazzino
                                           (codice, quantita, id_deposito_destinazione, tipo, id_fornitore, riferimento_bolla)
                                           VALUES (?, ?, ?, 6, ?, ?)
                                           """, (codice, qta_req - giac, origine, id_fornitore, bolla))
            except sqlite3.OperationalError:
                self.conn.cursor().execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_destinazione, tipo) VALUES (?, ?, ?, 6)", (codice, qta_req - giac, origine))

            self.conn.commit()
            popup.destroy()

            if tipo == 2:
                # Vendita forzata: resta nel flusso carrello/pagamento, non scarica subito
                item = {
                    'codice': codice,
                    'desc': articolo[0],
                    'colore': articolo[1],
                    'taglia': articolo[2],
                    'prezzo': articolo[4] or 0.0,
                    'qta': qta_req,
                    'origine': origine,
                    'destinazione': destinazione
                }
                self.carrello.append(item)
                self.aggiorna_ui_carrello()
                self.var_qta.set(1)
                item['id_riga_log'] = self.aggiorna_log(ora_attuale, nome_op, qta_req, codice, f"AGGIUNTO AL CARRELLO (forzato) - {articolo[0]}")
                return

            self.esegui_query_movimento(codice, origine, destinazione, tipo, nome_op, ora_attuale, articolo[0], qta_req, articolo[1], articolo[2], id_fornitore, bolla)
        ttk.Button(btn_frame, text="Annulla", command=annulla, bootstyle="secondary").pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text=f"Forza Allineamento (+{qta_req - giac})", command=forza, bootstyle="warning").pack(side=tk.LEFT, padx=10)

    def mostra_popup_nuovo_articolo(self, codice, tipo, origine, destinazione, nome_op, ora_attuale, qta, id_fornitore=None, bolla=None):
        popup = tk.Toplevel(self.root)
        popup.title("Nuovo Articolo")
        popup.geometry("450x330")
        popup.grab_set()
        form = ttk.Frame(popup)
        form.pack(fill=tk.BOTH, expand=True, ipadx=10, ipady=10)
        entries = {}
        for i, (label, key) in enumerate([("Descrizione:", "desc"), ("Colore:", "col"), ("Taglia:", "tag"), ("Costo Acq:", "acq"), ("Prezzo Ven:", "ven")]):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky=tk.E, pady=5)
            ent = ttk.Entry(form, width=30)
            ent.grid(row=i, column=1, pady=5)
            entries[key] = ent
        entries["desc"].focus()

        def salva(e=None):
            desc = entries["desc"].get().strip()
            if not desc: return

            colore_val = entries["col"].get()
            taglia_val = entries["tag"].get()

            acq_s, ven_s = entries["acq"].get().replace(',', '.'), entries["ven"].get().replace(',', '.')
            acq = float(acq_s) if acq_s.replace('.','',1).isdigit() else 0.0
            ven = float(ven_s) if ven_s.replace('.','',1).isdigit() else 0.0

            self.conn.cursor().execute("INSERT INTO articoli (codice, descrizione, colore, taglia, prezzo_acquisto, prezzo_vendita) VALUES (?, ?, ?, ?, ?, ?)", (codice, desc, colore_val, taglia_val, acq, ven))

            if tipo == 2:
                self.conn.cursor().execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_destinazione, tipo) VALUES (?, ?, ?, 1)", (codice, qta, origine))
                self.conn.commit()

                item = {
                    'codice': codice,
                    'desc': desc,
                    'colore': colore_val,
                    'taglia': taglia_val,
                    'prezzo': ven,
                    'qta': qta,
                    'origine': origine,
                    'destinazione': destinazione
                }
                self.carrello.append(item)
                self.aggiorna_ui_carrello()
                self.var_qta.set(1)
                item['id_riga_log'] = self.aggiorna_log(ora_attuale, nome_op, qta, codice, f"AGGIUNTO AL CARRELLO - {desc}")
                popup.destroy()
                return

            self.conn.commit()

            self.esegui_query_movimento(codice, origine, destinazione, tipo, nome_op, ora_attuale, desc, qta, colore_val, taglia_val, id_fornitore, bolla)
            popup.destroy()

        ttk.Button(popup, text="Salva", command=salva, bootstyle="success").pack(pady=10)
        popup.bind('<Return>', salva)

    def esegui_query_movimento(self, codice, origine, destinazione, tipo, nome_op, ora, desc, qta, colore, taglia, id_fornitore=None, bolla=None):
        cursor = self.conn.cursor()
        try:
            cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_origine, id_deposito_destinazione, tipo, id_fornitore, riferimento_bolla) VALUES (?, ?, ?, ?, ?, ?, ?)", (codice, qta, origine, destinazione, tipo, id_fornitore, bolla))
        except sqlite3.OperationalError:
            cursor.execute("INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_origine, id_deposito_destinazione, tipo) VALUES (?, ?, ?, ?, ?)", (codice, qta, origine, destinazione, tipo))
        id_movimento = cursor.lastrowid
        self.conn.commit()
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
    inizializza_database()
    root = ttk.Window(themename="minty")
    app = TerminaleMagazzino(root)
    root.mainloop()