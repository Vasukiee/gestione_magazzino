# -*- coding: utf-8 -*-
"""
Modulo per generare e stampare etichette a barcode (Code128) su NiimBot B1 Pro,
collegata via cavo USB (porta seriale).

Il contenuto del barcode e' SEMPRE il campo `codice` dell'articolo (chiave
primaria della tabella `articoli` in magazzino.db). Nessuna persistenza
separata: l'etichetta viene generata al volo al momento della stampa.

Dipendenze:
    pip install niimprint pyserial python-barcode pillow

Note collegamento:
    - NiimBot B1 Pro collegata via cavo USB: /dev/ttyACM0 o /dev/ttyUSB0.
    - L'utente deve essere nel gruppo "uucp" (Arch) o "dialout" (Debian/Ubuntu):
          sudo usermod -aG uucp $USER
      Poi logout/login, oppure udev rule con TAG+="uaccess".
"""

import io
import traceback

import barcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw
from serial.tools.list_ports import comports as list_comports

from niimprint import PrinterClient, SerialTransport


# ---------------------------------------------------------------------------
# Configurazione stampante
# ---------------------------------------------------------------------------
PORTA_SERIALE  = "auto"
DENSITA_STAMPA = 3   # 1–5; 3 è un buon default
TIPO_ETICHETTA = 1   # 1 = etichetta con gap (la più comune)

# ---------------------------------------------------------------------------
# Geometria etichetta
# ---------------------------------------------------------------------------
#
# HEAD_DOTS_PER_MM — risoluzione della testina (direzione larghezza etichetta).
#   Confermato 300 DPI dalla scheda tecnica NiimBot B1 Pro.
#   300 DPI / 25.4 mm/inch ≈ 11.81 dot/mm.
#
HEAD_DOTS_PER_MM = 300 / 25.4   # ≈ 11.81

#
# FEED_DOTS_PER_MM — risoluzione del motore (direzione avanzamento nastro).
#
#   ATTENZIONE: il motore del B1 Pro avanza a 600 DPI (23.62 step/mm), ma
#   accetta esattamente LABEL_FEED_MM × HEAD_DOTS_PER_MM step per etichetta
#   (≈354 step per 30mm). Inviare più di 354 righe causa troncamento.
#
#   Quindi FEED_DOTS_PER_MM DEVE restare uguale a HEAD_DOTS_PER_MM per
#   mantenere l'altezza del canvas a 354 righe. I contenuti vengono fisicamente
#   compressi 2:1 dal motore (15mm fisici per 354 righe). Questo è il limite
#   hardware — non è modificabile via software senza supporto firmware.
#
FEED_DOTS_PER_MM = HEAD_DOTS_PER_MM   # NON cambiare: vedi nota sopra

# Alias mantenuto per compatibilità con eventuali riferimenti diretti.
DOTS_PER_MM = HEAD_DOTS_PER_MM

#
# LABEL_HEAD_MM / LABEL_FEED_MM
#   La libreria niimprint manda una scanline per ogni riga dell'immagine:
#     image.width  = dot per scanline = ampiezza testina di stampa
#     image.height = numero di scanline = dimensione lungo l'uscita nastro
#
#   Per etichetta 30 mm (altezza) × 50 mm (larghezza) su nastro da 50 mm:
#     LABEL_HEAD_MM = 50  (attraversa la testina)
#     LABEL_FEED_MM = 30  (direzione uscita nastro)
#
#   Se il barcode stampato è ruotato di 90°, scambia i due valori.
#
LABEL_HEAD_MM = 50
LABEL_FEED_MM = 30
MARGIN_MM     = 2    # margine su ogni bordo in mm

#
# ROTATE_BARCODE
#   Il barcode Code128 è generato in landscape (barre verticali, lettura
#   orizzontale). Con False viene inviato senza rotazione.
#   Se le barre appaiono orizzontali sullo stampato, imposta True.
#
ROTATE_BARCODE = False

#
# FLIP_FEED
#   Se True, l'immagine viene capovolta (ruotata 180°) prima della stampa.
#   Prova: True se il contenuto appare al bordo sbagliato dell'etichetta.
#
FLIP_FEED = True

