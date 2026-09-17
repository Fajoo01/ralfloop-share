from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from scripts import refresh_atm_router_graph as refresh


def _write_feed(path: Path, *, surface_end_date: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("routes.txt", "route_id,route_short_name,route_type\nR,44,3\n")
        zf.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\nS,Stop,45,9\n")
        zf.writestr("trips.txt", "route_id,service_id,trip_id\nR,W,T\n")
        zf.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT,10:00:00,10:00:00,S,1\n")
        zf.writestr(
            "feed_info.txt",
            "feed_publisher_name,feed_version,surface_end_date\nAMAT,421," + surface_end_date + "\n",
        )


def test_surface_schedule_fresh_uses_surface_end_date():
    assert refresh.surface_schedule_fresh({"surface_end_date": "20260917"}, "2026-09-17")
    assert not refresh.surface_schedule_fresh({"surface_end_date": "20260913"}, "2026-09-17")
    assert not refresh.surface_schedule_fresh({}, "2026-09-17")


def test_gtfs_metadata_reads_feed_info_and_validates_required_files(tmp_path):
    path = tmp_path / "gtfs.zip"
    _write_feed(path, surface_end_date="20260913")
    metadata = refresh.gtfs_metadata(path)
    assert metadata["feed_version"] == "421"
    assert metadata["surface_end_date"] == "20260913"


def test_refresh_gtfs_atomically_replaces_destination(monkeypatch, tmp_path):
    source = tmp_path / "source.zip"
    destination = tmp_path / "current.zip"
    _write_feed(source, surface_end_date="20261005")
    destination.write_bytes(b"old")

    class Response:
        def __init__(self, data: bytes):
            self._stream = io.BytesIO(data)
        def read(self, size: int = -1) -> bytes:
            return self._stream.read(size)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        refresh.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(source.read_bytes()),
    )
    metadata = refresh.refresh_gtfs("https://example.test/gtfs.zip", destination)
    assert metadata["surface_end_date"] == "20261005"
    assert refresh.gtfs_metadata(destination)["feed_version"] == "421"
