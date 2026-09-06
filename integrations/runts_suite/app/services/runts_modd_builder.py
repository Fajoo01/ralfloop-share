from collections import OrderedDict, defaultdict
from decimal import Decimal

OFFICIAL_ROWS = [
    ("A1", "Materie prime, sussidiarie, di consumo e di merci", "Entrate da quote associative e apporti dei fondatori"),
    ("A2", "Servizi", "Entrate dagli associati per attivita mutuali"),
    ("A3", "Godimento di beni di terzi", "Entrate per prestazioni e cessioni ad associati e fondatori"),
    ("A4", "Personale", "Erogazioni liberali"),
    ("A5", "Uscite diverse di gestione", "Entrate del 5 per mille"),
    ("A6", "", "Contributi da soggetti privati"),
    ("A7", "", "Entrate per prestazioni e cessioni a terzi"),
    ("A8", "", "Contributi da enti pubblici"),
    ("A9", "", "Entrate da contratti con enti pubblici"),
    ("A10", "", "Altre entrate"),
    ("B1", "Materie prime, sussidiarie, di consumo e di merci", "Entrate per prestazioni e cessioni ad associati e fondatori"),
    ("B2", "Servizi", "Contributi da soggetti privati"),
    ("B3", "Godimento di beni di terzi", "Entrate per prestazioni e cessioni a terzi"),
    ("B4", "Personale", "Contributi da enti pubblici"),
    ("B5", "Uscite diverse di gestione", "Entrate da contratti con enti pubblici"),
    ("B6", "", "Altre entrate"),
    ("C1", "Uscite per raccolte fondi abituali", "Entrate da raccolte fondi abituali"),
    ("C2", "Uscite per raccolte fondi occasionali", "Entrate da raccolte fondi occasionali"),
    ("C3", "Altre uscite", "Altre entrate"),
    ("D1", "Su rapporti bancari", "Da rapporti bancari"),
    ("D2", "Su investimenti finanziari", "Da altri investimenti finanziari"),
    ("D3", "Su patrimonio edilizio", "Da patrimonio edilizio"),
    ("D4", "Su altri beni patrimoniali", "Da altri beni patrimoniali"),
    ("D5", "Altre uscite", "Altre entrate"),
    ("E1", "Materie prime, sussidiarie, di consumo e di merci", "Entrate da distacco del personale"),
    ("E2", "Servizi", "Altre entrate di supporto generale"),
    ("E3", "Godimento di beni di terzi", ""),
    ("E4", "Personale", ""),
    ("E5", "Altre uscite", ""),
]


def blank(uscite_label="", entrate_label=""):
    return {
        "uscite_label": uscite_label,
        "entrate_label": entrate_label,
        "uscite_corrente": 0,
        "uscite_precedente": 0,
        "entrate_corrente": 0,
        "entrate_precedente": 0,
    }


def _normalize_code(code):
    code = str(code or "").strip()
    if len(code) >= 3 and code[0] in {"R", "C"} and code[1] in {"A", "B", "C", "D", "E"} and code[2:].isdigit():
        return f"{code[1]}{code[2:]}"
    return code


CAPITAL_ROWS = [
    ("1", "Investimenti in immobilizzazioni inerenti alle attività di interesse generale", "Disinvestimenti di immobilizzazioni inerenti alle attività di interesse generale"),
    ("2", "Investimenti in immobilizzazioni inerenti alle attività diverse", "Disinvestimenti di immobilizzazioni inerenti alle attività diverse"),
    ("3", "Investimenti in attività finanziarie e patrimoniali", "Disinvestimenti di attività finanziarie e patrimoniali"),
    ("4", "Rimborso di finanziamenti per quota capitale e di prestiti", "Ricevimento di finanziamenti e di prestiti"),
]


