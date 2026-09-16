#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path
import cv2, numpy as np
from insightface.app import FaceAnalysis

SUFFIXES={".jpg",".jpeg",".png",".webp"}

def blur_score(img):
    g=cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g,cv2.CV_64F).var())

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--base",default=str(Path.home()/"bottazzi-face"))
    ap.add_argument("--output",required=True)
    ap.add_argument("--min-det",type=float,default=.60)
    ap.add_argument("--min-area",type=float,default=1800)
    ap.add_argument("--min-blur",type=float,default=12)
    a=ap.parse_args(); base=Path(a.base); out=Path(a.output)
    app=FaceAnalysis(name="buffalo_l",providers=["CPUExecutionProvider"]); app.prepare(ctx_id=-1,det_size=(640,640))
    people={}; rejected=[]
    roots=[base/"known",base/"known_insight"]
    for root in roots:
        if not root.exists(): continue
        for person in sorted(root.iterdir()):
            if not person.is_dir() or person.name.startswith("_"): continue
            for path in sorted(person.iterdir()):
                if path.suffix.lower() not in SUFFIXES: continue
                img=cv2.imread(str(path))
                if img is None: rejected.append({"path":str(path),"reason":"unreadable"}); continue
                faces=app.get(img)
                if len(faces)!=1: rejected.append({"path":str(path),"reason":f"faces_{len(faces)}"}); continue
                face=faces[0]; x1,y1,x2,y2=[float(x) for x in face.bbox.tolist()]; area=max(0,x2-x1)*max(0,y2-y1)
                bs=blur_score(img[max(0,int(y1)):max(1,int(y2)),max(0,int(x1)):max(1,int(x2))]) if x2>x1 and y2>y1 else 0
                if float(face.det_score)<a.min_det or area<a.min_area or bs<a.min_blur:
                    rejected.append({"path":str(path),"reason":"quality","det":float(face.det_score),"area":area,"blur":bs}); continue
                people.setdefault(person.name,[]).append({"embedding":[float(x) for x in face.embedding.astype("float32").tolist()],"source":str(path),"det_score":round(float(face.det_score),4),"face_area":round(area,1),"blur":round(bs,1)})
    payload={"schema_version":2,"model":"buffalo_l","people":people,"stats":{"accepted":{k:len(v) for k,v in people.items()},"rejected":len(rejected)},"rejected":rejected}
    out.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(payload["stats"],ensure_ascii=False))
if __name__=="__main__": main()
