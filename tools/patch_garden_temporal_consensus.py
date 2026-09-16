from pathlib import Path
import json

ROOT = Path('/home/bandi/ralfloop-garden-detection-refine')
SUP = ROOT / 'integrations/bottazzi_garden/person_event_supervisor.py'
DET = ROOT / 'integrations/bottazzi_garden/garden_person_detector.py'
CFG = ROOT / 'integrations/bottazzi_garden/event_supervisor_config.json'


def replace_once(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        raise RuntimeError(f'{label}: expected exactly 1 match, found {n}')
    return text.replace(old, new, 1)

s = SUP.read_text()
s = replace_once(s, 'import json, math, time\n', 'import json, math, time\nfrom statistics import median\n', 'median import')
s = replace_once(
    s,
    '    cooldown_remaining_seconds: float = 0.0\n',
    '    cooldown_remaining_seconds: float = 0.0\n    confidence_median: float = 0.0\n    confidence_max: float = 0.0\n',
    'decision evidence fields',
)
s = replace_once(
    s,
    '                tr.update({"bbox":det["bbox"],"center":new_center,"last_seen":ts,"hits":tr["hits"]+1,"lost":0,"matched":True,"confidence_sum":tr["confidence_sum"]+det["confidence"],"confidence_max":max(tr["confidence_max"],det["confidence"]),"speed":speed})\n                tr["trajectory"].append(new_center); det["track_id"]=best_id; assigned.add(best_id)\n',
    '                tr.update({"bbox":det["bbox"],"center":new_center,"last_seen":ts,"hits":tr["hits"]+1,"lost":0,"matched":True,"confidence_sum":tr["confidence_sum"]+det["confidence"],"confidence_max":max(tr["confidence_max"],det["confidence"]),"speed":speed})\n                tr.setdefault("confidence_history", []).append(float(det["confidence"]))\n                tr["confidence_history"] = tr["confidence_history"][-7:]\n                tr["trajectory"].append(new_center); det["track_id"]=best_id; assigned.add(best_id)\n',
    'matched confidence history',
)
s = replace_once(
    s,
    '                self.tracks[tid]={"track_id":tid,"first_seen":ts,"last_seen":ts,"hits":1,"lost":0,"bbox":det["bbox"],"center":c,"trajectory":[c],"confidence_sum":det["confidence"],"confidence_max":det["confidence"],"speed":0.0,"matched":True}\n',
    '                self.tracks[tid]={"track_id":tid,"first_seen":ts,"last_seen":ts,"hits":1,"lost":0,"bbox":det["bbox"],"center":c,"trajectory":[c],"confidence_sum":det["confidence"],"confidence_max":det["confidence"],"confidence_history":[float(det["confidence"])],"speed":0.0,"matched":True}\n',
    'new confidence history',
)
s = replace_once(
    s,
    '                det["track_hits"]=tr["hits"]; det["track_duration_ms"]=(tr["last_seen"]-tr["first_seen"])*1000.0; det["speed"] = tr.get("speed",0.0); det["conf_avg"]=tr["confidence_sum"]/max(1,tr["hits"]); det["conf_max"]=tr["confidence_max"]\n',
    '                history=[float(v) for v in tr.get("confidence_history", [])[-7:]]\n                padded=([0.0] * max(0, 3-len(history)) + history)\n                det["track_hits"]=tr["hits"]; det["track_duration_ms"]=(tr["last_seen"]-tr["first_seen"])*1000.0; det["speed"] = tr.get("speed",0.0); det["conf_avg"]=tr["confidence_sum"]/max(1,tr["hits"]); det["conf_max"]=tr["confidence_max"]; det["conf_history"]=history; det["conf_median"]=float(median(padded)) if padded else 0.0\n',
    'expose median evidence',
)
s = replace_once(
    s,
    '        persons=self.tracker.update(persons,timestamp)\n        now=timestamp\n',
    '''        # A duplicated/frozen frame is not new evidence. The old code updated the\n        # tracker first, so one static false positive could accumulate many "hits".\n        # Block before association and do not mature the track on repeated pixels.\n        if health.duplicated or health.frozen:\n            raw_best=max(persons,key=lambda x:x.get("confidence",0),default={})\n            conf=float(raw_best.get("confidence",0.0) or 0.0)\n            reason=health.reason or "duplicate_or_frozen_frame"\n            self._log("PERSON_ALERT_BLOCKED", {"reason":reason,"conf":conf,"frame_gap_ms":round(health.timestamp_gap_ms,1),"motion_score":round(health.motion_score,4),"evidence_counted":False,"frame_health":asdict(health)})\n            dec=Decision("block",reason,False,None,conf,0,0.0,health.motion_score)\n            self.last_decision=dec\n            return dec\n        persons=self.tracker.update(persons,timestamp)\n        now=timestamp\n''',
    'duplicate evidence gate',
)
s = replace_once(
    s,
    '        d=max(valid,key=lambda x:x.get("confidence",0)); tid=d.get("track_id"); hits=int(d.get("track_hits",1)); dur=float(d.get("track_duration_ms",0)); conf=float(d.get("confidence",0))\n',
    '        d=max(valid,key=lambda x:x.get("confidence",0)); tid=d.get("track_id"); hits=int(d.get("track_hits",1)); dur=float(d.get("track_duration_ms",0)); conf=float(d.get("confidence",0)); conf_median=float(d.get("conf_median",0.0)); conf_max=float(d.get("conf_max",conf))\n',
    'decision score fields',
)
needle = '        remain=max(0.0, self.cooldowns["global"]-now, self.cooldowns["track"].get(tid,0)-now)\n'
replacement = '''        fast=es["person_fast"]; slow=es["person_slow"]\n        init_hits=max(2, int(fast.get("min_hits",2)))\n        median_threshold=float(fast.get("median_threshold",0.55))\n        single_review_conf=float(fast.get("single_frame_vision_confidence",0.82))\n        single_review_motion=float(fast.get("single_frame_vision_motion_score",0.04))\n        if detector != "motion_fallback" and hits < init_hits:\n            if hits == 1 and conf >= single_review_conf and health.motion_score >= single_review_motion:\n                self._log("PERSON_ALERT_PENDING", {"reason":"single_frame_requires_vision","track_id":tid,"conf":conf,"conf_median":round(conf_median,4),"conf_max":round(conf_max,4),"hits":hits,"motion_score":round(health.motion_score,4)})\n                dec=Decision("review","single_frame_requires_vision",False,tid,conf,hits,dur,health.motion_score,0.0,conf_median,conf_max)\n                self.last_decision=dec\n                return dec\n            self._log("PERSON_ALERT_PENDING", {"reason":"track_initializing","track_id":tid,"conf":conf,"conf_median":round(conf_median,4),"hits":hits,"required_hits":init_hits,"motion_score":round(health.motion_score,4)})\n            dec=Decision("pending","track_initializing",False,tid,conf,hits,dur,health.motion_score,0.0,conf_median,conf_max)\n            self.last_decision=dec\n            return dec\n        if detector != "motion_fallback" and conf_median < median_threshold:\n            self._log("PERSON_ALERT_PENDING", {"reason":"track_score_consensus_low","track_id":tid,"conf":conf,"conf_median":round(conf_median,4),"threshold":median_threshold,"hits":hits,"motion_score":round(health.motion_score,4)})\n            dec=Decision("pending","track_score_consensus_low",False,tid,conf,hits,dur,health.motion_score,0.0,conf_median,conf_max)\n            self.last_decision=dec\n            return dec\n        remain=max(0.0, self.cooldowns["global"]-now, self.cooldowns["track"].get(tid,0)-now)\n'''
s = replace_once(s, needle, replacement, 'temporal consensus gate')
s = replace_once(s, '        fast=es["person_fast"]; slow=es["person_slow"]\n        if fast.get("enabled",True)', '        if fast.get("enabled",True)', 'remove duplicate fast slow')
s = replace_once(
    s,
    '            self._log("PERSON_ALERT_SENT", {"reason":"fast_person_detected","detector":detector,"track":tid,"conf":conf,"hits":hits,"speed":"high" if d.get("speed",0)>25 else d.get("speed",0),"motion_score":round(health.motion_score,4)})\n            dec=Decision("send_alert","fast_person_detected",True,tid,conf,hits,dur,health.motion_score); self.last_decision=dec; return dec\n',
    '            self._log("PERSON_ALERT_SENT", {"reason":"fast_person_detected","detector":detector,"track":tid,"conf":conf,"conf_median":round(conf_median,4),"conf_max":round(conf_max,4),"hits":hits,"speed":"high" if d.get("speed",0)>25 else d.get("speed",0),"motion_score":round(health.motion_score,4)})\n            dec=Decision("send_alert","fast_person_detected",True,tid,conf,hits,dur,health.motion_score,0.0,conf_median,conf_max); self.last_decision=dec; return dec\n',
    'fast decision evidence',
)
SUP.write_text(s)

s = DET.read_text()
s = replace_once(
    s,
    '''            alert_hits = filter_night_dynamic_cluster_memory_hits(alert_hits, locals().get('health'), img_path)\n            alert_hits = filter_day_large_low_motion_false_positive_hits(alert_hits, locals().get('health'), img_path)\n\n            frame = cv2.imread(str(img_path))\n            health = analyze_frame_health(prev_supervisor_frame, frame, now, prev_supervisor_ts, supervisor_freeze_state, supervisor_cfg)\n''',
    '''            # Compute frame health before any motion-aware filtering. Previously the filters\n            # saw locals().get("health") before it existed and therefore treated motion as zero.\n            frame = cv2.imread(str(img_path))\n            health = analyze_frame_health(prev_supervisor_frame, frame, now, prev_supervisor_ts, supervisor_freeze_state, supervisor_cfg)\n            alert_hits = filter_night_dynamic_cluster_memory_hits(alert_hits, health, img_path)\n            alert_hits = filter_day_large_low_motion_false_positive_hits(alert_hits, health, img_path)\n''',
    'health ordering',
)
s = replace_once(
    s,
    '                    dets.append({"confidence": float(h.get("confidence") or 0), "box": [x, y, x + bw, y + bh]})\n',
    '                    dets.append({"confidence": float(h.get("confidence") or 0), "box": [x, y, x + bw, y + bh], "detector": str(h.get("detector") or "unknown")})\n',
    'detector metadata',
)
s = replace_once(
    s,
    '''                elif decision.reason == "single_frame_low_motion" and prev_supervisor_frame is None:\n''',
    '''                elif decision.reason == "single_frame_requires_vision":\n                    # Preserve recall for a fast one-frame passage, but never alert directly:\n                    # force the VLM verifier to arbitrate this ambiguous case.\n                    alert_allowed = True\n                    for h in alert_hits:\n                        h["supervisor_reason"] = "single_frame_requires_vision"\n                        h["supervisor_motion_score"] = round(decision.motion_score, 4)\n                        h["track_id"] = decision.track_id\n                        h["supervisor_confidence_median"] = round(decision.confidence_median, 4)\n                        h["supervisor_confidence_max"] = round(decision.confidence_max, 4)\n                    log({"event": "alert_deferred_to_vision", "reason": "single_frame_requires_vision", "track_id": decision.track_id})\n                elif decision.reason == "single_frame_low_motion" and prev_supervisor_frame is None:\n''',
    'single frame vision route',
)
s = replace_once(
    s,
    '    supervisor_approved = any(h.get("supervisor_approved") for h in hits)\n',
    '    supervisor_approved = any(h.get("supervisor_approved") for h in hits)\n    has_yolo_detection = any(str(h.get("detector", "")).startswith("yolo") for h in hits)\n',
    'has yolo marker',
)
s = replace_once(
    s,
    '    if supervisor_approved and not needs_motion_vision and not force_supervisor_vision and not force_fp_fn_vision:\n        return True, {"skipped": True, "reason": "supervisor_approved", "decision": hits[0].get("supervisor_reason")}\n',
    '    if supervisor_approved and not needs_motion_vision and not force_supervisor_vision and not force_fp_fn_vision and not (VISION_VALIDATE_YOLO and has_yolo_detection):\n        return True, {"skipped": True, "reason": "supervisor_approved", "decision": hits[0].get("supervisor_reason")}\n',
    'respect validate yolo',
)
# Composite full-frame + zoom evidence for Gemma, improving distant/partial-person recall.
insert_before = 'def validate_with_vision(img_path: Path, hits):\n'
helper = '''def build_vision_evidence_image(img_path: Path, hits):\n    try:\n        img = cv2.imread(str(img_path))\n        if img is None or not hits:\n            return img_path\n        h, w = img.shape[:2]\n        boxes = []\n        for hit in hits:\n            box = hit.get("box") or hit.get("bbox") or []\n            if len(box) < 4:\n                continue\n            x, y, bw, bh = [int(float(v)) for v in box[:4]]\n            x1=max(0,x); y1=max(0,y); x2=min(w,x+max(1,bw)); y2=min(h,y+max(1,bh))\n            if x2>x1 and y2>y1:\n                boxes.append((x1,y1,x2,y2))\n        if not boxes:\n            return img_path\n        x1=min(b[0] for b in boxes); y1=min(b[1] for b in boxes); x2=max(b[2] for b in boxes); y2=max(b[3] for b in boxes)\n        padx=max(20,int((x2-x1)*0.35)); pady=max(20,int((y2-y1)*0.25))\n        x1=max(0,x1-padx); y1=max(0,y1-pady); x2=min(w,x2+padx); y2=min(h,y2+pady)\n        crop=img[y1:y2,x1:x2]\n        if crop.size == 0:\n            return img_path\n        context=img.copy()\n        for bx1,by1,bx2,by2 in boxes:\n            cv2.rectangle(context,(bx1,by1),(bx2,by2),(255,255,255),2)\n        target_h=max(180,min(480,h))\n        context_w=max(1,int(w*target_h/h))\n        crop_w=max(1,int(crop.shape[1]*target_h/crop.shape[0]))\n        context=cv2.resize(context,(context_w,target_h))\n        crop=cv2.resize(crop,(crop_w,target_h))\n        evidence=cv2.hconcat([context,crop])\n        out=Path('/run/bottazzi-garden-vision-evidence.jpg')\n        cv2.imwrite(str(out),evidence,[int(cv2.IMWRITE_JPEG_QUALITY),92])\n        return out if out.exists() and out.stat().st_size > 1000 else img_path\n    except Exception as exc:\n        log({"event":"vision_evidence_build_failed","error":repr(exc),"image":str(img_path)})\n        return img_path\n\n\n'''
s = replace_once(s, insert_before, helper + insert_before, 'vision evidence helper')
s = replace_once(
    s,
    '        b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")\n        prompt = (\n',
    '        evidence_path = build_vision_evidence_image(img_path, hits)\n        b64 = base64.b64encode(evidence_path.read_bytes()).decode("ascii")\n        prompt = (\n',
    'use evidence image',
)
s = replace_once(
    s,
    '            "You are validating a security camera alert. "\n',
    '            "You are validating a security camera alert. The image may contain the full scene on the left and a zoomed candidate crop on the right. "\n',
    'vision prompt context',
)
DET.write_text(s)

cfg = json.loads(CFG.read_text())
fast = cfg['event_supervisor']['person_fast']
fast['min_hits'] = 2
fast['median_threshold'] = 0.55
fast['single_frame_vision_confidence'] = 0.82
fast['single_frame_vision_motion_score'] = 0.04
CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
print('PATCH_OK')
