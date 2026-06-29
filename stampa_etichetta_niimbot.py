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
import math
import struct
import time
import traceback

import barcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageOps
from serial.tools.list_ports import comports as list_comports

from niimprint import PrinterClient, SerialTransport
from niimprint.packet import NiimbotPacket


# ---------------------------------------------------------------------------
# Configurazione stampante
# ---------------------------------------------------------------------------
PORTA_SERIALE  = "auto"  # "auto" = rilevamento automatico, oppure "/dev/ttyACM0"
DENSITA_STAMPA = 3       # densità di stampa 1–5 (3 = default)

# ---------------------------------------------------------------------------
# Geometria etichetta — 3 × 5 cm
# ---------------------------------------------------------------------------
# IMPORTANTE — densità reale misurata sull'hardware.
#   La testina del B1 Pro ha 384 dot fisici (limite della libreria niimprint).
#   Inviando un canvas largo 384 px, sull'etichetta esce una banda larga 33 mm:
#   384 dot / 33 mm ≈ 11.64 dot/mm (≈ 295 DPI, coerente con la testina "300 DPI").
#   Quindi DOTS_PER_MM DEVE valere ~11.64, NON 8: con 8 px/mm il canvas è troppo
#   corto e la stampante stampa solo una fascia (sintomo: nero/numeri solo in cima).
#   Nota fisica: la testina copre al massimo 384 dot ≈ 33 mm, quindi dei 50 mm di
#   larghezza dell'etichetta se ne stampano solo ~33; è un limite hardware.
# Le due direzioni hanno scale DIVERSE (misurate sull'hardware):
#   - TESTINA (larghezza): 384 dot ÷ ~34 mm ≈ 11.3 dot/mm
#   - AVANZAMENTO (altezza): 1200 righe ÷ 30 mm = 40 righe/mm
# Il motore di avanzamento gira a risoluzione maggiore della testina, quindi
# servono molte più righe per riempire l'altezza. Usare la stessa scala per
# entrambe (l'errore precedente) faceva uscire il contenuto schiacciato in
# una fascia di ~6 mm in cima all'etichetta.
# ATTENZIONE: superare ~1500 righe totali fa scartare l'intera pagina alla
# stampante (esce bianca). Con 40 righe/mm i 30 mm = 1200 righe, entro il limite.
DOTS_PER_MM      = 384 / 34    # ≈ 11.3 dot/mm — scala TESTINA (larghezza)
FEED_DOTS_PER_MM = 40          # 40 righe/mm   — scala AVANZAMENTO (altezza)
LABEL_W_MM  = 50          # larghezza etichetta (direzione testina)   = 5 cm
LABEL_H_MM  = 30          # altezza etichetta  (direzione avanzamento) = 3 cm
MARGIN_MM   = 2           # margine su ogni bordo in mm
BARCODE_H_MM = 10         # altezza del barcode (barre + testo); centrato in altezza
# Larghezza massima in pixel inviabile alla testina. La libreria assume 384
# (B1 base), ma il B1 Pro potrebbe coprire i 50 mm pieni → ~565 px. Test in corso.
HEAD_MAX_PX = round(LABEL_W_MM * DOTS_PER_MM)  # ≈ 565 px (50 mm pieni)

# ---------------------------------------------------------------------------
# Orientamento — modificare solo se il risultato appare ruotato o capovolto
# ---------------------------------------------------------------------------
ROTATE_BARCODE = False  # True = ruota il barcode di 90° (barre orizzontali)
FLIP_FEED      = False  # True = capovolge l'immagine prima della stampa
OFFSET_FEED_MM = 0     # sposta il contenuto verso il basso (+) o l'alto (−) in mm

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
        f"BATTERY={info['BATTERY']}  DOTS_PER_MM={DOTS_PER_MM}",
        flush=True,
    )


def _connetti_stampante() -> PrinterClient:
    porta = _trova_porta_stampante() if PORTA_SERIALE == "auto" else PORTA_SERIALE
    transport = SerialTransport(port=porta)
    # Scarta eventuali byte residui di una sessione precedente: senza questo,
    # le risposte vecchie nel buffer fanno fraintendere i pacchetti e START_PRINT
    # / SET_DIMENSION ricevono una risposta di errore (ValueError).
    try:
        transport._serial.reset_input_buffer()
        transport._serial.reset_output_buffer()
    except Exception:
        pass
    client = PrinterClient(transport)
    _log_info_stampante(client)
    return client


