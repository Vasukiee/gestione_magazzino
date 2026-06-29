# -*- coding: utf-8 -*-
"""
Stampa etichette barcode Code128 su NiimBot B1 Pro via USB seriale.

Il codice stampato è il campo `codice` dell'articolo (PK di `articoli`
in magazzino.db). L'immagine viene generata al volo ad ogni stampa.

Dipendenze:
    pip install niimprint pyserial python-barcode pillow

Permessi seriale (una tantum):
    sudo usermod -aG uucp $USER    # Arch Linux
    sudo usermod -aG dialout $USER # Debian / Ubuntu
    # Poi logout/login per applicare il gruppo

CLI:
    python stampa_etichetta_niimbot.py <CODICE>   # stampa etichetta
    python stampa_etichetta_niimbot.py test        # diagnostica strisce
    python stampa_etichetta_niimbot.py nero        # diagnostica tutto nero
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
PORTA_SERIALE  = "auto"  # "auto" = rilevamento automatico, oppure "/dev/ttyACM0"
DENSITA_STAMPA = 3       # densità di stampa 1–5 (3 = default)

# ---------------------------------------------------------------------------
# Geometria etichetta — 3 × 5 cm, 300 DPI
# ---------------------------------------------------------------------------
LABEL_W_MM  = 50          # larghezza etichetta (direzione testina)   = 5 cm
LABEL_H_MM  = 30          # altezza etichetta  (direzione avanzamento) = 3 cm
DPI         = 300
DOTS_PER_MM = DPI / 25.4  # ≈ 11.81 dot/mm
MARGIN_MM   = 2           # margine su ogni bordo in mm

# ---------------------------------------------------------------------------
# Orientamento — modificare solo se il risultato appare ruotato o capovolto
# ---------------------------------------------------------------------------
ROTATE_BARCODE = False  # True = ruota il barcode di 90° (barre orizzontali)
FLIP_FEED      = True   # True = capovolge l'immagine prima della stampa

# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
DEBUG_SAVE_IMAGE = False  # True = salva PNG in /tmp/ e salta la stampa reale


# ---------------------------------------------------------------------------
# Funzioni interne
# ---------------------------------------------------------------------------

def _trova_porta_stampante() -> str:
    """Individua la porta seriale USB della stampante, ignorando le porte fantasma."""
    porte = [p for p, _d, hwid in list_comports() if hwid != "n/a"]
    if len(porte) == 1:
        return porte[0]
    if not porte:
        raise RuntimeError(
            "Nessuna porta seriale USB trovata. "
            "Verificare il cavo e i permessi (gruppo uucp/dialout)."
        )
    raise RuntimeError(
        f"Più porte seriali rilevate: {porte}. "
        "Impostare PORTA_SERIALE manualmente."
    )


def _log_info_stampante(client: PrinterClient) -> None:
    """Stampa su stdout le informazioni di base della stampante."""
    from niimprint.printer import InfoEnum
    campi = {
        "DEVICETYPE":  InfoEnum.DEVICETYPE,
        "HARDVERSION": InfoEnum.HARDVERSION,
        "SOFTVERSION": InfoEnum.SOFTVERSION,
        "BATTERY":     InfoEnum.BATTERY,
    }
    info = {nome: "n/a" for nome in campi}
    for nome, chiave in campi.items():
        try:
            info[nome] = client.get_info(chiave)
        except Exception:
            pass
    print(
        f"[niimbot] DEVICETYPE={info['DEVICETYPE']}  "
        f"HW={info['HARDVERSION']}  SW={info['SOFTVERSION']}  "
        f"BATTERY={info['BATTERY']}  DPI={DPI}",
        flush=True,
    )


def _connetti_stampante() -> PrinterClient:
    porta = _trova_porta_stampante() if PORTA_SERIALE == "auto" else PORTA_SERIALE
    transport = SerialTransport(port=porta)
    client = PrinterClient(transport)
    _log_info_stampante(client)
    return client


def _debug_salva_immagine(immagine: Image.Image, nome: str) -> None:
    if not DEBUG_SAVE_IMAGE:
        return
    import datetime
    ts = datetime.datetime.now().strftime("%H%M%S")
    path = f"/tmp/niimbot_{nome}_{ts}_{immagine.width}x{immagine.height}.png"
    immagine.save(path)
    print(f"[debug] immagine salvata: {path}", flush=True)


def _prepara_per_stampa(img: Image.Image) -> Image.Image:
    """Applica l'eventuale ribaltamento pre-stampa."""
    if FLIP_FEED:
        img = img.rotate(180)
    return img


# ---------------------------------------------------------------------------
# Generazione immagine
# ---------------------------------------------------------------------------

