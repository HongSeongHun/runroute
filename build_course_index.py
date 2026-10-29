#!/usr/bin/env python3
"""courses/*.gpx -> courses/index.json (RunRoute 코스 라이브러리)
사용: python3 build_course_index.py   (runroute 폴더에서 실행)"""
import glob, json, math, os, xml.etree.ElementTree as ET
NS={"g":"http://www.topografix.com/GPX/1/1"}
def hav(a,b):
    R=6371008.8; p1,p2=math.radians(a[0]),math.radians(b[0]); dp=p2-p1; dl=math.radians(b[1]-a[1])
    s=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2; return 2*R*math.asin(min(1,math.sqrt(s)))
def resample(pts,step=40):
    out=[pts[0]]; acc=0.0
    for i in range(1,len(pts)):
        d=hav(pts[i-1],pts[i]); acc+=d
        if acc>=step: out.append(pts[i]); acc=0.0
    if out[-1]!=pts[-1]: out.append(pts[-1])
    return out
here=os.path.dirname(os.path.abspath(__file__)); cdir=os.path.join(here,"courses"); items=[]
for f in sorted(glob.glob(os.path.join(cdir,"*.gpx"))):
    root=ET.parse(f).getroot()
    pts=[(float(p.get("lat")),float(p.get("lon"))) for p in root.iterfind(".//g:trkpt",NS)] or \
        [(float(p.get("lat")),float(p.get("lon"))) for p in root.iterfind(".//g:rtept",NS)]
    if len(pts)<2: continue
    nm=root.find(".//g:trk/g:name",NS) or root.find(".//g:metadata/g:name",NS)
    dist=sum(hav(pts[i-1],pts[i]) for i in range(1,len(pts)))
    tr=resample(pts)
    items.append({"id":os.path.basename(f)[:-4],"name":(nm.text.strip() if nm is not None and nm.text else os.path.basename(f)),
      "dist":round(dist,1),"loop":hav(pts[0],pts[-1])<300,
      "start":[round(pts[0][0],6),round(pts[0][1],6)],"end":[round(pts[-1][0],6),round(pts[-1][1],6)],
      "track":[[round(a,5),round(b,5)] for a,b in tr]})
json.dump({"version":1,"courses":items},open(os.path.join(cdir,"index.json"),"w"),ensure_ascii=False,separators=(",",":"))
print(len(items),"courses ->",os.path.join("courses","index.json"))
for c in items: print(f'{c["id"]:>10} {c["dist"]/1000:6.2f}km loop={c["loop"]!s:5} pts={len(c["track"]):4} start={c["start"]} {c["name"]}')
