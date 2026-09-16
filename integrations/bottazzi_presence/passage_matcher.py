from __future__ import annotations

from datetime import datetime

def parse_ts(value):
    if not value: return None
    try: return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except Exception: return None

def one_to_one_pairs(citofono_events, garden_events, window_seconds=180, claimed_garden=None):
    claimed=set(claimed_garden or [])
    candidates=[]
    for c in citofono_events:
        cid=c.get("event_id"); ct=parse_ts(c.get("ts"))
        if not cid or not ct: continue
        for g in garden_events:
            gid=g.get("event_id"); gt=parse_ts(g.get("ts"))
            if not gid or gid in claimed or not gt: continue
            delta=(gt-ct).total_seconds()
            if abs(delta) <= window_seconds: candidates.append((abs(delta),cid,gid,delta,c,g))
    candidates.sort(key=lambda x:(x[0],x[1],x[2]))
    used_c=set(); used_g=set(); out=[]
    for _,cid,gid,delta,c,g in candidates:
        if cid in used_c or gid in used_g: continue
        used_c.add(cid); used_g.add(gid); out.append({"citofono":c,"garden":g,"delta_seconds":delta})
    return out
