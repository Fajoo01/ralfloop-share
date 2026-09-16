import json
from datetime import datetime
from integrations.bottazzi_presence import passage_tracker as pt

def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(x)+"\n" for x in rows))

def test_pending_event_can_pair_on_later_run(tmp_path, monkeypatch):
    c=tmp_path/"c.jsonl"; g=tmp_path/"g.jsonl"; out=tmp_path/"out.jsonl"; state=tmp_path/"state.json"
    for name,val in [("CITOFONO_EVENTS",c),("GARDEN_EVENTS",g),("OUT_EVENTS",out),("STATE_FILE",state),("BASE",tmp_path)]: monkeypatch.setattr(pt,name,val)
    now=datetime.now().replace(microsecond=0)
    ce={"event":"camera_wake_face_burst","event_id":"c1","ts":now.isoformat(),"frames":[]}
    write_jsonl(c,[ce]); write_jsonl(g,[])
    pt.process(20)
    st=json.loads(state.read_text()); assert "c1" in st["pending_citofono"] and not out.exists()
    ge={"event":"human_passage","event_id":"g1","ts":now.isoformat(),"best_confidence":.9}
    write_jsonl(g,[ge]); pt.process(20)
    rows=[json.loads(x) for x in out.read_text().splitlines()]
    assert len(rows)==1 and rows[0]["event"]=="physical_passage_tracked" and rows[0]["garden_event_id"]=="g1"
    st=json.loads(state.read_text()); assert "c1" not in st["pending_citofono"] and "g1" in st["claimed_garden_events"]
