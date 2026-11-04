# AGENTS.md — Gestione Magazzino

## Project Overview

This is a **desktop warehouse management system** ("gestionale") for a small Italian retail shop. It is a standalone Python/tkinter application with SQLite storage, barcode label printing on a Brother QL-700, self-update from GitHub, and automated daily shutdown via systemd. All UI, variable names, and comments are in Italian.

---

## Repository Structure

```
gestione_magazzino/
├── main.py                       # Main application (GUI + business logic, ~1550 lines)
├── db.py                         # Shared paths (DB, backup, log) and connetti() factory
├── inventario.py                 # GUI-free stock logic: giacenze, rettifiche, parsing input
├── setup_db.py                   # Database schema creation, migrations and views
├── aggiornamento.py              # Self-update from GitHub (git fast-forward, rollback)
├── importa_dati.py               # CSV import utilities for initial data loading
├── stampa_etichetta_brother.py   # Brother QL-700 label printing via CUPS (in use)
├── stampa_etichetta_niimbot.py   # Legacy NiimBot B1 Pro integration (no longer used by main.py)
├── test_gestionale.py            # Headless regression tests (stdlib unittest)
├── avvia_gestionale.sh           # Shell launcher (activates venv, starts main.py)
├── Istruzioni.txt                # Setup, printer configuration and calibration notes
├── README.md                     # Project overview for humans
├── CLAUDE.md                     # Same guide as this file, for Claude Code
├── .gitignore                    # Python-standard; excludes *.db, .venv, backups
├── ⁄etc⁄systemd⁄system⁄spegnimento-negozio.service   # Shutdown service
└── ⁄etc⁄systemd⁄system⁄spegnimento-negozio.timer     # Daily shutdown at 21:00
```

Note: the two systemd files sit in the repository root. Their names contain the
Unicode "fraction slash" (`⁄`, U+2044), not a real `/`, so they are plain files,
not a directory tree. They are meant to be copied to `/etc/systemd/system/`.

Runtime files (not committed; paths defined in `db.py`, anchored to the project folder):
- `magazzino.db`    — SQLite database (WAL mode)
- `backup/rolling/` — rolling backups, last 16 kept
- `backup/daily/`   — daily backup on application close
- `gestionale.log`  — application log

---

## Technology Stack

| Layer        | Technology                                                  |
|--------------|-------------------------------------------------------------|
| Language     | Python 3.x                                                  |
| GUI          | `tkinter` + `ttkbootstrap` (modern themes)                  |
| Database     | `sqlite3` (built-in), WAL, foreign keys on                  |
| Label print  | `python-barcode`, `pillow`, CUPS (`lp`) → Brother QL-700    |
| Updates      | `git` via `subprocess` (`aggiornamento.py`)                 |
| Data I/O     | `csv` (built-in)                                            |
| Concurrency  | `threading` (backup worker, update check, async print jobs) |
| Logging      | `logging` to `gestionale.log`                               |
| Tests        | `unittest` (built-in)                                       |
| System       | `systemd` timer for automated daily shutdown                |

No web framework, no Docker, no CI/CD pipeline.

---

## Development Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install ttkbootstrap python-barcode pillow

# Initialize / migrate the database
python setup_db.py

# Import initial product catalog (requires dati.csv in cwd)
python importa_dati.py

# Import historical data (requires storico_carico.csv, storico_scarico.csv)
python importa_dati.py --storico

# Run the tests (headless, no display needed)
python -m unittest test_gestionale -v

