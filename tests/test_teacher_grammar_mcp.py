from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import sqlite3
import subprocess
import unicodedata

import pytest

from ralfloop_agent.teacher.grammar_client import GrammarEvidenceClient, _canonical_key
from src.mcp_transport import MCPClientSession, StdioMCPTransport


ROOT = Path(__file__).resolve().parents[1]
GRAMMAR_DIR = ROOT / "tools" / "teacher_grammar_mcp"
BINARY = GRAMMAR_DIR / "ralf-teacher-grammar-mcp"


def _builder_module():
    path = GRAMMAR_DIR / "rebuild_grammar_db.py"
    spec = importlib.util.spec_from_file_location("teacher_grammar_builder", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tiny_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE lexemes(id INTEGER PRIMARY KEY,token TEXT,normalized TEXT,categoria TEXT,status TEXT,source TEXT);
    CREATE TABLE features(lexeme_id INTEGER,key TEXT,value TEXT);
    CREATE TABLE valency_frames(id INTEGER PRIMARY KEY,lemma TEXT,sense TEXT,valency TEXT,predicate_type TEXT,notes TEXT,source TEXT,status TEXT);
    CREATE TABLE valency_roles(frame_id INTEGER,role_order INTEGER,role_name TEXT,traditional_label TEXT,surface_pattern TEXT,required INTEGER);
    CREATE TABLE tpas_patterns(id INTEGER PRIMARY KEY,lemma TEXT,label TEXT,pattern_string TEXT,sense TEXT,freq INTEGER,ratio TEXT);
    """)
    rows = [
        (1,"e","e","congiunzione","approved","bulk_morphit"),
        (2,"è","è","verbo","approved","bulk_morphit"),
        (3,"dà","dà","verbo","approved","bulk_morphit"),
        (4,"sì","sì","avverbio","approved","bulk_morphit"),
        (5,"né","né","congiunzione","approved","bulk_morphit"),
        (6,"perché","perché","congiunzione","approved","bulk_morphit"),
        (7,"città","città","nome_comune","approved","bulk_morphit"),
        (8,"po'","po'","pronome","approved","bulk_morphit"),
        (9,"andare","andare","verbo","approved","bulk_morphit"),
        (10,"andato","andato","verbo","approved","bulk_morphit"),
    ]
    db.executemany("INSERT INTO lexemes VALUES(?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO features VALUES(?,?,?)", [
        (2,"lemma","essere"),(3,"lemma","dare"),
        (9,"lemma","andare"),(9,"modo","infinito"),(9,"tempo","presente"),
        (10,"lemma","andare"),(10,"modo","participio"),(10,"tempo","passato"),
        (10,"genere","maschile"),(10,"numero","singolare"),
    ])
    db.commit(); db.close()


def test_builder_reads_morphit_as_latin1_and_preserves_accents(tmp_path: Path) -> None:
    module = _builder_module()
    source = tmp_path / "morphit.txt"
    source.write_bytes("perché\tperché\tWH\ncittà\tcittà\tNOUN-F:s\n".encode("latin-1"))
    assert list(module.source_rows(source)) == [
        ("perché", "perché", "WH"),
        ("città", "città", "NOUN-F:s"),
    ]
    assert module.canonical_key("PERCHÉ") == "perché"
    assert module.canonical_key(unicodedata.normalize("NFD", "CITTÀ")) == "città"


def test_client_normalization_preserves_contrastive_accents_and_splits_elision() -> None:
    assert _canonical_key("È") == "è"
    assert _canonical_key("E") == "e"
    assert _canonical_key("po’") == "po'"
    assert _canonical_key("DÀ") != _canonical_key("DA")
    assert GrammarEvidenceClient._tokens("L’acqua sì, perché è più fredda.", 16)[:6] == [
        "l'", "acqua", "sì", "perché", "è", "più"
    ]


def test_c_grammar_mcp_handles_unicode_and_never_collapses_accents(tmp_path: Path) -> None:
    if shutil.which("cc") is None or not Path("/lib/x86_64-linux-gnu/libsqlite3.so.0").exists():
        pytest.skip("native sqlite runtime unavailable")
    subprocess.run(["make", "-C", str(GRAMMAR_DIR), "clean", "all"], check=True, capture_output=True, text=True)
    db = tmp_path / "grammar.sqlite"
    _tiny_db(db)
    argv = [str(BINARY), "--db", str(db), "--stdio"]
    with MCPClientSession(StdioMCPTransport(argv), timeout=3, client_name="grammar-test") as session:
        assert {tool.name for tool in session.list_tools()} == {
            "grammar.lookup_token", "grammar.lookup_lemma", "grammar.lookup_valency", "grammar.health"
        }
        def lookup(token: str) -> dict:
            return session.call_tool("grammar.lookup_token", {"token": token})["structuredContent"]
        forms = session.call_tool(
            "grammar.lookup_lemma",
            {"lemma": "ANDARE", "modo": "PARTICIPIO", "tempo": "PASSATO"},
        )["structuredContent"]
        assert [row["token"] for row in forms["forms"]] == ["andato"]
        assert forms["filters"] == {"modo": "participio", "tempo": "passato"}
        assert lookup("È")["analyses"][0]["category"] == "verbo"
        assert lookup("E")["analyses"][0]["category"] == "congiunzione"
        assert lookup("DÀ")["analyses"][0]["features"]["lemma"] == "dare"
        assert lookup("PERCHÉ")["analyses"]
        assert lookup(unicodedata.normalize("NFD", "CITTÀ"))["analyses"]
        assert lookup("po’")["token"] == "po'"
