# -*- coding: utf-8 -*-
"""
Stampa etichette barcode Code128 su Brother QL-700 tramite CUPS.

Il codice stampato e' il campo `codice` dell'articolo. L'immagine viene
generata al volo e inviata alla coda CUPS dedicata `QL700`.

Dipendenze:
    pip install python-barcode pillow

CLI:
    python stampa_etichetta_brother.py <CODICE>   # stampa etichetta
    python stampa_etichetta_brother.py test       # diagnostica rettangolo
"""

import datetime
import io
import os
import shutil
import subprocess
import tempfile
import traceback

import barcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont, ImageOps


# ---------------------------------------------------------------------------
# Configurazione stampante
# ---------------------------------------------------------------------------
NOME_STAMPANTE = "QL700"
DPI = 300
CUPS_PAGE_SIZE = "62x29"

# Rotolo Brother DK 62 mm. Il driver QL-700 non accetta PageSize custom via lp:
# usare un formato dichiarato nel PPD, altrimenti la stampante va in errore.
LABEL_W_MM = 62
LABEL_H_MM = 29
PRINTABLE_W_MM = 59
MARGIN_X_MM = 1.5
MARGIN_TOP_MM = 1.0
BARCODE_H_MM = 17.0
TEXT_GAP_MM = 0.8
TEXT_H_MM = 5.0

DEBUG_SAVE_IMAGE = False  # True = salva PNG in /tmp/ e salta la stampa reale


# ---------------------------------------------------------------------------
# Conversioni e utilita'
# ---------------------------------------------------------------------------

def _mm_to_px(mm: float) -> int:
    return round(mm * DPI / 25.4)