#
# DEBUG_SAVE_IMAGE
#   Se True, l'immagine generata viene salvata in /tmp/ prima della stampa.
#   Utile per verificare la composizione senza sprecare etichette.
#
DEBUG_SAVE_IMAGE = False


# ---------------------------------------------------------------------------
# Funzioni interne
# ---------------------------------------------------------------------------

def _trova_porta_stampante() -> str:
    """Rileva la porta seriale USB reale, ignorando le porte ttyS fantasma."""
    porte_reali = [p for p, _d, hwid in list_comports() if hwid != "n/a"]
    if len(porte_reali) == 1:
        return porte_reali[0]
    if not porte_reali:
        raise RuntimeError(
            "Nessuna porta seriale USB trovata. "
            "Controllare il collegamento della stampante."
        )
    raise RuntimeError(
        f"Più porte seriali USB trovate: {porte_reali}. "
        "Specificare PORTA_SERIALE manualmente."
    )


def _log_info_stampante(client: PrinterClient) -> None:
    """Interroga la stampante e stampa su stderr le info disponibili."""
    from niimprint.printer import InfoEnum
    labels = {
        "DEVICETYPE":  InfoEnum.DEVICETYPE,
        "HARDVERSION": InfoEnum.HARDVERSION,
        "SOFTVERSION": InfoEnum.SOFTVERSION,
        "BATTERY":     InfoEnum.BATTERY,
    }
    info = {}
    for nome, chiave in labels.items():
        try:
            info[nome] = client.get_info(chiave)
        except Exception:
            info[nome] = "n/a"
    print(
        f"[niimbot] DEVICETYPE={info['DEVICETYPE']}  "
        f"HW={info['HARDVERSION']}  SW={info['SOFTVERSION']}  "
        f"BATTERY={info['BATTERY']}  "
        f"DOTS_PER_MM={DOTS_PER_MM:.2f}",
        flush=True,
    )


def _connetti_stampante() -> PrinterClient:
    porta = _trova_porta_stampante() if PORTA_SERIALE == "auto" else PORTA_SERIALE
    transport = SerialTransport(port=porta)
    client = PrinterClient(transport)
    _log_info_stampante(client)
    return client


def _genera_barcode_px(
    codice: str,
    canvas_w: int,
    canvas_h: int,
    target_w: int,
    target_h: int,
) -> Image.Image:
    """
    Nucleo di generazione barcode. Restituisce un canvas PIL di (canvas_w × canvas_h)
    con il barcode centrato, largo target_w e alto target_h.

    Strategia resize:
    - Prima passata con module_width=1.0 mm per misurare la larghezza di riferimento.
    - Seconda passata con module_width scalato → il barcode esce già vicino a
      target_w, minimizzando il resize sull'asse di lettura (quello critico).
    - Solo l'altezza (non critica) viene forzata a target_h con NEAREST.
    """
    # Prima passata: misura la larghezza a module_width di riferimento
    opts_ref = {"module_width": 1.0, "module_height": 10.0,
                "quiet_zone": 4, "write_text": False}
    c_ref = barcode.get("code128", codice, writer=ImageWriter())
    buf_ref = io.BytesIO()
    c_ref.write(buf_ref, options=opts_ref)
    buf_ref.seek(0)
    ref_w = Image.open(buf_ref).size[0]

    # Seconda passata: module_width calibrato per avvicinarsi a target_w
    module_w = 1.0 * target_w / ref_w
    opts = {
        "module_width":  module_w,
        "module_height": 15.0,
        "font_size":     9,
        "text_distance": 3,
        "quiet_zone":    4,
        "write_text":    True,
    }
    c = barcode.get("code128", codice, writer=ImageWriter())
    buf = io.BytesIO()
    c.write(buf, options=opts)
    buf.seek(0)
    src = Image.open(buf).convert("L")

    if ROTATE_BARCODE:
        src = src.rotate(-90, expand=True)

    bcode_w = src.size[0]
    if bcode_w > canvas_w:
        src = src.resize((canvas_w, target_h), Image.NEAREST)
        bcode_w = canvas_w
    else:
        src = src.resize((bcode_w, target_h), Image.NEAREST)

    # Soglia hard B/N (NEAREST può lasciare grigi sul testo renderizzato)
    src = src.point(lambda p: 255 if p > 128 else 0)

    canvas = Image.new("L", (canvas_w, canvas_h), 255)
    off_x  = (canvas_w - bcode_w) // 2
    off_y  = (canvas_h - target_h) // 2
    canvas.paste(src, (off_x, off_y))
    return canvas