# Launch the application
./avvia_gestionale.sh
# or directly:
python main.py
```

Brother QL-700 printer setup (driver, CUPS queue named `QL700`, calibration) is
described step by step in `Istruzioni.txt`.

---

## Database Schema

Managed by `setup_db.py` (`inizializza_database(conn=None)`). Migrations are applied via dedicated `_migra_*` functions that inspect `PRAGMA table_info` and run `ALTER TABLE` only when a column is missing.

| Table                | Purpose                                | Key columns |
|----------------------|----------------------------------------|-------------|
| `articoli`           | Product catalogue                      | `codice` (PK), `descrizione`, `colore`, `taglia`, `prezzo_acquisto`, `prezzo_vendita`, `soglia_minima`, `attivo` |
| `depositi`           | Warehouse locations                    | `id` (1=Negozio, 2=Box), `nome_deposito` |
| `fornitori`          | Suppliers                              | `id`, `ragione_sociale` (UNIQUE) |
| `transazioni`        | Payment transactions                   | `id`, `data_ora`, `totale`, `metodo_pagamento` |
| `movimenti_magazzino`| All inventory movements                | `id`, `codice`, `quantita`, `tipo`, `id_deposito_origine`, `id_deposito_destinazione`, `data_ora`, `id_fornitore`, `riferimento_bolla`, `id_transazione`, `storico_passivo`, `nome_fornitore_storico` |

View `v_giacenze` (created by `_crea_viste`) exposes `codice`, `giac_negozio`, `giac_box`. Use it instead of re-writing the stock formula in new queries.

Movement `tipo` values (constants in `inventario.py`):
- `1` — `CARICO` (inbound/purchase)
- `2` — `SCARICO` (sale)
- `3` — `RESO_CLIENTE` (customer return)
- `4` — `RESO_FORNITORE` (supplier return)
- `5` — `TRASFERIMENTO` (internal transfer Negozio ↔ Box)
- `6` — `RETTIFICA_POSITIVA`
- `7` — `RETTIFICA_NEGATIVA`
- `8` — `MODIFICA_ANAGRAFICA`

Stock calculation for a warehouse X:
```
giacenza = SUM(quantita WHERE id_deposito_destinazione=X) - SUM(quantita WHERE id_deposito_origine=X)
```
Only rows with `storico_passivo=0` count towards stock. `storico_passivo=1` marks historical import rows and cancelled movements.

---

## Application Architecture

### Modules

- `db.py` — `DB_PATH`, `BACKUP_*_DIR`, `LOG_PATH` and `connetti()`, which opens a connection with WAL, `busy_timeout` and `foreign_keys=ON`. Every module must get its connection and paths from here.
- `inventario.py` — logic with no tkinter dependency: `giacenza()`, `giacenze()`, `registra_rettifica()` (does not commit), `parse_prezzo()` / `parse_quantita()` (raise `ValoreNonValido` on bad input instead of silently returning 0), `quantita_in_carrello()`. Put new testable business logic here.
- `aggiornamento.py` — `controlla()`, `applica()`, `rollback(commit)`. Updates only via `git pull --ff-only`; refuses with uncommitted local changes or divergent history; every git command has a timeout.
- `main.py` — the GUI.

### Central Class: `TerminaleMagazzino` (`main.py`)

```
TerminaleMagazzino
├── __init__()                 — DB connection, lock, instance variables, UI bootstrap
├── transazione()              — context manager: commit on success, rollback on error
├── scrivi()                   — runs a write inside transazione() and shows errors
├── esegui_backup()            — online backup (sqlite3.backup) + atomic os.replace
├── setup_ui()                 — Notebook with 5 tabs + fixed footer
├── setup_tab_movimenti()      — Inventory operations tab
├── setup_tab_trasferimenti()  — Internal transfers tab
├── setup_tab_ricerca()        — Search & history tab
├── setup_tab_statistiche()    — Analytics, backup status, updates
├── setup_tab_fornitori()      — Supplier CRUD tab
├── controlla_aggiornamenti()  — update check in a background thread
└── on_closing()               — Graceful shutdown + daily backup
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
| `elimina_*`  | Delete (or archive) a record                   |
| `on_*`       | Event handler callbacks                        |
| `_*`         | Private helper methods                         |

All names are in Italian.

### Key Instance Variables

```python
self.conn              # sqlite3.Connection from db.connetti(check_same_thread=False)
self.db_lock           # threading.RLock — every write and every backup takes it
self.carrello          # list[dict] — current sales cart
self.movimenti_log     # dict: treeview_id -> db_id (enables undo)
self.tipo_movimento    # tk.IntVar — radiobutton: carico/scarico/reso
self.mostra_archiviati # tk.BooleanVar — include archived articles in search
```

---

## Common Code Patterns

### Database writes
```python
with self.transazione() as cursor:
    cursor.execute("UPDATE articoli SET prezzo_vendita = ? WHERE codice = ?", (prezzo, codice))
    inventario.registra_rettifica(cursor, codice, NEGOZIO, delta)
# commit on exit, rollback on exception
```
Do not call `self.conn.commit()` by hand after partial writes: a failed operation must leave nothing pending on the shared connection.

