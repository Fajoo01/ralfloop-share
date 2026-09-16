from integrations.bottazzi_citofono.face_consensus import summarize_face_burst

def h(frame,name,distance=None,score=None,margin=None,engine=None):
    d={"frame":frame,"match":name}
    if distance is not None: d["distance"]=distance
    if score is not None: d["score"]=score
    if margin is not None: d["margin"]=margin
    if engine is not None: d["engine"]=engine
    return d

def test_single_frame_never_arms():
    r=summarize_face_burst([h("f1","fabio",0.20)],6,{"fabio"})
    assert r["verdict"]=="incerto" and not r["authorized"]

def test_duplicate_hits_same_frame_count_once():
    hits=[h("f1","maria",0.30),h("f1","maria",0.31),h("f1","maria",0.32),h("f2","fabio",0.25),h("f3","fabio",score=.575,margin=.53,engine="insightface")]
    r=summarize_face_burst(hits,6,{"fabio"})
    assert r["verdict"]=="fabio" and r["authorized"]
    maria=next(x for x in r["summary"] if x["name"]=="maria")
    assert maria["unique_frames"]==1

def test_historical_false_distance_does_not_support():
    r=summarize_face_burst([h("f1","fabio",0.4065),h("f2","fabio",0.41)],6,{"fabio"})
    assert r["verdict"]=="incerto" and not r["authorized"]

def test_known_but_not_allowed_is_not_authorized():
    r=summarize_face_burst([h("f1","maria",0.25),h("f2","maria",0.26)],4,{"fabio"})
    assert r["verdict"]=="maria" and r["reason"]=="identity_not_allowed" and not r["authorized"]

def test_insightface_requires_margin():
    weak=[h("f1","fabio",score=.63,margin=.02,engine="insightface"),h("f2","fabio",score=.64,margin=.03,engine="insightface")]
    assert not summarize_face_burst(weak,4,{"fabio"})["authorized"]
    strong=[h("f1","fabio",score=.63,margin=.25,engine="insightface"),h("f2","fabio",score=.64,margin=.24,engine="insightface")]
    assert summarize_face_burst(strong,4,{"fabio"})["authorized"]


def test_dlib_only_can_identify_but_never_authorize_gate():
    r=summarize_face_burst([h("f1","fabio",0.20),h("f2","fabio",0.22)],4,{"fabio"})
    assert r["verdict"]=="fabio"
    assert r["reason"]=="authorization_requires_insightface"
    assert not r["authorized"]


def test_realistic_citofono_insight_score_can_anchor_dlib_track():
    hits=[h("f1","fabio",0.25),h("f2","fabio",score=.575,margin=.53,engine="insightface")]
    r=summarize_face_burst(hits,4,{"fabio"})
    assert r["authorized"]
    assert r["summary"][0]["insight_support_frames"]==1