# ---------------------------------------------------------------------------
# API pubblica
# ---------------------------------------------------------------------------

def genera_immagine_barcode(codice: str) -> Image.Image:
    """Genera l'immagine etichetta con un singolo barcode a piena area."""
    head_px  = round(LABEL_HEAD_MM * HEAD_DOTS_PER_MM)
    feed_px  = round(LABEL_FEED_MM * FEED_DOTS_PER_MM)
    tgt_head = round((LABEL_HEAD_MM - 2 * MARGIN_MM) * HEAD_DOTS_PER_MM)
    tgt_feed = round((LABEL_FEED_MM - 2 * MARGIN_MM) * FEED_DOTS_PER_MM)
    return _genera_barcode_px(codice, head_px, feed_px, tgt_head, tgt_feed)


def genera_immagine_doppio_barcode(codice1: str, codice2: str) -> Image.Image:
    """
    Genera un'etichetta con due barcode impilati verticalmente.

    Il canvas è 591×354 (50mm×30mm a 300 DPI). Il motore del B1 Pro
    avanza a 600 DPI, quindi fisicamente vengono stampati solo ~15mm
    di nastro per etichetta: non superare 354 righe nel canvas.

    Layout (margini minimi per massimizzare le barre):
    ┌──────────────────────────┐  y=0
    │  [BARCODE  codice1]      │  y=4..172  (168 px ≈ 7mm fisici)
    ├──────────────────────────┤  y=174-178 separatore
    │  [BARCODE  codice2]      │  y=180..348 (168 px ≈ 7mm fisici)
    └──────────────────────────┘  y=354
    """
    head_px  = round(LABEL_HEAD_MM * HEAD_DOTS_PER_MM)
    feed_px  = round(LABEL_FEED_MM * FEED_DOTS_PER_MM)
    tgt_head = round((LABEL_HEAD_MM - 2 * MARGIN_MM) * HEAD_DOTS_PER_MM)

    # Margini minimi: 0.3mm esterno (4 px), 0.5mm interno (6 px per lato)
    outer_px = 4
    inner_px = 6
    mid_y    = feed_px // 2

    top_h    = mid_y - outer_px - inner_px
    bottom_h = (feed_px - mid_y) - outer_px - inner_px

    top_img    = _genera_barcode_px(codice1, head_px, top_h,    tgt_head, top_h)
    bottom_img = _genera_barcode_px(codice2, head_px, bottom_h, tgt_head, bottom_h)

    canvas = Image.new("L", (head_px, feed_px), 255)
    canvas.paste(top_img,    (0, outer_px))
    canvas.paste(bottom_img, (0, mid_y + inner_px))

    # Separatore centrale: 3 righe visibili (scuro/bianco/scuro)
    draw = ImageDraw.Draw(canvas)
    draw.line([(0, mid_y - 2), (head_px - 1, mid_y - 2)], fill=60,  width=1)
    draw.line([(0, mid_y - 1), (head_px - 1, mid_y - 1)], fill=200, width=1)
    draw.line([(0, mid_y),     (head_px - 1, mid_y)],     fill=60,  width=1)

    return canvas


def _debug_salva_immagine(immagine: Image.Image, nome: str) -> None:
    if not DEBUG_SAVE_IMAGE:
        return
    import datetime
    ts = datetime.datetime.now().strftime("%H%M%S")
    path = f"/tmp/niimbot_{nome}_{ts}_{immagine.width}x{immagine.height}.png"
    immagine.save(path)
    print(f"[debug] immagine salvata: {path}", flush=True)


def _prepara_per_stampa(img: Image.Image) -> Image.Image:
    """Applica trasformazioni pre-stampa (es. flip) secondo la configurazione."""
    if FLIP_FEED:
        img = img.rotate(180)
    return img