def _get_font(size: int):
    percorsi = [
        "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    for percorso in percorsi:
        try:
            return ImageFont.truetype(percorso, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _render_text(codice: str, max_w: int, max_h: int) -> Image.Image:
    size = max(10, max_h)
    while size >= 8:
        font = _get_font(size)
        try:
            left, top, right, bottom = font.getbbox(codice)
            tw, th = right - left, bottom - top
        except AttributeError:
            tw, th = font.getsize(codice)
            left, top = 0, 0

        if tw <= max_w and th <= max_h:
            img = Image.new("L", (tw + 4, th + 4), 255)
            ImageDraw.Draw(img).text((-left + 2, -top + 2), codice, font=font, fill=0)
            bbox = ImageOps.invert(img).getbbox()
            if bbox:
                img = img.crop(bbox)
            return img.point(lambda p: 255 if p > 128 else 0)
        size -= 1

    return Image.new("L", (1, 1), 255)


def _debug_salva_immagine(immagine: Image.Image, nome: str) -> str | None:
    if not DEBUG_SAVE_IMAGE:
        return None
    ts = datetime.datetime.now().strftime("%H%M%S")
    nome_sicuro = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in nome)
    path = f"/tmp/brother_ql700_{nome_sicuro}_{ts}_{immagine.width}x{immagine.height}.png"
    immagine.save(path)
    print(f"[debug] immagine salvata: {path}", flush=True)
    return path


def _verifica_coda_cups() -> None:
    if shutil.which("lpstat") is None or shutil.which("lp") is None:
        raise RuntimeError("Comandi CUPS non trovati: installare/configurare cups-client.")

    result = subprocess.run(
        ["lpstat", "-p", NOME_STAMPANTE],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Coda CUPS '{NOME_STAMPANTE}' non trovata. "
            "Configurare la Brother QL-700 come stampante CUPS dedicata."
        )

    result = subprocess.run(
        ["lpoptions", "-p", NOME_STAMPANTE, "-l"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and f" {CUPS_PAGE_SIZE} " not in f" {result.stdout} ":
        raise RuntimeError(
            f"Formato CUPS '{CUPS_PAGE_SIZE}' non supportato dalla coda '{NOME_STAMPANTE}'. "
            "Verificare che il modello sia 'Brother QL-700 CUPS'."
        )


def _invia_a_cups(immagine: Image.Image) -> str:
    _verifica_coda_cups()

    with tempfile.NamedTemporaryFile(prefix="ql700_", suffix=".png", delete=False) as tmp:
        path = tmp.name
    try:
        immagine.save(path)
        cmd = [
            "lp",
            "-d", NOME_STAMPANTE,
            "-n", "1",
            "-o", f"PageSize={CUPS_PAGE_SIZE}",
            "-o", "BrCutLabel=1",
            "-o", "BrCutAtEnd=ON",
            "-o", "BrTrimtape=ON",
            "-o", "BrMargin=3",
            "-o", "fitplot",
            path,
        ]
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            errore = result.stderr.strip() or result.stdout.strip() or "errore sconosciuto"
            raise RuntimeError(f"Errore CUPS: {errore}")
        return result.stdout.strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Generazione immagine
# ---------------------------------------------------------------------------

def genera_immagine_barcode(codice: str) -> Image.Image:
    """Restituisce un canvas PIL B/N con barcode Code128 e testo leggibile."""
    codice = str(codice).strip()
    if not codice:
        raise ValueError("Codice articolo vuoto.")

    canvas_w = _mm_to_px(LABEL_W_MM)
    canvas_h = _mm_to_px(LABEL_H_MM)
    printable_w = _mm_to_px(PRINTABLE_W_MM)
    margin_x = _mm_to_px(MARGIN_X_MM)
    target_w = min(printable_w - (2 * margin_x), canvas_w - (2 * margin_x))
    target_h = _mm_to_px(BARCODE_H_MM)
    text_h = _mm_to_px(TEXT_H_MM)

    buf = io.BytesIO()
    barcode.get("code128", codice, writer=ImageWriter()).write(
        buf,
        options={
            "module_width": 1.0,
            "module_height": 10.0,
            "quiet_zone": 4,
            "write_text": False,
        },
    )
    buf.seek(0)
    ref_w = Image.open(buf).size[0]

    buf = io.BytesIO()
    barcode.get("code128", codice, writer=ImageWriter()).write(
        buf,
        options={
            "module_width": target_w / ref_w,
            "module_height": 30.0,
            "quiet_zone": 4,
            "write_text": False,
        },
    )
    buf.seek(0)
    src = Image.open(buf).convert("L")

    bcode_w = min(src.width, target_w)
    src = src.resize((bcode_w, target_h), Image.NEAREST)
    src = src.point(lambda p: 255 if p > 128 else 0)

    canvas = Image.new("L", (canvas_w, canvas_h), 255)
    content_x = (canvas_w - printable_w) // 2
    x = content_x + (printable_w - bcode_w) // 2
    y = _mm_to_px(MARGIN_TOP_MM)
    canvas.paste(src, (x, y))

    testo = _render_text(codice, target_w, text_h)
    text_x = content_x + (printable_w - testo.width) // 2
    text_y = y + target_h + _mm_to_px(TEXT_GAP_MM)
    if text_y + testo.height <= canvas_h:
        canvas.paste(testo, (text_x, text_y))

    return canvas


def genera_immagine_test() -> Image.Image:
    """Immagine diagnostica per verificare orientamento e area stampabile."""
    canvas_w = _mm_to_px(LABEL_W_MM)
    canvas_h = _mm_to_px(LABEL_H_MM)
    printable_w = _mm_to_px(PRINTABLE_W_MM)
    content_x = (canvas_w - printable_w) // 2

    img = Image.new("L", (canvas_w, canvas_h), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle((content_x, 0, content_x + printable_w - 1, canvas_h - 1), outline=0, width=4)
    draw.line((content_x, canvas_h // 2, content_x + printable_w - 1, canvas_h // 2), fill=0, width=3)
    return img


# ---------------------------------------------------------------------------
# API pubblica
# ---------------------------------------------------------------------------

def stampa_etichetta_articolo(codice: str) -> tuple[bool, str]:
    """
    Genera il barcode dal codice articolo e lo invia alla Brother QL-700.
    Ritorna (successo: bool, messaggio: str).
    """
    try:
        immagine = genera_immagine_barcode(codice)
        debug_path = _debug_salva_immagine(immagine, codice)
        print(f"[stampa QL700] {codice} canvas={immagine.size}", flush=True)

        if DEBUG_SAVE_IMAGE:
            return True, f"[DEBUG] Immagine generata: {debug_path}"

        cups_msg = _invia_a_cups(immagine)
        dettaglio = f" ({cups_msg})" if cups_msg else ""
        return True, f"Etichetta '{codice}' inviata a CUPS{dettaglio}."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore durante la stampa: {e}"


def stampa_test() -> tuple[bool, str]:
    try:
        immagine = genera_immagine_test()
        debug_path = _debug_salva_immagine(immagine, "test")

        if DEBUG_SAVE_IMAGE:
            return True, f"[DEBUG] Immagine test generata: {debug_path}"

        cups_msg = _invia_a_cups(immagine)
        dettaglio = f" ({cups_msg})" if cups_msg else ""
        return True, f"Test QL-700 inviato a CUPS{dettaglio}."
    except Exception as e:
        traceback.print_exc()
        return False, f"Errore durante il test: {e}"


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso:")
        print("  python stampa_etichetta_brother.py <CODICE>   # stampa etichetta")
        print("  python stampa_etichetta_brother.py test       # diagnostica area")
        sys.exit(1)

    if sys.argv[1] == "test":
        ok, msg = stampa_test()
    else:
        ok, msg = stampa_etichetta_articolo(sys.argv[1])

    print(msg)
    sys.exit(0 if ok else 1)
