from __future__ import annotations

import importlib.util
from pathlib import Path
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "refresh_atm_router_graph.py"
SPEC = importlib.util.spec_from_file_location("refresh_atm_router_graph", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)


def _write_gtfs(path: Path, *, start: str = "20260914", end: str = "20261002", version: str = "423") -> Path:
    feed_info = (
        '"feed_publisher_name","feed_publisher_url","feed_lang","feed_start_date","feed_version","surface_end_date","mm_end_date"\n'
        f'"AMAT_Milan_feed","https://example.invalid","it","{start}","{version}","{end}","20261015"\n'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("feed_info.txt", feed_info)
        for name in ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt"):
            zf.writestr(name, "id\n")
    return path


def test_validate_feed_window_accepts_current_surface_feed(tmp_path: Path) -> None:
    gtfs = _write_gtfs(tmp_path / "gtfs.zip")
    info = refresh._validate_feed_window(gtfs, "2026-09-21")
    assert info["feed_version"] == "423"
    assert info["surface_end_date"] == "20261002"


def test_validate_feed_window_rejects_expired_surface_feed(tmp_path: Path) -> None:
    gtfs = _write_gtfs(tmp_path / "gtfs.zip", end="20260913", version="421")
    with pytest.raises(ValueError, match="GTFS superficie scaduto"):
        refresh._validate_feed_window(gtfs, "2026-09-21")


def test_validate_feed_window_rejects_future_feed(tmp_path: Path) -> None:
    gtfs = _write_gtfs(tmp_path / "gtfs.zip", start="20260922")
    with pytest.raises(ValueError, match="GTFS non ancora valido"):
        refresh._validate_feed_window(gtfs, "2026-09-21")


def test_read_feed_info_rejects_incomplete_zip(tmp_path: Path) -> None:
    gtfs = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(gtfs, "w") as zf:
        zf.writestr("feed_info.txt", "feed_start_date,surface_end_date\n20260914,20261002\n")
    with pytest.raises(ValueError, match="GTFS incompleto"):
        refresh._read_feed_info(gtfs)


def test_service_day_accepts_iso_and_compact_dates() -> None:
    assert refresh._service_day("2026-09-21") == 20260921
    assert refresh._service_day("20260921") == 20260921
