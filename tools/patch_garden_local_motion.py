from pathlib import Path
import json

ROOT = Path('/home/bandi/ralfloop-garden-detection-refine')
SUP = ROOT / 'integrations/bottazzi_garden/person_event_supervisor.py'
DET = ROOT / 'integrations/bottazzi_garden/garden_person_detector.py'
CFG = ROOT / 'integrations/bottazzi_garden/event_supervisor_config.json'


def once(s, old, new, name):
    n=s.count(old)
    if n != 1:
        raise RuntimeError(f'{name}: expected 1 match, got {n}')
    return s.replace(old,new,1)

s=DET.read_text()
marker='def filter_day_large_low_motion_false_positive_hits(alert_hits, health=None, img_path=None):\n'
helper='''def local_box_motion_score(prev_frame, frame, box, pad_ratio=0.18):\n    if prev_frame is None or frame is None or prev_frame.shape != frame.shape:\n        return None\n    try:\n        x, y, bw, bh = [int(float(v)) for v in box[:4]]\n        h, w = frame.shape[:2]\n        px=max(8,int(max(1,bw)*pad_ratio)); py=max(8,int(max(1,bh)*pad_ratio))\n        x1=max(0,x-px); y1=max(0,y-py); x2=min(w,x+bw+px); y2=min(h,y+bh+py)\n        if x2 <= x1 or y2 <= y1:\n            return None\n        a=cv2.resize(prev_frame[y1:y2,x1:x2],(96,96))\n        b=cv2.resize(frame[y1:y2,x1:x2],(96,96))\n        diff=cv2.absdiff(a,b)\n        return float(diff.mean()) / 255.0\n    except Exception:\n        return None\n\n\ndef attach_local_motion_scores(hits, prev_frame, frame):\n    for hit in hits or []:\n        score=local_box_motion_score(prev_frame, frame, hit.get("box") or [0,0,0,0])\n        hit["local_motion_score"] = None if score is None else round(score, 4)\n    return hits\n\n\n'''
s=once(s,marker,helper+marker,'local motion helper')
s=once(
    s,
    '''            frame = cv2.imread(str(img_path))\n            health = analyze_frame_health(prev_supervisor_frame, frame, now, prev_supervisor_ts, supervisor_freeze_state, supervisor_cfg)\n            alert_hits = filter_night_dynamic_cluster_memory_hits(alert_hits, health, img_path)\n''',
    '''            frame = cv2.imread(str(img_path))\n            health = analyze_frame_health(prev_supervisor_frame, frame, now, prev_supervisor_ts, supervisor_freeze_state, supervisor_cfg)\n            alert_hits = attach_local_motion_scores(alert_hits, prev_supervisor_frame, frame)\n            alert_hits = filter_night_dynamic_cluster_memory_hits(alert_hits, health, img_path)\n''',
    'attach local motion',
)
s=once(
    s,
    '                    dets.append({"confidence": float(h.get("confidence") or 0), "box": [x, y, x + bw, y + bh], "detector": str(h.get("detector") or "unknown")})\n',
    '                    dets.append({"confidence": float(h.get("confidence") or 0), "box": [x, y, x + bw, y + bh], "detector": str(h.get("detector") or "unknown"), "local_motion_score": h.get("local_motion_score")})\n',
    'pass local motion',
)
DET.write_text(s)