def stampa_etichetta_articolo(codice: str) -> tuple[bool, str]:
    """
    Genera il barcode dal codice articolo e lo invia alla NiimBot B1 Pro.
    Ritorna (successo: bool, messaggio: str).
    """
    try:
        immagine = genera_immagine_barcode(codice)
        _debug_salva_immagine(immagine, "singolo")
        print(f"[stampa] singolo  image.size={immagine.size}  (width x height)", flush=True)

        if DEBUG_SAVE_IMAGE:
            return True, f"[DEBUG] Immagine '{codice}' generata e salvata, stampa reale saltata (DEBUG_SAVE_IMAGE=True)."

        client = _connetti_stampante()
        client.print_image(_prepara_per_stampa(immagine), density=DENSITA_STAMPA)
        return True, f"Etichetta '{codice}' stampata."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore durante la stampa: {e}"



def stampa_doppio_barcode(codice1: str, codice2: str) -> tuple[bool, str]:
    """
    Genera un'etichetta con due barcode (codice1 in alto, codice2 in basso)
    e la invia alla NiimBot B1 Pro.
    Ritorna (successo: bool, messaggio: str).
    """
    try:
        immagine = genera_immagine_doppio_barcode(codice1, codice2)
        _debug_salva_immagine(immagine, "doppio")
        print(f"[stampa] doppio   image.size={immagine.size}  (width x height)", flush=True)
        client = _connetti_stampante()
        client.print_image(_prepara_per_stampa(immagine), density=DENSITA_STAMPA)
        return True, f"Etichetta doppia '{codice1}' + '{codice2}' stampata."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore durante la stampa: {e}"


def stampa_test_strisce() -> tuple[bool, str]:
    """
    Stampa un'immagine diagnostica con 3 strisce nere a posizioni note nel canvas:
      Striscia A: y=0..9    (bordo SUPERIORE del canvas)
      Striscia B: y=172..181 (CENTRO del canvas)
      Striscia C: y=344..353 (bordo INFERIORE del canvas)

    Osserva sull'etichetta fisica:
    - Quante strisce compaiono?
    - In che ordine dal bordo d'uscita verso l'alto?
    - Quanto sono fisicamente distanti?

    Questo rivela orientamento e scala DPI effettivi.
    """
    try:
        head_px = round(LABEL_HEAD_MM * HEAD_DOTS_PER_MM)
        feed_px = round(LABEL_FEED_MM * FEED_DOTS_PER_MM)
        img = Image.new("L", (head_px, feed_px), 255)
        draw = ImageDraw.Draw(img)
        # Striscia A — bordo superiore
        draw.rectangle([(0, 0),   (head_px - 1, 9)],   fill=0)
        # Striscia B — centro
        draw.rectangle([(0, 172), (head_px - 1, 181)],  fill=0)
        # Striscia C — bordo inferiore
        draw.rectangle([(0, 344), (head_px - 1, 353)],  fill=0)
        print(f"[test] immagine strisce: {img.size}", flush=True)
        _debug_salva_immagine(img, "strisce")
        client = _connetti_stampante()
        client.print_image(_prepara_per_stampa(img), density=DENSITA_STAMPA)
        return True, "Test strisce inviato."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore: {e}"


def stampa_test_nero_totale() -> tuple[bool, str]:
    """
    Stampa un canvas completamente nero (591×354).
    Se tutta l'etichetta diventa nera → il canvas copre l'intera etichetta.
    Se solo una fascia è nera → quella fascia è l'area effettivamente stampata.
    Misura la fascia nera con un righello per ottenere mm/canvas.
    """
    try:
        head_px = round(LABEL_HEAD_MM * HEAD_DOTS_PER_MM)
        feed_px = round(LABEL_FEED_MM * FEED_DOTS_PER_MM)
        img = Image.new("L", (head_px, feed_px), 0)   # tutto nero
        print(f"[test] rettangolo nero: {img.size}", flush=True)
        _debug_salva_immagine(img, "nero")
        client = _connetti_stampante()
        client.print_image(_prepara_per_stampa(img), density=DENSITA_STAMPA)
        return True, "Test nero totale inviato."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore: {e}"


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "singolo"
    if cmd == "test":
        ok, msg = stampa_test_strisce()
    elif cmd == "nero":
        ok, msg = stampa_test_nero_totale()
    elif cmd == "doppio" and len(sys.argv) >= 4:
        ok, msg = stampa_doppio_barcode(sys.argv[2], sys.argv[3])
    else:
        codice_test = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "singolo" else "TEST001"
        ok, msg = stampa_etichetta_articolo(codice_test)
    print(msg)
