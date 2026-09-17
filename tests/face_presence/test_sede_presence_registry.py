import json
from pathlib import Path

from integrations.bottazzi_presence.sede_presence_registry import apply_session, bootstrap_people, run


def session(sid='s1', claims=None, direction=None, presence=None, confidence=None, site='sede'):
    return {
        'event_id':sid,'event':'SITE_ACTIVITY_SESSION','site':site,'ended_at':'2026-09-17T10:00:00+00:00',
        'direction_hint':direction,'presence_state':presence,'movement_confidence':confidence,
        'subject_claims': claims or [],
    }


def claim(name='fabio', state='inside', certainty='confirmed', source='access_ui'):
    return {'kind':'known','subject':name,'state':state,'certainty':certainty,'occurred_at':'2026-09-17T10:00:00+00:00','event_type':'PRESENCE_CONFIRMED','event_source':source,'event_id':'e1','direction_hint':'rientro_confermato_ui'}


def test_bootstrap_legacy_never_treats_old_state_as_current(tmp_path: Path):
    legacy=tmp_path/'legacy.json'; legacy.write_text(json.dumps({'people':{'Fabio':{'state':'inside','last_seen':'2026-05-22'}}}))
    people=bootstrap_people(legacy)
    assert people['fabio']['state']=='unknown'
    assert people['fabio']['certainty']=='stale'
    assert people['fabio']['legacy_last_state']=='inside'


def test_confirmed_claim_updates_known_person_once():
    reg={'people':{},'seen_session_ids':[], 'anonymous':{'direction_balance':0,'unresolved_count':0,'movements':[]}}
    changes=apply_session(reg,session(claims=[claim()]))
    assert reg['people']['fabio']['state']=='inside'
    assert reg['people']['fabio']['certainty']=='confirmed'
    assert len(changes)==1
    assert apply_session(reg,session(claims=[claim()]))==[]


def test_probable_named_claim_does_not_become_confirmed():
    reg={'people':{},'seen_session_ids':[], 'anonymous':{'direction_balance':0,'unresolved_count':0,'movements':[]}}
    c=claim(state='outside',certainty='probable',source='presence_correlator'); c['event_type']='PRESENCE_INFERRED'
    apply_session(reg,session(claims=[c]))
    assert reg['people']['fabio']['state']=='outside'
    assert reg['people']['fabio']['certainty']=='probable'


def test_anonymous_medium_direction_is_ledger_not_person():
    reg={'people':{},'seen_session_ids':[], 'anonymous':{'direction_balance':0,'unresolved_count':0,'movements':[]}}
    apply_session(reg,session(direction='uscita_probabile',presence='outside',confidence='medium'))
    assert reg['anonymous']['direction_balance']==-1
    assert reg['anonymous']['unresolved_count']==0
    assert reg['people']=={}


def test_unpaired_anonymous_is_uncertain():
    reg={'people':{},'seen_session_ids':[], 'anonymous':{'direction_balance':0,'unresolved_count':0,'movements':[]}}
    apply_session(reg,session(direction='unknown_direction',presence='unknown',confidence='low'))
    assert reg['anonymous']['direction_balance']==0
    assert reg['anonymous']['unresolved_count']==1


def test_other_sites_are_ignored():
    reg={'people':{},'seen_session_ids':[], 'anonymous':{'direction_balance':0,'unresolved_count':0,'movements':[]}}
    assert apply_session(reg,session(site='asiago',claims=[claim()]))==[]
    assert reg['people']=={}


def test_first_run_baselines_without_replaying_sessions(tmp_path: Path):
    src=tmp_path/'sessions.jsonl'; state=tmp_path/'state.json'; events=tmp_path/'events.jsonl'; legacy=tmp_path/'legacy.json'
    src.write_text(json.dumps(session('old',claims=[claim()]))+'\n'); legacy.write_text(json.dumps({'people':{'fabio':{'state':'inside'}}}))
    r=run(src,state,events,legacy)
    assert r['status']=='baseline_initialized'
    assert json.load(open(state))['people']['fabio']['state']=='unknown'
    assert not events.exists()


def test_identity_hint_is_recorded_but_never_changes_presence_state():
    reg={'people':{'fabio':{'state':'unknown','certainty':'stale'}},'seen_session_ids':[],'identity_observations':[]}
    s=session('s-face', claims=[])
    s['identity_hints']=[{'subject':'fabio','occurred_at':'2026-09-17T10:00:00+00:00','reason':'multiframe_consensus_with_insightface','support_frames':3,'frame_ratio':0.6}]
    changes=apply_session(reg,s)
    assert changes==[]
    assert reg['people']['fabio']['state']=='unknown'
    assert reg['people']['fabio']['certainty']=='stale'
    assert reg['identity_observations'][-1]['subject']=='fabio'
    assert reg['identity_observations'][-1]['role']=='identity_hint_only'


def test_identity_passage_link_updates_probable_and_avoids_anonymous_count():
    reg={'people':{'fabio':{'state':'unknown','certainty':'stale'}},'seen_session_ids':[],'identity_observations':[],'anonymous':{'direction_balance':0,'unresolved_count':0,'movements':[]}}
    s=session('s-link', claims=[])
    s.update({'ended_at':'2026-09-17T10:01:00+00:00','direction_hint':'entrata_probabile','presence_state':'inside','movement_confidence':'high'})
    s['identity_passage_links']=[{'subject':'fabio','presence_state':'inside','movement_confidence':'high','direction_hint':'entrata_probabile','citofono_event_id':'cit1','track_id':'trk1'}]
    changes=apply_session(reg,s)
    assert reg['people']['fabio']['state']=='inside'
    assert reg['people']['fabio']['certainty']=='probable'
    assert reg['people']['fabio']['evidence_kind']=='identity_passage_link'
    assert reg['anonymous']['direction_balance']==0
    assert reg['anonymous']['unresolved_count']==0
    assert changes[-1]['certainty']=='probable'


def test_presence_claim_has_precedence_over_identity_passage_link():
    reg={'people':{'fabio':{'state':'unknown','certainty':'stale'}},'seen_session_ids':[],'identity_observations':[],'anonymous':{'direction_balance':0,'unresolved_count':0,'movements':[]}}
    s=session('s-both', claims=[claim(name='fabio',state='outside',certainty='confirmed')])
    s['identity_passage_links']=[{'subject':'fabio','presence_state':'inside','movement_confidence':'high','direction_hint':'entrata_probabile','citofono_event_id':'cit1','track_id':'trk1'}]
    apply_session(reg,s)
    assert reg['people']['fabio']['state']=='outside'
    assert reg['people']['fabio']['certainty']=='confirmed'


def test_low_confidence_link_does_not_assign_person_and_stays_anonymous():
    reg={'people':{'fabio':{'state':'unknown','certainty':'stale'}},'seen_session_ids':[],'identity_observations':[],'anonymous':{'direction_balance':0,'unresolved_count':0,'movements':[]}}
    s=session('s-low', claims=[])
    s.update({'ended_at':'2026-09-17T10:01:00+00:00','direction_hint':'uscita_probabile','presence_state':'outside','movement_confidence':'low'})
    s['identity_passage_links']=[{'subject':'fabio','presence_state':'outside','movement_confidence':'low','direction_hint':'uscita_probabile','citofono_event_id':'cit1','track_id':'trk1'}]
    apply_session(reg,s)
    assert reg['people']['fabio']['state']=='unknown'
    assert reg['anonymous']['unresolved_count']==1
