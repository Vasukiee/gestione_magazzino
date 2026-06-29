# CLAUDE.md — Gestione Magazzino

## Project Overview

This is a **desktop warehouse management system** ("gestionale") for a small Italian retail shop. It is a standalone Python/tkinter application with SQLite storage, barcode label printing support, and automated daily shutdown via systemd. All UI, variable names, and comments are in Italian.

---

## Repository Structure

```
gestione_magazzino/
├── main.py                       # Main application (GUI + business logic, ~1377 lines)
├── setup_db.py                   # Database schema creation and migrations
├── importa_dati.py               # CSV import utilities for initial data loading
├── stampa_etichetta_niimbot.py   # NiimBot B1 Pro label printer integration
├── avvia_gestionale.sh           # Shell launcher (activates venv, starts main.py)
├── Istruzioni.txt                # Setup and installation instructions
├── README.md                     # Minimal project header
├── .gitignore                    # Python-standard; excludes *.db, .venv, backups
└── /etc/systemd/system/
    ├── spegnimento-negozio.service   # Shutdown service
    └── spegnimento-negozio.timer     # Daily shutdown at 21:00
```

Runtime directories (not committed):
- `backup/rolling/` — rolling backups, last 16 kept
- `backup/daily/`   — daily backup on application close
- `magazzino.db`    — SQLite database

---

## Technology Stack

| Layer        | Technology                                       |
|--------------|--------------------------------------------------|
| Language     | Python 3.x                                       |
| GUI          | `tkinter` + `ttkbootstrap` (modern themes)       |
| Database     | `sqlite3` (built-in)                             |
| Label print  | `niimprint`, `pyserial`, `python-barcode`, `pillow` |
| Data I/O     | `csv` (built-in)                                 |
| Concurrency  | `threading` (backup worker, async print jobs)    |
| System       | `systemd` timer for automated daily shutdown     |

No web framework, no external APIs, no Docker, no CI/CD pipeline.

---

## Development Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install ttkbootstrap niimprint pyserial python-barcode pillow

# Initialize the database
python setup_db.py

# Import initial product catalog (requires dati.csv in cwd)
python importa_dati.py

# Import historical data (requires storico_carico.csv, storico_scarico.csv)
python importa_dati.py --storico

# Launch the application
./avvia_gestionale.sh
# or directly:
python main.py
```

For NiimBot printer serial port access:
```bash
sudo usermod -aG uucp $USER   # or use a udev rule
# Re-login for group changes to take effect
```

---

## Database Schema

Managed by `setup_db.py`. Migrations are applied via dedicated `_migra_*` functions that use `ALTER TABLE` with `sqlite3.OperationalError` catching for backwards compatibility.

| Table                | Purpose                                | Key columns |
|----------------------|----------------------------------------|-------------|
| `articoli`           | Product catalogue                      | `codice` (PK), `descrizione`, `colore`, `taglia`, `prezzo_acquisto`, `prezzo_vendita`, `soglia_minima` |
| `depositi`           | Warehouse locations                    | `id` (1=Negozio, 2=Box), `nome_deposito` |
| `fornitori`          | Suppliers                              | `id`, `ragione_sociale` (UNIQUE) |
| `transazioni`        | Payment transactions                   | `id`, `data_ora`, `totale`, `metodo_pagamento` |
| `movimenti_magazzino`| All inventory movements                | `id`, `codice`, `quantita`, `tipo`, `id_deposito_origine`, `id_deposito_destinazione`, `data_ora`, `id_fornitore`, `riferimento_bolla`, `id_transazione`, `storico_passivo`, `nome_fornitore_storico` |

Movement `tipo` values:
- `1` — Carico (inbound/purchase)
- `2` — Scarico (sale)
- `3` — Reso Cliente (customer return)
- `4` — Reso Fornitore (supplier return)
- `6`, `7`, `8` — Rettifica (inventory correction variants)

Stock calculation for a warehouse X:
```
giacenza = SUM(quantita WHERE id_deposito_destinazione=X) - SUM(quantita WHERE id_deposito_origine=X)
```
The `storico_passivo=1` flag marks historical import rows — they follow different aggregation logic.

---

## Application Architecture

### Central Class: `TerminaleMagazzino` (`main.py`)

The entire application lives in a single class. There are no separate model, controller, or service layers.

```
TerminaleMagazzino
├── __init__()            — DB connection, instance variables, UI bootstrap
├── setup_ui()            — Notebook with 5 tabs
├── setup_tab_movimenti() — Inventory operations tab
├── setup_tab_trasferimenti() — Internal transfers tab
├── setup_tab_ricerca()   — Search & history tab
├── setup_tab_statistiche() — Analytics tab
├── setup_tab_fornitori() — Supplier CRUD tab
└── on_closing()          — Graceful shutdown + daily backup
```

### Method Naming Conventions

| Prefix       | Purpose                                        |
|--------------|------------------------------------------------|
| `setup_*`    | UI widget creation and layout                  |
| `esegui_*`   | Execute a business operation (DB write + UI)   |
| `aggiorna_*` | Refresh/redraw a UI section from DB            |
| `mostra_*`   | Show a dialog or popup                         |
| `carica_*`   | Load data from DB into a widget                |
| `salva_*`    | Persist form data to DB                        |
| `elimina_*`  | Delete a record                                |
| `on_*`       | Event handler callbacks                        |
| `_*`         | Private helper methods                         |

All names are in Italian.

### Key Instance Variables

```python
self.conn              # sqlite3.Connection (check_same_thread=False)
self.carrello          # list[dict] — current sales cart
self.movimenti_log     # dict: treeview_id -> db_id (enables undo)
self.tipo_movimento    # tk.IntVar — radiobutton: carico/scarico/reso
self._pending_doppio_codice1  # state for dual-barcode label flow
```

---

## Common Code Patterns

### SQLite Queries
```python
cursor = self.conn.cursor()
cursor.execute("SELECT ... WHERE codice = ?", (codice,))
row = cursor.fetchone()
# or
rows = cursor.fetchall()
self.conn.commit()
```

### Tkinter Widget Layout
```python
frame = ttk.Frame(parent)
frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

