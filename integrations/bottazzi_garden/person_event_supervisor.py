from __future__ import annotations

import json, math, time
from statistics import median
from dataclasses import dataclass, asdict
from pathlib import Path
from collections import deque
from typing import Any

try:
    import cv2
    import numpy as np
except Exception:  # main system must not fail importing config/gui helpers
    cv2 = None
    np = None

DEFAULT_CONFIG = {
  "event_supervisor": {"enabled": True, "target_classes": ["person"], "buffer_seconds": 5,
    "frame_health": {"max_frame_gap_ms": 3000, "duplicate_frame_threshold": 0.98, "freeze_duration_ms": 1000, "severe_lag_blocks_single_frame_person": True, "min_motion_score": 0.01},
    "tracker": {"enabled": True, "type": "auto", "preferred": "bytetrack", "fallback": "iou", "iou_match_threshold": 0.3, "lost_tolerance_frames": 5},
    "person_fast": {"enabled": True, "min_confidence": 0.35, "min_hits": 1, "time_window_ms": 2000, "require_motion": True, "min_motion_score": 0.03, "block_if_severe_lag": True, "block_if_frozen": True},
    "person_slow": {"enabled": True, "min_confidence": 0.50, "min_hits": 3, "min_duration_ms": 800, "require_track": True},
    "person_box_filter": {"min_box_area_ratio": 0.01, "max_box_area_ratio": 0.80},
    "alerts": {"global_cooldown_seconds": 45, "per_track_cooldown_seconds": 90, "per_zone_cooldown_seconds": 60},
    "zones": {"critical": [], "ignore": [], "shadow": [], "lines": []},
    "gui": {"enabled": True, "host": "127.0.0.1", "port": 8501}}
}

@dataclass
class FrameHealth:
    ok: bool = True
    duplicated: bool = False
    frozen: bool = False
    severe_lag: bool = False
    timestamp_gap_ms: float = 0.0
    motion_score: float = 0.0
    quality_score: float = 1.0
    reason: str = ""

@dataclass
class Decision:
    status: str
    reason: str
    send_alert: bool = False
    track_id: int | None = None
    confidence: float = 0.0
    hits: int = 0
    duration_ms: float = 0.0
    motion_score: float = 0.0
    cooldown_remaining_seconds: float = 0.0
    confidence_median: float = 0.0
    confidence_max: float = 0.0

class IouTracker:
    def __init__(self, iou_threshold=0.3, lost_tolerance=5):
        self.iou_threshold=float(iou_threshold); self.lost_tolerance=int(lost_tolerance); self.next_id=1; self.tracks={}
    @staticmethod
    def iou(a,b):
        ax1,ay1,ax2,ay2=a; bx1,by1,bx2,by2=b
        ix1,iy1=max(ax1,bx1),max(ay1,by1); ix2,iy2=min(ax2,bx2),min(ay2,by2)
        inter=max(0,ix2-ix1)*max(0,iy2-iy1)
        aa=max(1,(ax2-ax1)*(ay2-ay1)); ba=max(1,(bx2-bx1)*(by2-by1))
        return inter/(aa+ba-inter) if aa+ba-inter else 0.0
    @staticmethod
    def center(box):
        x1,y1,x2,y2=box; return ((x1+x2)/2.0,(y1+y2)/2.0)
    def update(self, detections, ts):
        assigned=set()
        for tr in self.tracks.values(): tr["matched"]=False
        for det in detections:
            best_id,best_iou=None,0.0
            for tid,tr in self.tracks.items():
                if tid in assigned: continue
                score=self.iou(det["bbox"], tr["bbox"])
                if score>best_iou: best_id,best_iou=tid,score
            if best_id and best_iou>=self.iou_threshold:
                tr=self.tracks[best_id]; old_center=tr["center"]; new_center=self.center(det["bbox"])
                dt=max(0.001, ts-tr["last_seen"]); speed=math.dist(old_center,new_center)/dt
                tr.update({"bbox":det["bbox"],"center":new_center,"last_seen":ts,"hits":tr["hits"]+1,"lost":0,"matched":True,"confidence_sum":tr["confidence_sum"]+det["confidence"],"confidence_max":max(tr["confidence_max"],det["confidence"]),"speed":speed})
                tr.setdefault("confidence_history", []).append(float(det["confidence"]))
                tr["confidence_history"] = tr["confidence_history"][-7:]
                tr["trajectory"].append(new_center); det["track_id"]=best_id; assigned.add(best_id)
            else:
                tid=self.next_id; self.next_id+=1; c=self.center(det["bbox"])
                self.tracks[tid]={"track_id":tid,"first_seen":ts,"last_seen":ts,"hits":1,"lost":0,"bbox":det["bbox"],"center":c,"trajectory":[c],"confidence_sum":det["confidence"],"confidence_max":det["confidence"],"confidence_history":[float(det["confidence"])],"speed":0.0,"matched":True}
                det["track_id"]=tid; assigned.add(tid)
        for tid in list(self.tracks):
            tr=self.tracks[tid]
            if not tr.get("matched"):
                tr["lost"]+=1
                if tr["lost"]>self.lost_tolerance: del self.tracks[tid]
        for det in detections:
            tr=self.tracks.get(det.get("track_id"));
            if tr:
                history=[float(v) for v in tr.get("confidence_history", [])[-7:]]
                padded=([0.0] * max(0, 3-len(history)) + history)
                det["track_hits"]=tr["hits"]; det["track_duration_ms"]=(tr["last_seen"]-tr["first_seen"])*1000.0; det["speed"] = tr.get("speed",0.0); det["conf_avg"]=tr["confidence_sum"]/max(1,tr["hits"]); det["conf_max"]=tr["confidence_max"]; det["conf_history"]=history; det["conf_median"]=float(median(padded)) if padded else 0.0
        return detections