def genera_immagine_barcode(codice: str) -> Image.Image:
    """
    Restituisce un canvas PIL in scala di grigi con il barcode Code128 centrato.

    Dimensioni canvas: LABEL_W_MM × LABEL_H_MM a DOTS_PER_MM dot/mm.
    Il barcode occupa l'area al netto di MARGIN_MM su ogni lato.

    Approccio two-pass:
      1. Genera a module_width=1.0 per misurare la larghezza prodotta.
      2. Scala module_width proporzionalmente → il barcode esce già vicino
         alla larghezza target, minimizzando il resize sulle barre.
      3. Ridimensiona solo l'altezza (non critica per la leggibilità).
    """
    canvas_w = round(LABEL_W_MM * DOTS_PER_MM)
    canvas_h = round(LABEL_H_MM * DOTS_PER_MM)
    target_w = round((LABEL_W_MM - 2 * MARGIN_MM) * DOTS_PER_MM)
    target_h = round((LABEL_H_MM - 2 * MARGIN_MM) * DOTS_PER_MM)

    # Prima passata: misura larghezza prodotta a module_width di riferimento
    buf = io.BytesIO()
    barcode.get("code128", codice, writer=ImageWriter()).write(
        buf, options={"module_width": 1.0, "module_height": 10.0,
                      "quiet_zone": 4, "write_text": False}
    )
    buf.seek(0)
    ref_w = Image.open(buf).size[0]

    # Seconda passata: module_width scalato per coprire target_w
    buf = io.BytesIO()
    barcode.get("code128", codice, writer=ImageWriter()).write(
        buf, options={
            "module_width":  target_w / ref_w,
            "module_height": 15.0,
            "quiet_zone":    4,
            "write_text":    True,
            "font_size":     9,
            "text_distance": 3,
        }
    )
    buf.seek(0)
    src = Image.open(buf).convert("L")

    if ROTATE_BARCODE:
        src = src.rotate(-90, expand=True)

    # Se il barcode supera il canvas, clampa la larghezza; ridimensiona l'altezza
    bcode_w = min(src.size[0], canvas_w)
    src = src.resize((bcode_w, target_h), Image.NEAREST)

    # Converti in B/N netto (le stampanti termiche non usano grigi)
    src = src.point(lambda p: 255 if p > 128 else 0)

    # Centra nel canvas su sfondo bianco
    canvas = Image.new("L", (canvas_w, canvas_h), 255)
    canvas.paste(src, ((canvas_w - bcode_w) // 2, (canvas_h - target_h) // 2))
    return canvas


# ---------------------------------------------------------------------------
# API pubblica
# ---------------------------------------------------------------------------

def stampa_etichetta_articolo(codice: str) -> tuple[bool, str]:
    """
    Genera il barcode dal codice articolo e lo invia alla NiimBot B1 Pro.
    Ritorna (successo: bool, messaggio: str).
    """
    try:
        immagine = genera_immagine_barcode(codice)
        _debug_salva_immagine(immagine, codice)
        print(f"[stampa] {codice}  canvas={immagine.size}", flush=True)

        if DEBUG_SAVE_IMAGE:
            return True, "[DEBUG] Immagine generata e salvata, stampa reale saltata."

        client = _connetti_stampante()
        client.print_image(_prepara_per_stampa(immagine), density=DENSITA_STAMPA)
        return True, f"Etichetta '{codice}' stampata."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore durante la stampa: {e}"


# ---------------------------------------------------------------------------
# Funzioni diagnostiche
# ---------------------------------------------------------------------------

def stampa_test_strisce() -> tuple[bool, str]:
    """
    Stampa tre strisce nere in posizioni note nel canvas:
      A — bordo superiore  (y = 0 .. 9)
      B — centro           (y = canvas_h//2 - 5 .. canvas_h//2 + 4)
      C — bordo inferiore  (y = canvas_h-10 .. canvas_h-1)

    Permette di misurare orientamento e scala DPI reali sull'etichetta fisica.
    """
    try:
        canvas_w = round(LABEL_W_MM * DOTS_PER_MM)
        canvas_h = round(LABEL_H_MM * DOTS_PER_MM)
        mid = canvas_h // 2
        img = Image.new("L", (canvas_w, canvas_h), 255)
        draw = ImageDraw.Draw(img)
        draw.rectangle([(0, 0),          (canvas_w - 1, 9)],              fill=0)  # A
        draw.rectangle([(0, mid - 5),    (canvas_w - 1, mid + 4)],        fill=0)  # B
        draw.rectangle([(0, canvas_h - 10), (canvas_w - 1, canvas_h - 1)], fill=0) # C
        print(f"[test] strisce  canvas={img.size}  mid={mid}", flush=True)
        _debug_salva_immagine(img, "test_strisce")
        client = _connetti_stampante()
        client.print_image(_prepara_per_stampa(img), density=DENSITA_STAMPA)
        return True, "Test strisce inviato."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore: {e}"


def stampa_test_nero_totale() -> tuple[bool, str]:
    """Canvas completamente nero: verifica che l'area stampata coincida con il canvas."""
    try:
        canvas_w = round(LABEL_W_MM * DOTS_PER_MM)
        canvas_h = round(LABEL_H_MM * DOTS_PER_MM)
        img = Image.new("L", (canvas_w, canvas_h), 0)
        print(f"[test] nero totale  canvas={img.size}", flush=True)
        _debug_salva_immagine(img, "test_nero")
        client = _connetti_stampante()
        client.print_image(_prepara_per_stampa(img), density=DENSITA_STAMPA)
        return True, "Test nero totale inviato."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore: {e}"


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso:")
        print("  python stampa_etichetta_niimbot.py <CODICE>   # stampa etichetta")
        print("  python stampa_etichetta_niimbot.py test        # diagnostica strisce")
        print("  python stampa_etichetta_niimbot.py nero        # diagnostica nero totale")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "test":
        ok, msg = stampa_test_strisce()
    elif cmd == "nero":
        ok, msg = stampa_test_nero_totale()
    else:
        ok, msg = stampa_etichetta_articolo(cmd)

    print(msg)
    sys.exit(0 if ok else 1)