ttk.Label(frame, text="Etichetta", font=('Helvetica', 12)).pack(side=tk.LEFT)
self.tree = ttk.Treeview(frame, columns=colonne, show='headings')
self.tree.heading('col', text='Colonna')
self.tree.column('col', width=100, anchor=tk.E)
self.tree.pack(fill=tk.BOTH, expand=True)
self.tree.bind('<Double-1>', self.on_double_click)
```

### Async Operations (printing / backups)
```python
def esegui():
    ok, msg = stampa_etichetta_articolo(codice)
    self.root.after(0, lambda: self._fine_stampa(ok, msg))
threading.Thread(target=esegui, daemon=True).start()
```

### Error Handling
```python
try:
    # DB operation
except sqlite3.IntegrityError:
    messagebox.showerror("Errore", "Codice già presente")
except Exception as e:
    messagebox.showerror("Errore", str(e))
```

### CSV Export
```python
path = filedialog.asksaveasfilename(
    defaultextension=".csv",
    initialfile=f"export_{datetime.now().strftime('%Y%m%d')}.csv"
)
if path:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(intestazioni)
        writer.writerows(dati)
```

### Schema Migration
```python
def _migra_nuova_colonna(cursor):
    try:
        cursor.execute("ALTER TABLE articoli ADD COLUMN nuova_col TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
```

---

## Label Printing Module (`stampa_etichetta_niimbot.py`)

Standalone module — can also be run directly from CLI.

```bash
# Single label
python stampa_etichetta_niimbot.py CODICE

# Dual label (two barcodes stacked on one label)
python stampa_etichetta_niimbot.py doppio CODICE1 CODICE2

# Hardware diagnostics
python stampa_etichetta_niimbot.py test   # stripe pattern
python stampa_etichetta_niimbot.py nero   # full-black test
```

Key constants at the top of the file:
- `PORTA_SERIALE` — serial port (auto-detected, default `ttyACM0`)
- `DENSITA_STAMPA` — print density
- `LABEL_HEAD_MM` — label width
- `DEBUG_SAVE_IMAGE` — set to `True` to save generated images to `/tmp/` without printing

---

## Testing

There is **no automated test suite**. The project is tested manually via the GUI.

Built-in debug facilities:
- `DEBUG_SAVE_IMAGE = True` in `stampa_etichetta_niimbot.py` saves label images to `/tmp/` for inspection without a connected printer
- `stampa_test_strisce()` and `stampa_test_nero_totale()` functions provide hardware calibration prints

---

## Git Conventions

- **Branch for AI work**: `claude/claude-md-docs-qsh6ln`
- Commit messages are written in Italian, plain imperative style (e.g., `"Aggiunto sistema di backup"`)
- No conventional-commits format (no `feat:`, `fix:` prefixes)
- `*.db`, `.venv/`, `backup/`, `.claude/` are in `.gitignore` — never commit them

---

## Key Business Rules

1. **Dual warehouse**: Inventory is tracked separately for *Negozio* (shop floor, id=1) and *Box* (storage, id=2).
2. **Minimum stock threshold**: `soglia_minima` defaults to 2 units; the app warns when stock falls below it.
3. **Movement undo**: Double-clicking a movement row in the log allows cancellation (creates a reversal movement, does not delete the original).
4. **Historical data**: Rows with `storico_passivo=1` represent pre-system imports and are excluded from some aggregations.
5. **Supplier name snapshot**: `nome_fornitore_storico` preserves the supplier name in movement rows even if the supplier record is later deleted.
6. **Rolling backups**: Every 30 minutes during operation; the last 16 rolling backups are kept. A final daily backup is saved on clean application exit.
7. **Automatic shutdown**: A systemd timer triggers a system poweroff at 21:00 daily — this is intentional store-closing automation.
