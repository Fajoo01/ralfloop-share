import json
from pathlib import Path

from integrations.bottazzi_presence.site_activity_sessionizer import activity_row, finalize_session, parse_ts, run


def _event(eid, etype, source, site, ts, **payload):
    return {"event_id":eid,"event_type":etype,"source_kind":source,"site":site,"occurred_at":ts,"payload":{"site":site,**payload}}


def test_activity_row_never_crosses_unassigned_or_health_events():
    assert activity_row(_event("x","HUMAN_PASSAGE","garden","unassigned","2026-09-17T10:00:00+00:00")) is None
    assert activity_row(_event("x","DOMOTICS_ENTITY_UNAVAILABLE","domotics","sede","2026-09-17T10:00:00+00:00")) is None
    assert activity_row(_event("x","HUMAN_PASSAGE","garden","sede","2026-09-17T10:00:00+00:00"))["site"] == "sede"


def test_finalize_marks_cross_source_correlation():
    s={"site":"sede","started_at":"a","last_at":"b","events":[
        {"event_id":"g1","event_type":"HUMAN_PASSAGE","source_kind":"garden"},
        {"event_id":"h1","event_type":"HA_STATE_CHANGED","source_kind":"ha_sede","presence_state":"inside"},
    ]}
    out=finalize_session(s)
    assert out["correlation"] == "multi_source"
    assert out["site"] == "sede"
    assert out["presence_state"] == "inside"


def test_run_baselines_then_groups_same_site_and_separates_other_site(tmp_path: Path):
    src=tmp_path/'activity.jsonl'; state=tmp_path/'state.json'; out=tmp_path/'sessions.jsonl'
    src.write_text(json.dumps(_event('old','HUMAN_PASSAGE','garden','sede','2026-09-17T09:00:00+00:00'))+'\n')
    assert run(src,state,out,now_epoch=0)["status"] == "baseline_initialized"
    with src.open('a') as fh:
        fh.write(json.dumps(_event('g1','HUMAN_PASSAGE','garden','sede','2026-09-17T10:00:00+00:00'))+'\n')
        fh.write(json.dumps(_event('h1','HA_STATE_CHANGED','ha_sede','sede','2026-09-17T10:01:00+00:00',new_state='on'))+'\n')
        fh.write(json.dumps(_event('a1','HA_STATE_CHANGED','ha','asiago','2026-09-17T10:01:30+00:00',new_state='on'))+'\n')
    r=run(src,state,out,now_epoch=parse_ts('2026-09-17T10:02:00+00:00'),window_seconds=180)
    assert r['processed'] == 3
    # force quiet close later
    r2=run(src,state,out,now_epoch=parse_ts('2026-09-17T10:10:00+00:00'),window_seconds=180)
    assert r2['finalized'] == 2
    rows=[json.loads(x) for x in out.read_text().splitlines()]
    sede=next(x for x in rows if x['site']=='sede')
    asiago=next(x for x in rows if x['site']=='asiago')
    assert sede['event_count']==2 and sede['correlation']=='multi_source'
    assert asiago['event_count']==1 and asiago['correlation']=='single_source'


def test_auxiliary_gate_relay_cannot_start_session(tmp_path: Path):
    src=tmp_path/'activity.jsonl'; state=tmp_path/'state.json'; out=tmp_path/'sessions.jsonl'
    src.write_text('')
    assert run(src,state,out,now_epoch=0)["status"] == "baseline_initialized"
    gate=_event('r1','HA_STATE_CHANGED','ha_sede','sede','2026-09-17T10:00:00+00:00',entity_id='switch.cancello_switch_1',old_state='off',new_state='on',signal_role='auxiliary_relay')
    with src.open('a') as fh: fh.write(json.dumps(gate)+'\n')
    r=run(src,state,out,now_epoch=parse_ts('2026-09-17T10:01:00+00:00'),window_seconds=180)
    assert r['processed'] == 1
    assert r['open_sites'] == 0
    assert not out.exists()


def test_auxiliary_gate_relay_only_enriches_existing_session(tmp_path: Path):
    src=tmp_path/'activity.jsonl'; state=tmp_path/'state.json'; out=tmp_path/'sessions.jsonl'
    src.write_text('')
    run(src,state,out,now_epoch=0)
    primary=_event('g1','HUMAN_PASSAGE','garden','sede','2026-09-17T10:00:00+00:00')
    gate=_event('r1','HA_STATE_CHANGED','ha_sede','sede','2026-09-17T10:00:30+00:00',entity_id='switch.cancello_switch_1',old_state='off',new_state='on',signal_role='auxiliary_relay')
    with src.open('a') as fh:
        fh.write(json.dumps(primary)+'\n'); fh.write(json.dumps(gate)+'\n')
    r=run(src,state,out,now_epoch=parse_ts('2026-09-17T10:01:00+00:00'),window_seconds=180)
    assert r['open_sites'] == 1
    r2=run(src,state,out,now_epoch=parse_ts('2026-09-17T10:10:00+00:00'),window_seconds=180)
    assert r2['finalized'] == 1
    row=json.loads(out.read_text().splitlines()[0])
    assert row['event_count'] == 2
