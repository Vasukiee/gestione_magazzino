"""Logica di magazzino indipendente dalla GUI.

Tutto cio' che riguarda giacenze, rettifiche e parsing dei valori inseriti
dall'operatore vive qui, senza dipendenze da tkinter: e' il livello che i
test possono esercitare in modo headless.
"""

NEGOZIO = 1
BOX = 2

# Tipi di movimento
CARICO = 1
SCARICO = 2
RESO_CLIENTE = 3
RESO_FORNITORE = 4
TRASFERIMENTO = 5
RETTIFICA_POSITIVA = 6
RETTIFICA_NEGATIVA = 7
MODIFICA_ANAGRAFICA = 8

NOMI_OPERAZIONE = {
    CARICO: 'Carico',
    SCARICO: 'Scarico',
    RESO_CLIENTE: 'Reso Cliente',
    RESO_FORNITORE: 'Reso Fornitore',
    TRASFERIMENTO: 'Trasferimento Interno',
    RETTIFICA_POSITIVA: 'Rettifica Positiva',
    RETTIFICA_NEGATIVA: 'Rettifica Negativa',
    MODIFICA_ANAGRAFICA: 'Modifica Anagrafica',
}


class ValoreNonValido(ValueError):
    """Input dell'operatore che non e' interpretabile: va segnalato, non silenziato."""

    def __init__(self, campo, valore):
        self.campo = campo
        self.valore = valore
        super().__init__(f"'{valore}' non e' un valore valido per il campo {campo}.")


def parse_prezzo(testo, campo="prezzo"):
    """Converte il testo di un campo prezzo in float.

    Vuoto -> 0.0. Qualsiasi altra cosa non interpretabile solleva
    ValoreNonValido: prima un prezzo scritto male diventava silenziosamente
    0.00 e l'articolo veniva venduto gratis.
    """
    if testo is None:
        return 0.0
    # Il simbolo di valuta si toglie solo ai bordi: '12€50' deve essere
    # rifiutato, non interpretato come 1250.
    pulito = str(testo).strip().strip('€').strip().replace(',', '.')
    if not pulito:
        return 0.0
    try:
        valore = float(pulito)
    except ValueError:
        raise ValoreNonValido(campo, testo)
    if valore != valore or valore in (float('inf'), float('-inf')):
        raise ValoreNonValido(campo, testo)
    if valore < 0:
        raise ValoreNonValido(campo, testo)
    return round(valore, 2)


def parse_quantita(testo, campo="quantita", minimo=0):
    """Converte il testo di un campo quantita in int, rifiutando input invalidi."""
    if testo is None or str(testo).strip() == '':
        raise ValoreNonValido(campo, testo)
    try:
        valore = int(str(testo).strip())
    except ValueError:
        raise ValoreNonValido(campo, testo)
    if valore < minimo:
        raise ValoreNonValido(campo, testo)
    return valore


def giacenza(conn, codice, deposito):
    """Giacenza di un articolo in un singolo deposito, letta dalla vista."""
    colonna = 'giac_negozio' if deposito == NEGOZIO else 'giac_box'
    riga = conn.execute(
        f"SELECT {colonna} FROM v_giacenze WHERE codice = ?", (codice,)
    ).fetchone()
    return riga[0] if riga else 0


def giacenze(conn, codice):
    """Coppia (negozio, box) per un articolo."""
    riga = conn.execute(
        "SELECT giac_negozio, giac_box FROM v_giacenze WHERE codice = ?", (codice,)
    ).fetchone()
    return (riga[0], riga[1]) if riga else (0, 0)


def registra_rettifica(cursor, codice, deposito, delta, nota=None):
    """Scrive il movimento di rettifica che porta la giacenza del delta richiesto.

    delta > 0 -> rettifica positiva (tipo 6), delta < 0 -> negativa (tipo 7).
    Non fa commit: deve stare nella transazione del chiamante.
    """
    if delta == 0:
        return None
    if delta > 0:
        cursor.execute(
            "INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_destinazione, tipo, riferimento_bolla) "
            "VALUES (?, ?, ?, ?, ?)",
            (codice, delta, deposito, RETTIFICA_POSITIVA, nota),
        )
    else:
        cursor.execute(
            "INSERT INTO movimenti_magazzino (codice, quantita, id_deposito_origine, tipo, riferimento_bolla) "
            "VALUES (?, ?, ?, ?, ?)",
            (codice, abs(delta), deposito, RETTIFICA_NEGATIVA, nota),
        )
    return cursor.lastrowid


def quantita_in_carrello(carrello, codice):
    """Pezzi dello stesso articolo gia' nel carrello ma non ancora scaricati."""
    return sum(riga['qta'] for riga in carrello if riga['codice'] == codice)
