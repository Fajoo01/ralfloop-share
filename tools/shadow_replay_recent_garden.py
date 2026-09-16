import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path

import cv2

ROOT=Path('/home/bandi/ralfloop-garden-detection-refine')
SRC=ROOT/'integrations/bottazzi_garden'
os.environ.setdefault('HA_TOKEN','shadow-no-ha-call')
os.environ['BOTTAZZI_GARDEN_BASE']=str(SRC)
os.environ['BOTTAZZI_GARDEN_YOLO_MANIFEST']='/opt/bottazzi-garden/garden_model_manifest.json'
sys.path.insert(0,str(SRC))

import garden_person_detector as gd
from person_event_supervisor import PersonEventSupervisor, analyze_frame_health, load_config

gd.log=lambda obj: None
cfg=load_config(SRC/'event_supervisor_config.json')
sup=PersonEventSupervisor(cfg, ROOT/'tmp/shadow_person_events.jsonl')
paths=sorted((Path(p) for p in glob.glob('/opt/bottazzi-garden/snapshots/garden_*.jpg')), key=lambda p:p.stat().st_mtime)[-16:]
prev=None
prev_ts=None
freeze={}
counts=Counter()
rows=[]
for i,p in enumerate(paths,1):
    ts=p.stat().st_mtime
    frame=cv2.imread(str(p))
    health=analyze_frame_health(prev,frame,ts,prev_ts,freeze,cfg)
    hits=gd.detect_people(p) or []
    hits=gd.attach_local_motion_scores(hits,prev,frame)
    dets=[]
    for h in hits:
        x,y,bw,bh=h.get('box') or [0,0,0,0]
        dets.append({'confidence':float(h.get('confidence') or 0),'box':[x,y,x+bw,y+bh],'detector':str(h.get('detector') or 'unknown'),'local_motion_score':h.get('local_motion_score')})
    decision=sup.decide(i,ts,dets,health,frame.shape if frame is not None else None)
    counts[decision.reason]+=1
    rows.append({'file':p.name,'hits':len(hits),'top_conf':max([float(h.get('confidence') or 0) for h in hits],default=0),'motion':round(health.motion_score,4),'duplicate':health.duplicated,'frozen':health.frozen,'decision':decision.reason,'send':decision.send_alert,'track_hits':decision.hits,'median':round(decision.confidence_median,4)})
    prev=frame
    prev_ts=ts
print(json.dumps({'frames':len(paths),'decisions':dict(counts),'direct_alerts':sum(1 for r in rows if r['send']),'rows':rows},ensure_ascii=False,indent=2))
