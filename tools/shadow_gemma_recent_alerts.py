import json, os, sys
from pathlib import Path
ROOT=Path('/home/bandi/ralfloop-garden-detection-refine')
SRC=ROOT/'integrations/bottazzi_garden'
os.environ.setdefault('HA_TOKEN','shadow-no-ha-call')
os.environ['BOTTAZZI_GARDEN_BASE']=str(SRC)
os.environ['BOTTAZZI_GARDEN_YOLO_MANIFEST']='/opt/bottazzi-garden/garden_model_manifest.json'
os.environ['BOTTAZZI_GARDEN_VISION_MODEL']='gemma4:12b-it-qat'
os.environ['BOTTAZZI_GARDEN_VISION_TIMEOUT_SECONDS']='35'
os.environ['BOTTAZZI_GARDEN_VALIDATE_YOLO']='1'
sys.path.insert(0,str(SRC))
import garden_person_detector as gd
gd.log=lambda obj: None
rows=[]
for line in Path('/opt/bottazzi-garden/events.jsonl').read_text(errors='replace').splitlines()[-80:]:
    try: row=json.loads(line)
    except Exception: continue
    if row.get('event')=='human_passage' and Path(str(row.get('snapshot',''))).exists(): rows.append(row)
for row in rows[-4:]:
    ok, verdict=gd.validate_with_vision(Path(row['snapshot']), row.get('hits') or [])
    print(json.dumps({'event_id':row.get('event_id'),'best_confidence':row.get('best_confidence'),'ok':ok,'vision':verdict},ensure_ascii=False))
