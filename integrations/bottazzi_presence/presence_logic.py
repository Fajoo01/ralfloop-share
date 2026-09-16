from __future__ import annotations

from datetime import datetime

def parse_ts(value):
    if not value: return None
    try: return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except Exception: return None

def face_summary_row(face_event):
    verdict=str(face_event.get("verdict") or "")
    rows=face_event.get("summary") or []
    return next((r for r in rows if str(r.get("name") or "") == verdict), rows[0] if rows else {})

def trusted_face_event(face_event, max_distance=0.39):
    verdict=str(face_event.get("verdict") or "")
    if not verdict or verdict in {"incerto","nessun_volto_riconosciuto"}: return False
    consensus=face_event.get("consensus") or {}
    if consensus.get("verdict") == verdict and consensus.get("reason") in {
        "multiframe_consensus", "multiframe_consensus_with_insightface", "identity_not_allowed"
    }:
        return True
    row=face_summary_row(face_event)
    try: return float(row.get("best_distance")) <= float(max_distance)
    except (TypeError,ValueError): return False

def should_process_passage(passage, cutover_ts, backfill=False):
    if backfill:
        return True
    cutover=parse_ts(cutover_ts)
    passage_ts=parse_ts(passage.get("ts"))
    if cutover is None or passage_ts is None:
        return False
    return passage_ts >= cutover

def correlate_passage_face(passage, face_event):
    if passage.get("citofono_event_id") != face_event.get("event_id"): return None
    if not trusted_face_event(face_event): return None
    name=str(face_event.get("verdict"))
    row=face_summary_row(face_event)
    observed=passage.get("garden_ts") or passage.get("citofono_ts")
    return {
        "source":"presence_correlator_v2", "event":"presence_inferred",
        "name":name, "track_id":passage.get("track_id"),
        "direction_hint":passage.get("direction_hint"), "presence_state":passage.get("presence_state"),
        "observed_ts":observed, "citofono_event_id":passage.get("citofono_event_id"),
        "garden_event_id":passage.get("garden_event_id"), "garden_delta_seconds":passage.get("garden_delta_seconds"),
        "citofono_best_distance":row.get("best_distance"), "citofono_best_score":row.get("best_score"),
        "snapshot":passage.get("snapshot"), "garden_clip":passage.get("garden_clip"),
    }
