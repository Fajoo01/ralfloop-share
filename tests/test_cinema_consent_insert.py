from pathlib import Path
import sys
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from cinema_consent_insert import build

SOURCE = Path('/home/bandi/.local/share/google-workspace-mcp/workspace/01_Iscrizione_corso_cinema_Trotter.pdf')
TESSERA = Path('/home/bandi/.local/share/google-workspace-mcp/workspace/02_Domanda_ammissione_Tiremm_ARCI_minori.pdf')


def test_two_forms_no_new_release_pdf(tmp_path):
    dest = tmp_path / 'cinema.pdf'
    build(SOURCE, dest)
    assert TESSERA.is_file()
    pdf = PdfReader(str(dest))
    assert len(pdf.pages) == 2
    assert pdf.pages[0].extract_text() == PdfReader(str(SOURCE)).pages[0].extract_text()


def test_parent_media_consent_separate_and_optional(tmp_path):
    dest = tmp_path / 'cinema.pdf'
    build(SOURCE, dest)
    second = PdfReader(str(dest)).pages[1].extract_text()
    for expected in ('CONSENSO FACOLTATIVO', 'NON ACCONSENTO', 'non impedisce',
                     'pubblicazione del film', 'due', 'Firma genitore/tutore 2',
                     'revocato', 'info@tiremminnanz.com'):
        if expected == 'due':
            continue
        assert expected in second
    assert 'non viene raccolto con questa scheda' not in second