def deep_merge(a,b):
    out=dict(a)
    for k,v in (b or {}).items():
        if isinstance(v,dict) and isinstance(out.get(k),dict): out[k]=deep_merge(out[k],v)
        else: out[k]=v
    return out

def load_config(path):
    p=Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(DEFAULT_CONFIG,indent=2),encoding='utf-8')
    try: data=json.loads(p.read_text(encoding='utf-8'))
    except Exception: data={}
    return deep_merge(DEFAULT_CONFIG,data)

def save_config(path,cfg):
    Path(path).parent.mkdir(parents=True,exist_ok=True); Path(path).write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')

def analyze_frame_health(prev_frame, frame, timestamp, prev_ts=None, freeze_state=None, cfg=None):
    c=(cfg or DEFAULT_CONFIG)["event_supervisor"]["frame_health"]; fs=freeze_state if isinstance(freeze_state,dict) else {"since":None}
    if frame is None: return FrameHealth(False, reason="frame_none")
    h=FrameHealth()
    if prev_ts is not None:
        h.timestamp_gap_ms=(timestamp-prev_ts)*1000.0; h.severe_lag=h.timestamp_gap_ms>float(c.get("max_frame_gap_ms",500))
    if cv2 is None or np is None or prev_frame is None:
        return h
    try:
        if prev_frame.shape != frame.shape:
            return h
        small1=cv2.resize(prev_frame,(96,54)); small2=cv2.resize(frame,(96,54))
        diff=cv2.absdiff(small1,small2); h.motion_score=float(np.mean(diff))/255.0
        gray=cv2.cvtColor(small2,cv2.COLOR_BGR2GRAY); h.quality_score=max(0.0,min(1.0,float(cv2.Laplacian(gray,cv2.CV_64F).var())/120.0))
        same=1.0-h.motion_score
        h.duplicated=same>=float(c.get("duplicate_frame_threshold",0.98))
        now_ms=timestamp*1000.0
        if h.duplicated or h.motion_score<float(c.get("min_motion_score",0.01)):
            fs["since"]=fs.get("since") or now_ms
        else:
            fs["since"]=None
        h.frozen=fs.get("since") is not None and now_ms-fs["since"]>=float(c.get("freeze_duration_ms",1000))
        bad=[]
        if h.duplicated: bad.append('duplicated')
        if h.frozen: bad.append('frozen')
        if h.severe_lag: bad.append('severe_lag')
        if h.quality_score<0.05: bad.append('low_quality')
        h.ok=not bad; h.reason=','.join(bad)
    except Exception as e:
        h.ok=False; h.reason=type(e).__name__
    return h

def norm_box(box):
    if len(box)==4:
        x1,y1,a,b=map(float,box)
        # detect xywh by positive width/height and x+a plausible; fallback xyxy
        if a>0 and b>0 and (a<x1 or b<y1): return [x1,y1,x1+a,y1+b]
        return [x1,y1,a,b]
    return [0,0,0,0]