s=SUP.read_text()
s=once(
    s,
    '            persons.append({"confidence":conf,"bbox":box,"area_ratio":area,"center":center,"class":"person","detector":str(d.get("detector","unknown"))})\n',
    '            persons.append({"confidence":conf,"bbox":box,"area_ratio":area,"center":center,"class":"person","detector":str(d.get("detector","unknown")),"local_motion_score":d.get("local_motion_score")})\n',
    'supervisor local motion input',
)
s=once(
    s,
    '        d=max(valid,key=lambda x:x.get("confidence",0)); tid=d.get("track_id"); hits=int(d.get("track_hits",1)); dur=float(d.get("track_duration_ms",0)); conf=float(d.get("confidence",0)); conf_median=float(d.get("conf_median",0.0)); conf_max=float(d.get("conf_max",conf))\n',
    '        d=max(valid,key=lambda x:x.get("confidence",0)); tid=d.get("track_id"); hits=int(d.get("track_hits",1)); dur=float(d.get("track_duration_ms",0)); conf=float(d.get("confidence",0)); conf_median=float(d.get("conf_median",0.0)); conf_max=float(d.get("conf_max",conf)); local_motion_raw=d.get("local_motion_score"); local_motion=health.motion_score if local_motion_raw is None else float(local_motion_raw); track_speed=float(d.get("speed",0.0) or 0.0)\n',
    'decision local motion vars',
)
s=once(
    s,
    '        single_review_motion=float(fast.get("single_frame_vision_motion_score",0.04))\n',
    '        single_review_motion=float(fast.get("single_frame_vision_motion_score",0.025))\n        min_local_motion=float(fast.get("min_local_motion_score",0.012))\n',
    'local motion thresholds',
)
s=once(
    s,
    '            if hits == 1 and conf >= single_review_conf and health.motion_score >= single_review_motion:\n',
    '            if hits == 1 and conf >= single_review_conf and local_motion >= single_review_motion:\n',
    'single review local motion',
)
s=once(
    s,
    '                self._log("PERSON_ALERT_PENDING", {"reason":"single_frame_requires_vision","track_id":tid,"conf":conf,"conf_median":round(conf_median,4),"conf_max":round(conf_max,4),"hits":hits,"motion_score":round(health.motion_score,4)})\n',
    '                self._log("PERSON_ALERT_PENDING", {"reason":"single_frame_requires_vision","track_id":tid,"conf":conf,"conf_median":round(conf_median,4),"conf_max":round(conf_max,4),"hits":hits,"motion_score":round(health.motion_score,4),"local_motion_score":round(local_motion,4)})\n',
    'single review log local',
)
s=once(
    s,
    '        if fast.get("enabled",True) and conf>=float(fast.get("min_confidence",0.50)) and hits>=int(fast.get("min_hits",1)) and (high_conf_lag_allowed or not fast.get("require_motion",True) or health.motion_score>=float(fast.get("min_motion_score",0.03))):\n',
    '        if fast.get("enabled",True) and conf>=float(fast.get("min_confidence",0.50)) and hits>=int(fast.get("min_hits",2)) and (high_conf_lag_allowed or not fast.get("require_motion",True) or (health.motion_score>=float(fast.get("min_motion_score",0.03)) and local_motion>=min_local_motion)):\n',
    'fast local motion gate',
)
s=once(
    s,
    '            self._log("PERSON_ALERT_SENT", {"reason":"fast_person_detected","detector":detector,"track":tid,"conf":conf,"conf_median":round(conf_median,4),"conf_max":round(conf_max,4),"hits":hits,"speed":"high" if d.get("speed",0)>25 else d.get("speed",0),"motion_score":round(health.motion_score,4)})\n',
    '            self._log("PERSON_ALERT_SENT", {"reason":"fast_person_detected","detector":detector,"track":tid,"conf":conf,"conf_median":round(conf_median,4),"conf_max":round(conf_max,4),"hits":hits,"speed":"high" if d.get("speed",0)>25 else d.get("speed",0),"motion_score":round(health.motion_score,4),"local_motion_score":round(local_motion,4)})\n',
    'fast log local motion',
)
s=once(
    s,
    '        if slow.get("enabled",True) and conf>=float(slow.get("min_confidence",0.50)) and hits>=int(slow.get("min_hits",3)) and dur>=float(slow.get("min_duration_ms",800)):\n',
    '        slow_local_min=float(slow.get("min_local_motion_score",0.006)); slow_speed_min=float(slow.get("min_track_speed",1.0))\n        if slow.get("enabled",True) and conf>=float(slow.get("min_confidence",0.50)) and hits>=int(slow.get("min_hits",3)) and dur>=float(slow.get("min_duration_ms",800)) and (local_motion>=slow_local_min or track_speed>=slow_speed_min):\n',
    'slow local movement gate',
)
SUP.write_text(s)

cfg=json.loads(CFG.read_text())
fast=cfg['event_supervisor']['person_fast']
fast['min_local_motion_score']=0.012
fast['single_frame_vision_motion_score']=0.025
slow=cfg['event_supervisor']['person_slow']
slow['min_local_motion_score']=0.006
slow['min_track_speed']=1.0
CFG.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n')
print('LOCAL_MOTION_PATCH_OK')
