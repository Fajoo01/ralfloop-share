from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys

import pytest

np = pytest.importorskip("numpy")

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "train_ds4_route_pheromone.py"
SPEC = importlib.util.spec_from_file_location("train_ds4_route_pheromone", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
trainer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trainer
SPEC.loader.exec_module(trainer)


def _trace(path: Path, experts: list[int]) -> Path:
    path.write_text(
        "".join(
            f"{layer},0,0,{expert}\n"
            for layer, expert in enumerate(experts)
        ),
        encoding="utf-8",
    )
    return path


def _matrix(path: Path):
    raw = path.read_bytes()
    assert raw[:8] == trainer.MAGIC
    version, horizon, layers, experts = struct.unpack("<4I", raw[8:24])
    values = np.frombuffer(raw[24:], dtype="<f4").reshape(layers, experts, experts)
    return (version, horizon, layers, experts), values


def test_ds4_pheromone_trainer_writes_btmtrn1_compatible_probabilities(tmp_path: Path) -> None:
    route = _trace(tmp_path / "route.csv", [0, 1, 2])
    output = tmp_path / "predictor.bin"
    result = trainer.train(
        [trainer.FeedbackSpec(route=route, observed_at=100.0)],
        output=output,
        horizon=1,
        layers=3,
        experts=4,
        now=100.0,
    )

    header, matrix = _matrix(output)
    assert header == (1, 1, 2, 4)
    assert np.allclose(matrix.sum(axis=2), 1.0, atol=1e-6)
    assert matrix[0, 0, 1] > matrix[0, 0, 2]
    assert result["policy_boundary"] == "prefetch_prediction_only_model_router_authoritative"


def test_recent_route_wins_after_pheromone_evaporation(tmp_path: Path) -> None:
    old = _trace(tmp_path / "old.csv", [0, 1, 3])
    recent = _trace(tmp_path / "recent.csv", [0, 2, 3])
    output = tmp_path / "predictor.bin"
    trainer.train(
        [
            trainer.FeedbackSpec(route=old, observed_at=0.0),
            trainer.FeedbackSpec(route=recent, observed_at=100.0),
        ],
        output=output,
        horizon=1,
        layers=3,
        experts=4,
        half_life_seconds=10.0,
        now=100.0,
    )

    _, matrix = _matrix(output)
    assert matrix[0, 0, 2] > matrix[0, 0, 1]


def test_failed_prefetch_path_is_penalized_and_alternative_strengthens(tmp_path: Path) -> None:
    repeated = _trace(tmp_path / "repeated.csv", [0, 1, 3])
    alternative = _trace(tmp_path / "alternative.csv", [0, 2, 3])
    output = tmp_path / "predictor.bin"
    trainer.train(
        [
            trainer.FeedbackSpec(route=repeated, outcome="success", observed_at=10.0),
            trainer.FeedbackSpec(route=repeated, outcome="failure", quality=0.0, observed_at=20.0),
            trainer.FeedbackSpec(route=alternative, outcome="success", observed_at=30.0),
        ],
        output=output,
        horizon=1,
        layers=3,
        experts=4,
        half_life_seconds=1000.0,
        now=30.0,
    )

    _, matrix = _matrix(output)
    assert matrix[0, 0, 2] > matrix[0, 0, 1]


def test_manifest_accepts_verified_quality_latency_and_relative_route(tmp_path: Path) -> None:
    route = _trace(tmp_path / "route.csv", [1, 2, 3])
    manifest = tmp_path / "feedback.jsonl"
    manifest.write_text(
        '{"route":"route.csv","outcome":"success","quality":0.8,"latency_ms":1200,"observed_at":50}\n',
        encoding="utf-8",
    )

    specs = trainer._feedback_from_manifest(manifest)
    assert len(specs) == 1
    assert specs[0].route == route.resolve()
    assert specs[0].quality == pytest.approx(0.8)
    assert specs[0].latency_ms == pytest.approx(1200.0)
    assert specs[0].observed_at == pytest.approx(50.0)