class PersonEventSupervisor:
    def __init__(self,cfg,log_path):
        self.cfg=cfg; es=cfg["event_supervisor"]; tr=es["tracker"]
        self.buffer=deque(); self.buffer_seconds=float(es.get("buffer_seconds",5)); self.tracker=IouTracker(tr.get("iou_match_threshold",0.3),tr.get("lost_tolerance_frames",5)); self.cooldowns={"global":0,"track":{},"zone":{}}; self.log_path=Path(log_path); self.last_decision=None
    def _log(self, kind, data):
        row=dict(data); row["ts"]=time.strftime('%Y-%m-%dT%H:%M:%S'); row["kind"]=kind
        self.log_path.parent.mkdir(parents=True,exist_ok=True)
        with self.log_path.open('a',encoding='utf-8') as f: f.write(json.dumps(row,ensure_ascii=False)+"\n")
    def _box_ok(self, det):
        bf=self.cfg["event_supervisor"]["person_box_filter"]; ar=det.get("area_ratio",0)
        if ar<float(bf.get("min_box_area_ratio",0.01)): return False,"box_too_small"
        if ar>float(bf.get("max_box_area_ratio",0.80)): return False,"box_too_large"
        x1,y1,x2,y2=det["bbox"]; w=max(1,x2-x1); h=max(1,y2-y1); ratio=h/float(w)
        if ratio<0.45 or ratio>6.0: return False,"box_deformed"
        return True,""
    def decide(self, frame_id, timestamp, detections, health:FrameHealth, frame_shape=None):
        es=self.cfg["event_supervisor"]
        if not es.get("enabled",True): return Decision("pending","supervisor_disabled")
        H,W=(frame_shape[:2] if frame_shape is not None and len(frame_shape)>=2 else (1,1))
        persons=[]
        for d in detections:
            conf=float(d.get("confidence",0)); box=norm_box(d.get("box") or d.get("bbox") or [0,0,0,0]); x1,y1,x2,y2=box
            area=max(0,x2-x1)*max(0,y2-y1)/float(max(1,W*H)); center=((x1+x2)/2.0,(y1+y2)/2.0)
            persons.append({"confidence":conf,"bbox":box,"area_ratio":area,"center":center,"class":"person","detector":str(d.get("detector","unknown")),"local_motion_score":d.get("local_motion_score")})
        # A duplicated/frozen frame is not new evidence. The old code updated the
        # tracker first, so one static false positive could accumulate many "hits".
        # Block before association and do not mature the track on repeated pixels.
        if health.duplicated or health.frozen:
            raw_best=max(persons,key=lambda x:x.get("confidence",0),default={})
            conf=float(raw_best.get("confidence",0.0) or 0.0)
            reason=health.reason or "duplicate_or_frozen_frame"
            self._log("PERSON_ALERT_BLOCKED", {"reason":reason,"conf":conf,"frame_gap_ms":round(health.timestamp_gap_ms,1),"motion_score":round(health.motion_score,4),"evidence_counted":False,"frame_health":asdict(health)})
            dec=Decision("block",reason,False,None,conf,0,0.0,health.motion_score)
            self.last_decision=dec
            return dec
        persons=self.tracker.update(persons,timestamp)
        now=timestamp
        for d in persons:
            d.update({"frame_id":frame_id,"timestamp":timestamp,"health":asdict(health),"motion_score":health.motion_score})
            ok,reason=self._box_ok(d)
            if not ok:
                self._log("PERSON_ALERT_BLOCKED", {"reason":reason,"conf":d["confidence"],"area_ratio":round(d.get("area_ratio",0),4),"track_id":d.get("track_id")})
                continue
        self.buffer.append({"timestamp":timestamp,"frame_id":frame_id,"health":asdict(health),"detections":persons})
        while self.buffer and timestamp-self.buffer[0]["timestamp"]>self.buffer_seconds: self.buffer.popleft()
        valid=[]
        for d in persons:
            ok,reason=self._box_ok(d)
            if ok: valid.append(d)
        if not valid:
            dec=Decision("pending","no_valid_person",motion_score=health.motion_score); self.last_decision=dec; return dec
        d=max(valid,key=lambda x:x.get("confidence",0)); tid=d.get("track_id"); hits=int(d.get("track_hits",1)); dur=float(d.get("track_duration_ms",0)); conf=float(d.get("confidence",0)); conf_median=float(d.get("conf_median",0.0)); conf_max=float(d.get("conf_max",conf)); local_motion_raw=d.get("local_motion_score"); local_motion=health.motion_score if local_motion_raw is None else float(local_motion_raw); track_speed=float(d.get("speed",0.0) or 0.0)
        # HIGH_CONF_LAG_ALLOW_PATCH_20260608
        # Non bloccare una persona vera >= soglia solo per frame gap/lag.
        # Ma NON vale per motion_fallback: il lag genera falsi movimenti.
        detector_for_lag = str(d.get("detector", "unknown"))
        # LAG_REAL_PERSON_ONLY_20260608
        # Durante lag: accetta solo detector reale/persona, mai motion_fallback.
        # Non richiede 3 hit, perché i passaggi veloci possono apparire in un solo frame.
        high_conf_lag_allowed = (
            detector_for_lag != "motion_fallback"
            and conf >= max(
                0.5,
                float(es.get("person_fast", {}).get("min_confidence", 0.5)),
                float(es.get("person_slow", {}).get("min_confidence", 0.5)),
            )
        )
        if health.duplicated or health.frozen or (health.severe_lag and not high_conf_lag_allowed):
            reason="single_frame_after_lag" if hits<=1 else (health.reason or "bad_frame_health")
            self._log("PERSON_ALERT_BLOCKED", {"reason":reason,"conf":conf,"frame_gap_ms":round(health.timestamp_gap_ms,1),"motion_score":round(health.motion_score,4),"track_id":tid,"hits":hits,"frame_health":asdict(health)})
            dec=Decision("block",reason,False,tid,conf,hits,dur,health.motion_score); self.last_decision=dec; return dec
        if health.motion_score < float(es["frame_health"].get("min_motion_score",0.01)) and hits<=1 and not high_conf_lag_allowed:
            self._log("PERSON_ALERT_BLOCKED", {"reason":"single_frame_low_motion","conf":conf,"motion_score":round(health.motion_score,4),"track_id":tid,"hits":hits})
            dec=Decision("block","single_frame_low_motion",False,tid,conf,hits,dur,health.motion_score); self.last_decision=dec; return dec
        # MOTION_FALLBACK_NO_DIRECT_ALERT_20260608
        detector=str(d.get("detector","unknown"))
        motion_fallback_min_hits=int(es.get("person_fast",{}).get("motion_fallback_min_hits",3))
        if detector=="motion_fallback" and hits<motion_fallback_min_hits:
            self._log("PERSON_ALERT_BLOCKED", {"reason":"motion_fallback_needs_confirmation","detector":detector,"conf":conf,"hits":hits,"required_hits":motion_fallback_min_hits,"motion_score":round(health.motion_score,4),"track_id":tid})
            dec=Decision("block","motion_fallback_needs_confirmation",False,tid,conf,hits,dur,health.motion_score); self.last_decision=dec; return dec
        fast=es["person_fast"]; slow=es["person_slow"]
        init_hits=max(2, int(fast.get("min_hits",2)))
        median_threshold=float(fast.get("median_threshold",0.55))
        single_review_conf=float(fast.get("single_frame_vision_confidence",0.82))
        single_review_motion=float(fast.get("single_frame_vision_motion_score",0.025))
        min_local_motion=float(fast.get("min_local_motion_score",0.012))
        if detector != "motion_fallback" and hits < init_hits:
            if hits == 1 and conf >= single_review_conf and local_motion >= single_review_motion:
                self._log("PERSON_ALERT_PENDING", {"reason":"single_frame_requires_vision","track_id":tid,"conf":conf,"conf_median":round(conf_median,4),"conf_max":round(conf_max,4),"hits":hits,"motion_score":round(health.motion_score,4),"local_motion_score":round(local_motion,4)})
                dec=Decision("review","single_frame_requires_vision",False,tid,conf,hits,dur,health.motion_score,0.0,conf_median,conf_max)
                self.last_decision=dec
                return dec
            self._log("PERSON_ALERT_PENDING", {"reason":"track_initializing","track_id":tid,"conf":conf,"conf_median":round(conf_median,4),"hits":hits,"required_hits":init_hits,"motion_score":round(health.motion_score,4)})
            dec=Decision("pending","track_initializing",False,tid,conf,hits,dur,health.motion_score,0.0,conf_median,conf_max)
            self.last_decision=dec
            return dec
        if detector != "motion_fallback" and conf_median < median_threshold:
            self._log("PERSON_ALERT_PENDING", {"reason":"track_score_consensus_low","track_id":tid,"conf":conf,"conf_median":round(conf_median,4),"threshold":median_threshold,"hits":hits,"motion_score":round(health.motion_score,4)})
            dec=Decision("pending","track_score_consensus_low",False,tid,conf,hits,dur,health.motion_score,0.0,conf_median,conf_max)
            self.last_decision=dec
            return dec
        remain=max(0.0, self.cooldowns["global"]-now, self.cooldowns["track"].get(tid,0)-now)
        if remain>0:
            self._log("PERSON_ALERT_SUPPRESSED", {"reason":"cooldown_active","track":tid,"remaining_seconds":round(remain,1),"conf":conf,"hits":hits})
            dec=Decision("cooldown","cooldown_active",False,tid,conf,hits,dur,health.motion_score,remain); self.last_decision=dec; return dec
        # BLOCK_SINGLE_MOTION_FRAME_20260608
        # Non basta che il track abbia accumulato hits: se nel frame corrente
        # c'è un solo motion_fallback, è troppo facile generare falsi positivi.
        if detector=="motion_fallback":
            current_motion_hits = len([x for x in persons if str(x.get("detector",""))=="motion_fallback"])
            required_current_motion_hits = int(es.get("person_fast",{}).get("motion_fallback_min_hits",2))
            if current_motion_hits < required_current_motion_hits:
                self._log("PERSON_ALERT_BLOCKED", {
                    "reason":"motion_fallback_single_frame_blocked",
                    "detector":detector,
                    "conf":conf,
                    "track_hits":hits,
                    "current_motion_hits":current_motion_hits,
                    "required_current_motion_hits":required_current_motion_hits,
                    "motion_score":round(health.motion_score,4),
                    "track_id":tid
                })
                dec=Decision("block","motion_fallback_single_frame_blocked",False,tid,conf,hits,dur,health.motion_score)
                self.last_decision=dec
                return dec

        # BLOCK_CURRENT_ONLY_MOTION_FALLBACK_20260608

        # Se nel frame corrente ci sono solo hit motion_fallback, non mandare alert diretto.

        # Il lag/stallo video può creare falsi movimenti: motion_fallback resta solo candidato.

        current_motion_hits = len([x for x in persons if str(x.get("detector",""))=="motion_fallback"])

        current_total_hits = len(persons)

        current_real_hits = current_total_hits - current_motion_hits

        if current_motion_hits > 0 and current_real_hits <= 0:

            self._log("PERSON_ALERT_BLOCKED", {

                "reason":"current_only_motion_fallback_blocked",

                "conf":conf,

                "track_hits":hits,

                "current_motion_hits":current_motion_hits,

                "current_real_hits":current_real_hits,

                "motion_score":round(health.motion_score,4),

                "track_id":tid

            })

            dec=Decision("block","current_only_motion_fallback_blocked",False,tid,conf,hits,dur,health.motion_score)

            self.last_decision=dec

            return dec


        if fast.get("enabled",True) and conf>=float(fast.get("min_confidence",0.50)) and hits>=int(fast.get("min_hits",2)) and (high_conf_lag_allowed or not fast.get("require_motion",True) or (health.motion_score>=float(fast.get("min_motion_score",0.03)) and local_motion>=min_local_motion)):
            self.cooldowns["global"]=now+float(es["alerts"].get("global_cooldown_seconds",45)); self.cooldowns["track"][tid]=now+float(es["alerts"].get("per_track_cooldown_seconds",90))
            self._log("PERSON_ALERT_SENT", {"reason":"fast_person_detected","detector":detector,"track":tid,"conf":conf,"conf_median":round(conf_median,4),"conf_max":round(conf_max,4),"hits":hits,"speed":"high" if d.get("speed",0)>25 else d.get("speed",0),"motion_score":round(health.motion_score,4),"local_motion_score":round(local_motion,4)})
            dec=Decision("send_alert","fast_person_detected",True,tid,conf,hits,dur,health.motion_score,0.0,conf_median,conf_max); self.last_decision=dec; return dec
        slow_local_min=float(slow.get("min_local_motion_score",0.006)); slow_speed_min=float(slow.get("min_track_speed",1.0))
        if slow.get("enabled",True) and conf>=float(slow.get("min_confidence",0.50)) and hits>=int(slow.get("min_hits",3)) and dur>=float(slow.get("min_duration_ms",800)) and (local_motion>=slow_local_min or track_speed>=slow_speed_min):
            self.cooldowns["global"]=now+float(es["alerts"].get("global_cooldown_seconds",45)); self.cooldowns["track"][tid]=now+float(es["alerts"].get("per_track_cooldown_seconds",90))
            self._log("PERSON_ALERT_SENT", {"reason":"slow_person_confirmed","detector":detector,"track":tid,"conf":conf,"hits":hits,"duration_ms":round(dur,1),"motion_score":round(health.motion_score,4)})
            dec=Decision("send_alert","slow_person_confirmed",True,tid,conf,hits,dur,health.motion_score); self.last_decision=dec; return dec
        self._log("PERSON_ALERT_PENDING", {"reason":"waiting_more_hits","hits":hits,"conf":conf,"track_id":tid,"motion_score":round(health.motion_score,4)})
        dec=Decision("pending","waiting_more_hits",False,tid,conf,hits,dur,health.motion_score); self.last_decision=dec; return dec