def _chiudi_stampante(client: PrinterClient) -> None:
    """Chiude la porta seriale sottostante.

    La libreria niimprint non chiude mai il `serial.Serial`: senza questa
    chiusura la porta resta occupata e la stampa successiva trova la
    stampante in stato inconsistente (risposta di errore a START_PRINT).
    """
    if client is None:
        return
    try:
        client._transport._serial.close()
    except Exception:
        pass
    # La stampante impiega un istante a tornare pronta dopo il rilascio
    # della porta; senza questa pausa la stampa immediatamente successiva
    # può trovarla ancora occupata.
    time.sleep(0.5)


def _invia_immagine(client: PrinterClient, img: Image.Image, density: int) -> None:
    """Invia l'immagine al B1 Pro usando il protocollo NIIMBOT *V4*.

    La `print_image` di niimprint v0.1.0 usa il protocollo vecchio:
      - PrintStart  con payload da 1 byte   (V4 ne vuole 9)
      - SetDimension con payload da 4 byte  (V4 vuole SetPageSize da 13)
      - righe bitmap 0x85 con black-count = 0 e senza run-length
    Il firmware V4 del B1 Pro accetta (echo) quei comandi ma ignora la
    dimensione pagina e ripiega su un avanzamento minimo (~6 mm), troncando
    il resto dell'etichetta. Qui invece inviamo la sequenza V4 corretta:
    PrintStart(9) → SetPageSize(13) → PageStart → righe 0x84/0x85 con il
    conteggio dei pixel neri → PageEnd → PrintEnd.

    Riferimenti protocollo:
      https://printers.niim.blue/interfacing/print-tasks/
      protocol-v4 (niimbot-web-bluetooth)
    """
    def invia(reqcode, data):
        client._send(NiimbotPacket(reqcode, data))

    def transceive(reqcode, data, respoffset=1):
        # come PrinterClient._transceive ma tollerante: ritorna la prima
        # risposta col codice atteso, ignorando lo stato.
        return client._transceive(reqcode, data, respoffset)

    h, w = img.height, img.width

    # --- Init ---
    client.set_label_density(density)
    client.set_label_type(1)
    # PrintStart V4 (9 byte): pagine=1, speed=density, resto padding
    transceive(0x01, struct.pack(">H5BBB", 1, 0, 0, 0, 0, 0, density, 0))

    # --- Pagina ---
    # SetPageSize V4 (13 byte): righe(H), colonne(W), costante 00 01, padding
    transceive(0x13, struct.pack(">HHBB7B", h, w, 0x00, 0x01, 0, 0, 0, 0, 0, 0, 0))
    # PrintStatus (0xA3): nel flusso V4 si interroga lo stato senza attendere,
    # niimprint non lo richiede come ack quindi inviamo il pacchetto raw.
    try:
        invia(0xA3, b"\x01")
    except Exception:
        pass

    # --- Righe immagine ---
    # I pacchetti riga vengono bufferizzati e spediti in un'unica write:
    # con ~1800 righe l'invio pacchetto-per-pacchetto è troppo lento e la
    # stampante va in underrun, annulla la pagina ed espelle l'etichetta bianca.
    bw = ImageOps.invert(img.convert("L")).convert("1")
    stride = math.ceil(w / 8)
    blob = bytearray()
    for y in range(bw.height):
        bits = "".join("1" if bw.getpixel((x, y)) else "0" for x in range(w))
        nero = bits.count("1")
        if nero == 0:
            # PrintEmptyRow (0x84): row(2) + run(1)
            blob += NiimbotPacket(0x84, struct.pack(">HB", y, 1)).to_bytes()
            continue
        data = int(bits, 2).to_bytes(stride, "big")
        # PrintBitmapRow (0x85): row(2,BE) + 00 + total_nero(2,LE) + run(1) + bitmap
        header = struct.pack(">H", y) + b"\x00" + struct.pack("<H", nero) + b"\x01"
        blob += NiimbotPacket(0x85, header + data).to_bytes()
    client._transport._serial.write(bytes(blob))
    client._transport._serial.flush()

    # --- Fine ---
    transceive(0xE3, b"\x01")          # PageEnd
    time.sleep(0.3)
    while not client.end_print():       # PrintEnd (0xF3) con polling
        time.sleep(0.1)


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

    Dimensioni canvas: LABEL_W_MM × LABEL_H_MM a DOTS_PER_MM dot/mm
    (8 px/mm, la risoluzione assunta da niimprint per il B1), con la
    larghezza limitata a HEAD_MAX_PX. Il barcode occupa l'area al netto
    di MARGIN_MM su ogni lato.

    Approccio two-pass:
      1. Genera a module_width=1.0 per misurare la larghezza prodotta.
      2. Scala module_width proporzionalmente → il barcode esce già vicino
         alla larghezza target, minimizzando il resize sulle barre.
      3. Ridimensiona solo l'altezza (non critica per la leggibilità).
    """
    # Larghezza → scala TESTINA (DOTS_PER_MM), limitata a HEAD_MAX_PX.
    # Altezza  → scala AVANZAMENTO (FEED_DOTS_PER_MM).
    # IMPORTANTE: il canvas è alto SOLO quanto il barcode più un piccolo
    # margine, NON i 30 mm pieni dell'etichetta. La stampante stampa dall'alto
    # del canvas verso il basso: un canvas alto 30 mm con il barcode in mezzo
    # lascerebbe ~1.5 cm di bianco in cima e farebbe uscire un'etichetta bianca.
    canvas_w = min(round(LABEL_W_MM * DOTS_PER_MM), HEAD_MAX_PX)
    target_w = min(round((LABEL_W_MM - 2 * MARGIN_MM) * DOTS_PER_MM), canvas_w)
    target_h = round(BARCODE_H_MM * FEED_DOTS_PER_MM)
    # Il canvas copre l'intera altezza etichetta (1200 righe): una pagina più
    # corta viene espulsa bianca dalla stampante (non rileva il gap). Il barcode
    # va posizionato IN CIMA, così il bianco resta sotto e non in testa.
    canvas_h = round(LABEL_H_MM * FEED_DOTS_PER_MM)

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
            "module_height": 30.0,
            "quiet_zone":    4,
            "write_text":    True,
            "font_size":     7,
            "text_distance": 2,
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

    # Posiziona nel canvas: barcode con margine in alto (MARGIN_MM) più
    # l'eventuale offset di feed (positivo = più in basso).
    canvas = Image.new("L", (canvas_w, canvas_h), 255)
    pos_y = round((MARGIN_MM + max(0, OFFSET_FEED_MM)) * FEED_DOTS_PER_MM)
    pos_y = max(0, min(canvas_h - target_h, pos_y))
    canvas.paste(src, ((canvas_w - bcode_w) // 2, pos_y))
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
        try:
            _invia_immagine(client, _prepara_per_stampa(immagine), DENSITA_STAMPA)
        finally:
            _chiudi_stampante(client)
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
        canvas_w = min(round(LABEL_W_MM * DOTS_PER_MM), HEAD_MAX_PX)
        canvas_h = round(LABEL_H_MM * FEED_DOTS_PER_MM)
        mid = canvas_h // 2
        img = Image.new("L", (canvas_w, canvas_h), 255)
        draw = ImageDraw.Draw(img)
        draw.rectangle([(0, 0),          (canvas_w - 1, 9)],              fill=0)  # A
        draw.rectangle([(0, mid - 5),    (canvas_w - 1, mid + 4)],        fill=0)  # B
        draw.rectangle([(0, canvas_h - 10), (canvas_w - 1, canvas_h - 1)], fill=0) # C
        print(f"[test] strisce  canvas={img.size}  mid={mid}", flush=True)
        _debug_salva_immagine(img, "test_strisce")
        client = _connetti_stampante()
        try:
            _invia_immagine(client, _prepara_per_stampa(img), DENSITA_STAMPA)
        finally:
            _chiudi_stampante(client)
        return True, "Test strisce inviato."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore: {e}"


def stampa_test_nero_totale() -> tuple[bool, str]:
    """Canvas completamente nero: verifica che l'area stampata coincida con il canvas."""
    try:
        canvas_w = min(round(LABEL_W_MM * DOTS_PER_MM), HEAD_MAX_PX)
        canvas_h = round(LABEL_H_MM * FEED_DOTS_PER_MM)
        img = Image.new("L", (canvas_w, canvas_h), 0)
        print(f"[test] nero totale  canvas={img.size}", flush=True)
        _debug_salva_immagine(img, "test_nero")
        client = _connetti_stampante()
        try:
            _invia_immagine(client, _prepara_per_stampa(img), DENSITA_STAMPA)
        finally:
            _chiudi_stampante(client)
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
