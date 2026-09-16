import copy
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]
MOD_PATH = ROOT / "integrations/bottazzi_garden/person_event_supervisor.py"
CFG_PATH = ROOT / "integrations/bottazzi_garden/event_supervisor_config.json"

spec = importlib.util.spec_from_file_location("garden_supervisor_under_test", MOD_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def cfg():
    return json.loads(CFG_PATH.read_text())


def healthy(motion=0.08):
    return mod.FrameHealth(ok=True, motion_score=motion, quality_score=1.0)


def duplicate():
    return mod.FrameHealth(ok=False, duplicated=True, motion_score=0.0, quality_score=1.0, reason="duplicated")


def det(conf):
    return {"confidence": conf, "box": [100, 50, 160, 200], "detector": "yolo11s_hf"}


def supervisor(tmp_path):
    return mod.PersonEventSupervisor(copy.deepcopy(cfg()), tmp_path / "events.jsonl")


def test_single_medium_candidate_initializes_but_does_not_alert(tmp_path):
    s = supervisor(tmp_path)
    d = s.decide(1, 1.0, [det(0.60)], healthy(), (270, 480, 3))
    assert not d.send_alert
    assert d.reason == "track_initializing"
    assert d.hits == 1


def test_single_strong_candidate_is_sent_to_vision_not_direct_alert(tmp_path):
    s = supervisor(tmp_path)
    d = s.decide(1, 1.0, [det(0.90)], healthy(0.09), (270, 480, 3))
    assert not d.send_alert
    assert d.status == "review"
    assert d.reason == "single_frame_requires_vision"
    assert d.hits == 1


def test_two_healthy_consistent_frames_reach_temporal_consensus(tmp_path):
    s = supervisor(tmp_path)
    d1 = s.decide(1, 1.0, [det(0.70)], healthy(), (270, 480, 3))
    d2 = s.decide(2, 2.0, [det(0.72)], healthy(), (270, 480, 3))
    assert d1.reason == "track_initializing"
    assert d2.send_alert
    assert d2.hits == 2
    assert d2.confidence_median >= 0.69


def test_duplicate_frame_does_not_mature_track(tmp_path):
    s = supervisor(tmp_path)
    d1 = s.decide(1, 1.0, [det(0.70)], healthy(), (270, 480, 3))
    dd = s.decide(2, 2.0, [det(0.70)], duplicate(), (270, 480, 3))
    d3 = s.decide(3, 3.0, [det(0.72)], healthy(), (270, 480, 3))
    assert d1.hits == 1
    assert dd.reason == "duplicated"
    assert dd.hits == 0
    assert d3.hits == 2
    assert d3.send_alert


def test_mixed_scores_do_not_pass_median_consensus(tmp_path):
    s = supervisor(tmp_path)
    d1 = s.decide(1, 1.0, [det(0.40)], healthy(), (270, 480, 3))
    d2 = s.decide(2, 2.0, [det(0.90)], healthy(), (270, 480, 3))
    assert d1.reason == "track_initializing"
    assert not d2.send_alert
    assert d2.reason == "track_score_consensus_low"
    assert d2.confidence_median == 0.40
