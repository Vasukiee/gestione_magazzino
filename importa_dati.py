import csv
import sqlite3
import sys

def pulisci_prezzo(valore_str):
    if not valore_str:
        return 0.0
    # Rimuove l'euro, gli spazi, i punti delle migliaia e converte la virgola decimale
    pulito = valore_str.replace('€', '').replace(' ', '').replace('.', '').replace(',', '.')
    try:
        return float(pulito)
    except ValueError:
        return 0.0

def esegui_migrazione():
    file_csv = 'dati.csv'
    
    try:
        # Rilevamento automatico del delimitatore (virgola o punto e virgola)
        with open(file_csv, 'r', encoding='utf-8') as f:
            prima_riga = f.readline()
            delimitatore = ';' if ';' in prima_riga else ','
            
        conn = sqlite3.connect('magazzino.db')
        cursor = conn.cursor()
        
        conteggio_articoli = 0
        conteggio_movimenti = 0
        
        with open(file_csv, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=delimitatore)
            
            for riga in reader:
                # Salta le righe vuote o troppo corte
                if not riga or len(riga) < 8:
                    continue
                    
                codice = str(riga[0]).strip()
                
                # Salta le righe di intestazione e i titoli del foglio
                if not codice or 'CODICE' in codice.upper() or 'MAGAZZINO' in codice.upper():
                    continue
                    
                # Mappatura indici in base allo screenshot:
                # 0: CODICE, 1: Colonna 1 (ignorata), 2: DESCRIZIONE, 3: COLORE
                # 4: TG, 5: PREZZO UNITARIO, 6: PREZZO DI VENDITA, 7: GIACENZA
                descrizione = str(riga[2]).strip()
                colore = str(riga[3]).strip()
                taglia = str(riga[4]).strip()
                
                prezzo_acq = pulisci_prezzo(riga[5])
                prezzo_ven = pulisci_prezzo(riga[6])
                
                try:
                    giacenza = int(riga[7].strip())
                except ValueError:
                    giacenza = 0

                # 1. Inserimento in Anagrafica (Articoli)
                # INSERT OR IGNORE previene crash se il foglio ha codici duplicati
                cursor.execute("""
                    INSERT OR IGNORE INTO articoli 
                    (codice, descrizione, colore, taglia, prezzo_acquisto, prezzo_vendita) 
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (codice, descrizione, colore, taglia, prezzo_acq, prezzo_ven))
                
                conteggio_articoli += 1

                # 2. Generazione Movimento Giacenza Iniziale (Solo se > 0)
                if giacenza > 0:
                    cursor.execute("""
                        INSERT INTO movimenti_magazzino 
                        (codice, quantita, id_deposito_destinazione, tipo) 
                        VALUES (?, ?, 1, 1)
                    """, (codice, giacenza))
                    conteggio_movimenti += 1
                    
        conn.commit()
        conn.close()
        
        print("--- MIGRAZIONE COMPLETATA ---")
        print(f"Articoli elaborati e inseriti in anagrafica: {conteggio_articoli}")
        print(f"Movimenti di carico (Negozio) generati: {conteggio_movimenti}")

    except FileNotFoundError:
        print(f"ERRORE: File '{file_csv}' non trovato nella directory corrente.")
    except Exception as e:
        print(f"ERRORE CRITICO durante l'importazione: {e}")

def importa_storico_passivo():
    conn = sqlite3.connect('magazzino.db')
    cursor = conn.cursor()

    # Mappatura colonne reale dei file (verificata sui CSV):
    # storico_carico.csv  -> CODICE, DESCRIZIONE, COLORE, TG, QUANTITA, DATA
    # storico_scarico.csv -> CODICE, DESCRIZIONE, COLORE, TG, GIACENZA, VENDUTO, DATA
    file_storico = [
        ('storico_carico.csv', 1, 4, 5),   # tipo=1 (carico), idx_quantita=4, idx_data=5
        ('storico_scarico.csv', 2, 5, 6),  # tipo=2 (scarico), idx_quantita=5 (VENDUTO), idx_data=6
    ]

    for file_name, tipo_movimento, idx_quantita, idx_data in file_storico:
        try:
            # Recupera i codici già presenti in anagrafica per segnalare eventuali mancanti
            cursor.execute("SELECT codice FROM articoli")
            codici_validi = {r[0] for r in cursor.fetchall()}

            with open(file_name, 'r', encoding='utf-8') as f:
                prima_riga = f.readline()
                delimitatore = ';' if ';' in prima_riga else ','
                f.seek(0)
                reader = csv.reader(f, delimiter=delimitatore)

                conteggio = 0
                saltate_corte = 0
                saltate_quantita = 0
                codici_mancanti = set()

                for riga in reader:
                    if not riga or len(riga) <= idx_quantita:
                        saltate_corte += 1
                        continue

                    codice = str(riga[0]).strip()

                    # Salta titolo del foglio (es. "CARICO", "SCARICO MERCE"),
                    # riga vuota e l'intestazione vera (es. "CODICE,...")
                    if not codice or 'CODICE' in codice.upper() or codice.upper() in ('CARICO', 'SCARICO MERCE'):
                        continue

                    try:
                        quantita = int(str(riga[idx_quantita]).strip())
                    except ValueError:
                        saltate_quantita += 1
                        continue

                    if codice not in codici_validi:
                        codici_mancanti.add(codice)

                    data_ora = str(riga[idx_data]).strip() if len(riga) > idx_data and riga[idx_data].strip() else "2023-01-01 00:00:00"

                    if tipo_movimento == 1:
                        # Carico: il deposito di destinazione è il Negozio (id 1)
                        cursor.execute("""
                            INSERT INTO movimenti_magazzino 
                            (codice, quantita, id_deposito_destinazione, tipo, data_ora, storico_passivo) 
                            VALUES (?, ?, 1, ?, ?, 1)
                        """, (codice, quantita, tipo_movimento, data_ora))
                    else:
                        # Scarico: il deposito di origine è il Negozio (id 1)
                        cursor.execute("""
                            INSERT INTO movimenti_magazzino 
                            (codice, quantita, id_deposito_origine, tipo, data_ora, storico_passivo) 
                            VALUES (?, ?, 1, ?, ?, 1)
                        """, (codice, quantita, tipo_movimento, data_ora))

                    conteggio += 1

                print(f"Importati {conteggio} record storici da {file_name}")
                if saltate_corte:
                    print(f"  (righe troppo corte/non valide saltate: {saltate_corte})")
                if saltate_quantita:
                    print(f"  (righe con quantità non numerica saltate: {saltate_quantita})")
                if codici_mancanti:
                    print(f"  ATTENZIONE: {len(codici_mancanti)} codici non presenti in anagrafica 'articoli' (importati comunque, ma orfani):")
                    for c in sorted(codici_mancanti)[:10]:
                        print(f"    - {c}")
                    if len(codici_mancanti) > 10:
                        print(f"    ... e altri {len(codici_mancanti) - 10}")

        except FileNotFoundError:
            print(f"ATTENZIONE: File '{file_name}' non trovato. Salto importazione.")
        except sqlite3.OperationalError as e:
             if 'storico_passivo' in str(e):
                 print("ERRORE: Colonna 'storico_passivo' non trovata. Eseguire prima python setup_db.py.")
                 break
             else:
                 print(f"ERRORE SQLite: {e}")
        except Exception as e:
            print(f"ERRORE durante l'importazione di {file_name}: {e}")
            
    conn.commit()
    conn.close()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--storico':
        importa_storico_passivo()
    else:
        print("Avvio migrazione giacenze iniziali...")
        esegui_migrazione()
        print("\nPer importare i file storici usa il comando: python importa_dati.py --storico")