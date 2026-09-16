from __future__ import annotations

from dataclasses import dataclass
from statistics import median

@dataclass(frozen=True)
class FaceConsensusConfig:
    min_support_frames: int = 2
    min_support_ratio: float = 0.30
    max_dlib_distance: float = 0.39
    min_insight_score: float = 0.55
    min_insight_margin: float = 0.20
    min_insight_score_without_margin: float = 0.60
    min_insight_support_frames_for_authorization: int = 1
    min_winner_frame_margin: int = 1

def _f(v, default=None):
    try: return float(v)
    except (TypeError, ValueError): return default

def _evidence(hit, cfg):
    name=str(hit.get("match") or "").strip()
    if not name or name == "sconosciuto" or name.startswith("forse_"):
        return None
    engine=str(hit.get("engine") or ("insightface" if hit.get("score") is not None else "dlib"))
    dist=_f(hit.get("distance"), 999.0)
    score=_f(hit.get("score"))
    margin=_f(hit.get("margin"))
    if engine == "insightface" or score is not None:
        if score is None: score=max(0.0, 1.0-dist)
        supported = score >= (cfg.min_insight_score_without_margin if margin is None else cfg.min_insight_score)
        if margin is not None: supported = supported and margin >= cfg.min_insight_margin
        strength=score
    else:
        supported = dist <= cfg.max_dlib_distance
        strength=max(0.0, 1.0-dist)
    return {"name":name,"engine":engine,"distance":dist,"score":score,"margin":margin,"strength":strength,"supported":supported,"frame":str(hit.get("frame") or "") }

def summarize_face_burst(hits, frames_checked, allowlist=None, cfg=None):
    cfg=cfg or FaceConsensusConfig(); allow={x.strip().lower() for x in (allowlist or []) if x.strip()}
    per_frame={}
    for hit in hits or []:
        ev=_evidence(hit,cfg)
        if not ev: continue
        key=(ev["frame"],ev["name"].lower())
        if key not in per_frame or ev["strength"] > per_frame[key]["strength"]: per_frame[key]=ev
    by_name={}
    for ev in per_frame.values(): by_name.setdefault(ev["name"].lower(),[]).append(ev)
    summary=[]
    denom=max(int(frames_checked or 0), len({k[0] for k in per_frame}), 1)
    for name,rows in by_name.items():
        supports=[r for r in rows if r["supported"]]
        ds=[r["distance"] for r in supports if r["distance"] is not None and r["distance"] < 999]
        ss=[r["score"] for r in supports if r["score"] is not None]
        insight_supports=[r for r in supports if r["engine"] == "insightface"]
        dlib_supports=[r for r in supports if r["engine"] != "insightface"]
        summary.append({"name":name,"count":len(supports),"unique_frames":len(supports),"insight_support_frames":len(insight_supports),"dlib_support_frames":len(dlib_supports),"observed_frames":len(rows),"frame_ratio":round(len(supports)/denom,3),"median_distance":round(median(ds),4) if ds else None,"best_distance":round(min(ds),4) if ds else None,"median_score":round(median(ss),4) if ss else None,"best_score":round(max(ss),4) if ss else None})
    summary.sort(key=lambda x:(x["unique_frames"],x.get("median_score") or 0,-(x.get("median_distance") or 999)), reverse=True)
    if not summary: return {"verdict":"nessun_volto_riconosciuto","authorized":False,"reason":"no_supported_identity","summary":[]}
    top=summary[0]; runner=summary[1] if len(summary)>1 else None
    if top["unique_frames"] < cfg.min_support_frames or top["frame_ratio"] < cfg.min_support_ratio:
        return {"verdict":"incerto","authorized":False,"reason":"insufficient_multiframe_support","summary":summary}
    if runner and top["unique_frames"]-runner["unique_frames"] < cfg.min_winner_frame_margin:
        return {"verdict":"incerto","authorized":False,"reason":"identity_competition","summary":summary}
    verdict=top["name"]
    if allow and verdict.lower() not in allow:
        return {"verdict":verdict,"authorized":False,"reason":"identity_not_allowed","summary":summary}
    if top.get("insight_support_frames", 0) < cfg.min_insight_support_frames_for_authorization:
        return {"verdict":verdict,"authorized":False,"reason":"authorization_requires_insightface","summary":summary}
    return {"verdict":verdict,"authorized":True,"reason":"multiframe_consensus_with_insightface","summary":summary}
