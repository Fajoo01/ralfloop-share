#!/usr/bin/env python3
"""Replace only page 2 of the blank cinema enrolment PDF with optional media consent.
Keep page 1 and the separate association-membership form unchanged.
"""
import io
import sys
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from pypdf import PdfReader, PdfWriter

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
pdfmetrics.registerFont(TTFont('TiremmSans', FONT))
pdfmetrics.registerFont(TTFont('TiremmSans-Bold', FONT_BOLD))
DARK = HexColor('#223448')


def build(source: Path, destination: Path):
    reader = PdfReader(str(source))
    assert len(reader.pages) == 2, 'only the verified two-page cinema form is supported'
    assert 'MODULO DI ISCRIZIONE AL CORSO DI' in reader.pages[0].extract_text()
    width, height = float(reader.pages[1].mediabox.width), float(reader.pages[1].mediabox.height)
    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=(width, height))
    c.setFillColor(DARK)
    c.setFont('TiremmSans-Bold', 8)
    c.drawString(48, height-43, 'TIREMM INNANZ APS | ASSOCIAZIONE DI PROMOZIONE SOCIALE')
    c.setLineWidth(0.5)
    c.line(48, height-53, width-48, height-53)
    y = height-80

    def line(text, size=9, bold=False, down=20):
        nonlocal y
        c.setFont('TiremmSans-Bold' if bold else 'TiremmSans', size)
        c.drawString(48, y, text)
        y -= down

    def para(text, down=9, size=8.6):
        nonlocal y
        s = ParagraphStyle('body', fontName='TiremmSans', fontSize=size, leading=size+4, textColor=DARK)
        p = Paragraph(text, s)
        _, ph = p.wrap(width-96, 500)
        p.drawOn(c, 48, y-ph+8)
        y -= ph+down

    line('5. CONFERMA ISCRIZIONE AL CORSO', 11, True, 22)
    para('Confermo i dati indicati nella prima pagina e richiedo l’iscrizione del/della minore al corso di cinematografia al Parco Trotter.', 13)
    line('Luogo e data: _____________________    Firma del genitore/tutore: __________________________', 8.5, False, 29)
    c.line(48, y+8, width-48, y+8)
    y -= 20
    line('6. CONSENSO FACOLTATIVO A IMMAGINI, VIDEO E AUDIO', 11, True, 23)
    para('La scelta è distinta dall’iscrizione al corso e dalla tessera ARCI. Il rifiuto di autorizzare l’uso o la pubblicazione di immagini non impedisce la partecipazione al laboratorio.', 12)
    line('Nome e cognome del/della minore: ________________________________________________', 8.5, False, 27)
    choices = [
        ('1. Riprese e registrazioni per uso interno del laboratorio:', 'Documentazione del percorso e visione limitata ai partecipanti e agli organizzatori, senza pubblicazione online.'),
        ('2. Uso di foto/video nei materiali informativi Tiremm:', 'Pubblicazione selezionata su sito, newsletter, locandine o resoconti dell’associazione.'),
        ('3. Diffusione su canali social e pubblicazione del film:', 'Pubblicazione del film o di estratti con il/la minore su canali pubblici ufficiali Tiremm, inclusi social/video.'),
    ]
    for title, description in choices:
        line(title, 8.5, True, 15)
        para(description, down=7, size=8)
        line('☐ ACCONSENTO              ☐ NON ACCONSENTO', 8.5, False, 26)

    para('Il consenso può essere revocato scrivendo a info@tiremminnanz.com; Tiremm interromperà gli usi futuri e rimuoverà i propri contenuti ove possibile. Le copie già diffuse presso terzi potrebbero non essere integralmente eliminabili. Informativa privacy completa disponibile presso l’associazione.', 12, 8)
    line('Firma genitore/tutore 1: ___________________________   Data: __________________', 8.2, False, 21)
    line('Firma genitore/tutore 2: ___________________________', 8.2, False, 24)
    para('Per la diffusione pubblica occorre il consenso di entrambi gli esercenti la responsabilità genitoriale, oppure la dichiarazione di esercizio esclusivo qui sotto.', 7, 8)
    line('☐ Dichiaro di esercitare da solo/a la responsabilità genitoriale.', 8, False, 16)
    assert y > 62, f'Consent page would overflow: {y}'
    c.setFont('TiremmSans', 7.1)
    c.setFillColor(DARK)
    c.drawCentredString(width/2, 37, 'Tiremm Innanz APS · CF 97826900157 · info@tiremminnanz.com · www.tiremminnanz.com')
    c.save()
    stream.seek(0)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    writer.add_page(PdfReader(stream).pages[0])
    with destination.open('wb') as f:
        writer.write(f)

if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: cinema_consent_insert.py SOURCE.pdf DESTINATION.pdf')
    build(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f'OK revised 2-page blank enrollment with independent optional media consent: {sys.argv[2]}')