def build_modd_matrix(curr_rows, prev_rows, *, capital_rows=(), prev_capital_rows=()):
    """General ministerial layout. Amounts never manufacture cash balances.

    Known legacy A6/A7 expense labels map to their exact official row.
    Unknown code/side combinations fail instead of creating unofficial rows.
    Capital rows must come from separately verified accounting classifications.
    """
    matrix = OrderedDict((code, blank(usc, ent)) for code, usc, ent in OFFICIAL_ROWS)
    corrections = []

    def load(rows, suffix):
        for r in rows or []:
            code = _normalize_code(r.get("voce_codice"))
            side = r.get("side")
            label = r.get("voce_label") or ""
            if side not in {"uscita", "entrata"}:
                raise ValueError("modd_invalid_side")
            if side == "uscita" and code in {"A6", "A7"}:
                exact = [key for key, usc, _ in OFFICIAL_ROWS if key.startswith("A") and usc == label]
                if len(exact) != 1:
                    raise ValueError("modd_legacy_mapping_ambiguous")
                corrections.append({"year_column": suffix, "source_code": code, "target_code": exact[0], "side": side})
                code = exact[0]
            if code not in matrix or not matrix[code]["uscite_label" if side == "uscita" else "entrate_label"]:
                raise ValueError("modd_unofficial_row")
            val = Decimal(str(r.get("totale") or 0))
            if not val.is_finite():
                raise ValueError("modd_invalid_amount")
            if side == "uscita":
                matrix[code]["uscite_" + suffix] += val
                if label and not matrix[code]["uscite_label"]:
                    matrix[code]["uscite_label"] = label
            else:
                matrix[code]["entrate_" + suffix] += val
                if label and not matrix[code]["entrate_label"]:
                    matrix[code]["entrate_label"] = label

    load(curr_rows, "corrente")
    load(prev_rows, "precedente")

    totals = {
        "uscite_corrente": sum(r["uscite_corrente"] for r in matrix.values()),
        "uscite_precedente": sum(r["uscite_precedente"] for r in matrix.values()),
        "entrate_corrente": sum(r["entrate_corrente"] for r in matrix.values()),
        "entrate_precedente": sum(r["entrate_precedente"] for r in matrix.values()),
    }
    totals["saldo_corrente"] = totals["entrate_corrente"] - totals["uscite_corrente"]
    totals["saldo_precedente"] = totals["entrate_precedente"] - totals["uscite_precedente"]

    sections = defaultdict(
        lambda: {
            "uscite_corrente": 0,
            "uscite_precedente": 0,
            "entrate_corrente": 0,
            "entrate_precedente": 0,
        }
    )
    for code, row in matrix.items():
        section = str(code)[0]
        sections[section]["uscite_corrente"] += row["uscite_corrente"]
        sections[section]["uscite_precedente"] += row["uscite_precedente"]
        sections[section]["entrate_corrente"] += row["entrate_corrente"]
        sections[section]["entrate_precedente"] += row["entrate_precedente"]

    capital = OrderedDict((code, blank(usc, ent)) for code, usc, ent in CAPITAL_ROWS)
    for rows, suffix in ((capital_rows, "corrente"), (prev_capital_rows, "precedente")):
        for row in rows:
            code, side = str(row["voce_codice"]), row["side"]
            if code not in capital or side not in {"uscita", "entrata"}:
                raise ValueError("modd_capital_row_invalid")
            amount = Decimal(str(row["totale"]))
            if not amount.is_finite() or amount < 0:
                raise ValueError("modd_capital_amount_invalid")
            capital[code][("uscite_" if side == "uscita" else "entrate_") + suffix] += amount
    return {
        "rows": matrix,
        "totali": totals,
        "sezioni": sections,
        "capital": capital,
        "corrections": corrections,
        # The caller must provide independently reconciled cash/bank balances.
        "cassa_finale": None,
        "banca_finale": None,
        "cassa_finale_precedente": None,
        "banca_finale_precedente": None,
        "figurativi": {"costi": 0, "proventi": 0},
    }
