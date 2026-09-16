from integrations.bottazzi_presence.presence_logic import trusted_face_event, correlate_passage_face

def test_multiframe_recognized_non_gate_identity_is_still_valid_for_registry():
    f={"event_id":"c1","verdict":"maria","consensus":{"verdict":"maria","reason":"identity_not_allowed"},"summary":[{"name":"maria","best_distance":.31}]}
    assert trusted_face_event(f)

def test_legacy_face_over_distance_is_rejected():
    f={"event_id":"c1","verdict":"fabio","summary":[{"name":"fabio","best_distance":.4065}]}
    assert not trusted_face_event(f)

def test_identity_attaches_only_to_same_passage():
    p={"track_id":"p1","citofono_event_id":"c1","garden_event_id":"g1","direction_hint":"entrata_probabile","presence_state":"inside","garden_ts":"2026-09-16T12:00:20"}
    f={"event_id":"c1","verdict":"fabio","consensus":{"verdict":"fabio","reason":"multiframe_consensus"},"summary":[{"name":"fabio","best_distance":.25}]}
    r=correlate_passage_face(p,f)
    assert r["track_id"]=="p1" and r["name"]=="fabio" and r["presence_state"]=="inside"
    f["event_id"]="c2"
    assert correlate_passage_face(p,f) is None