### Parsing operator input
```python
try:
    prezzo = parse_prezzo(entry.get(), "Prezzo vendita")
except ValoreNonValido as e:
    messagebox.showerror("Valore non valido", str(e))
    return
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

### Async Operations (printing / backups / updates)
```python
def esegui():
    ok, msg = stampa_etichetta_articolo(codice)
    self.root.after(0, lambda: self._fine_stampa(ok, msg))
threading.Thread(target=esegui, daemon=True).start()
```
Never touch tkinter widgets from a worker thread: always go back through `self.root.after(0, ...)`.

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
    cursor.execute("PRAGMA table_info(articoli)")
    colonne = [col[1] for col in cursor.fetchall()]
    if 'nuova_col' not in colonne:
        cursor.execute("ALTER TABLE articoli ADD COLUMN nuova_col TEXT DEFAULT ''")
```
Add the call to the migration list in `inizializza_database()` and a test in `TestMigrazioni`.

---

## Label Printing Module (`stampa_etichetta_brother.py`)

Prints Code128 barcodes on a Brother QL-700 (62 mm DK continuous roll, 62×29 mm label) through the CUPS queue `QL700`. `main.py` imports `stampa_etichetta_articolo` from here.

```bash
python stampa_etichetta_brother.py CODICE   # print one label
python stampa_etichetta_brother.py test     # diagnostic rectangle
```

Key constants at the top of the file:
- `NOME_STAMPANTE` — CUPS queue name (`QL700`; do not use the A4 queue `Stampante`)
- `CUPS_PAGE_SIZE` — must be a format listed by `lpoptions -p QL700 -l` (custom PageSize is rejected)
- `LABEL_H_MM`, `MARGIN_TOP_MM`, `BARCODE_H_MM`, `TEXT_H_MM` — the only values to touch for calibration
- `DEBUG_SAVE_IMAGE` — set to `True` to save generated images to `/tmp/` without printing

`stampa_etichetta_niimbot.py` is kept for reference only; the NiimBot printer is no longer in use.

---

## Testing

```bash
python -m unittest test_gestionale -v
```

`test_gestionale.py` runs headless (no display, no `main.py` import) on an in-memory database and temporary git repositories. It covers parsing, stock calculation, rettifiche, cart quantities, soft delete, migrations, orphaned suppliers, online backup and the update logic. Run it before every commit; keep new business logic in `inventario.py` so it can be tested here.

The GUI itself is still tested manually. For labels, `DEBUG_SAVE_IMAGE = True` lets you inspect the output without a printer.

---

## Git Conventions

- AI work happens on the `claude/...` branch assigned to the session, merged through a pull request
- Commit messages are written in Italian, plain imperative or past-participle style (e.g., `"Aggiunto sistema di backup"`)
- No conventional-commits format (no `feat:`, `fix:` prefixes)
- `*.db`, `.venv/`, `backup/`, `.claude/` are in `.gitignore` — never commit them
- The shop machine updates itself by fast-forwarding `main`: never rewrite history on `main`

---

## Key Business Rules

1. **Dual warehouse**: Inventory is tracked separately for *Negozio* (shop floor, id=1) and *Box* (storage, id=2).
2. **Minimum stock threshold**: `soglia_minima` defaults to 2 units; the app warns when stock falls below it.
3. **Movement undo**: Double-clicking a movement row in the log cancels it by setting `storico_passivo = 1` (no DELETE) and updates the total of the linked transaction.
4. **Soft delete**: articles are archived (`attivo = 0`), never physically deleted, so their history and reports stay consistent. "Mostra archiviati" and "Ripristina" bring them back.
5. **Historical data**: Rows with `storico_passivo=1` represent pre-system imports or cancelled movements and are excluded from stock.
6. **Supplier name snapshot**: `nome_fornitore_storico` preserves the supplier name in movement rows even if the supplier record is later deleted.
7. **Invalid input is rejected**: a price or quantity that cannot be parsed stops the operation; it is never silently turned into 0.
8. **Rolling backups**: Every 30 minutes during operation (`INTERVALLO_BACKUP = 1800`); the last 16 are kept (`MAX_BACKUP_ROLLING`). A final daily backup is saved on clean application exit. Backups use the SQLite online backup API, never a file copy of the open database.
9. **Self-update**: at startup the app checks GitHub in the background; updating requires confirmation, takes a database backup first and restarts the app.
10. **Automatic shutdown**: A systemd timer triggers a system poweroff at 21:00 daily — this is intentional store-closing automation.
