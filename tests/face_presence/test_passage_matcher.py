from integrations.bottazzi_presence.passage_matcher import one_to_one_pairs

def ev(eid,ts): return {"event_id":eid,"ts":ts}

def test_garden_event_cannot_be_reused():
    c=[ev("c1","2026-09-16T12:00:00"),ev("c2","2026-09-16T12:00:20")]
    g=[ev("g1","2026-09-16T12:00:15") ]
    out=one_to_one_pairs(c,g,180)
    assert len(out)==1 and out[0]["citofono"]["event_id"]=="c2"

def test_global_candidates_pair_each_side_once():
    c=[ev("c1","2026-09-16T12:00:00"),ev("c2","2026-09-16T12:02:00")]
    g=[ev("g1","2026-09-16T12:00:20"),ev("g2","2026-09-16T12:02:10")]
    out=one_to_one_pairs(c,g,180)
    assert {(x["citofono"]["event_id"],x["garden"]["event_id"]) for x in out}=={("c1","g1"),("c2","g2")}

def test_claimed_garden_is_excluded():
    c=[ev("c1","2026-09-16T12:00:00")]; g=[ev("g1","2026-09-16T12:00:10")]
    assert one_to_one_pairs(c,g,180,{"g1"})==[]
