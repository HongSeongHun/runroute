#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["garminconnect==0.3.2"]
# ///
"""RunRoute → Garmin Connect 중계 서버 (개인용)

- 127.0.0.1:8787 에서만 대기. 외부 노출은 `tailscale serve`(tailnet 내부 전용 HTTPS)로만.
- 인증: 헤더 X-RR-Key == ~/.runroute_relay_key (최초 실행 시 자동 생성)
- Garmin 로그인: garmin_mcp 와 같은 토큰 폴더 ~/.garminconnect (비밀번호 사용 안 함)
- POST /course  {"name": str, "gpx": str, "public": true}
    → /course-service/course/import (GPX 파싱) → /course-service/course (생성, rulePK=1 공개)
    → {"courseId", "url", "rulePK", "name", "distance_m"}
- GET /health → {"ok": true, "garmin": "<표시 이름>"}
- GET /popular?s=&w=&n=&e=&max=400 → 그 범위의 다른 사람 '공개' 러닝 코스 선(좌표).
    Garmin Connect 코스 탐색과 같은 /course-service/course/search?south&west&north&east (호출당 최대 50개)
    → 결과가 50개로 잘리면 4분할(최대 깊이 4) → 러닝 종류만, 내 코스 제외 → /course-service/course/{id} 상세의 geoPoints.
    상세는 ~/.runroute/gcache/ 에 저장(코스 하나당 1회만 호출). 미수집분은 백그라운드로 천천히(0.4s 간격) 수집하고
    응답은 현재까지 수집된 것만 즉시 반환 → {"courses":[{"id","d","pts":[[lat,lon],...]}],"found","cached","crawling"}
"""
import io, json, math, os, secrets, sys, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from garminconnect import Garmin

HOST, PORT = "127.0.0.1", int(os.environ.get("RR_RELAY_PORT", "8787"))
ALLOWED_ORIGINS = {"https://hongseonghun.github.io", "null"}  # null = 로컬 파일로 열었을 때
KEY_FILE = os.path.expanduser("~/.runroute_relay_key")
TOKENS = os.path.expanduser(os.environ.get("GARMINTOKENS", "~/.garminconnect"))
RULE_PUBLIC, RULE_PRIVATE = 1, 2
MAX_BODY = 5 * 1024 * 1024

def relay_key():
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w") as f: f.write(secrets.token_urlsafe(24))
        os.chmod(KEY_FILE, 0o600)
    return open(KEY_FILE).read().strip()

_g, _lock = None, threading.Lock()
def garmin():
    global _g
    with _lock:
        if _g is None:
            g = Garmin(); g.login(TOKENS); _g = g
        return _g

def hav(a, b):
    R = 6371008.8; p1, p2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    dp, dl = p2 - p1, math.radians(b["longitude"] - a["longitude"])
    return 2 * R * math.asin(math.sqrt(math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2))

def bearing(a, b):
    p1, p2 = math.radians(a["latitude"]), math.radians(b["latitude"]); dl = math.radians(b["longitude"] - a["longitude"])
    return (math.degrees(math.atan2(math.sin(dl)*math.cos(p2), math.cos(p1)*math.sin(p2)-math.sin(p1)*math.cos(p2)*math.cos(dl))) + 360) % 360

def create_course(name, gpx_text, public=True):
    g = garmin()
    parsed = g.client.post("connectapi", "/course-service/course/import",
        files={"file": ("runroute.gpx", io.BytesIO(gpx_text.encode()), "application/gpx+xml")}, api=True)
    pts = parsed.get("geoPoints") or []
    if not pts: raise ValueError("GPX에 좌표가 없습니다")
    for p in pts:
        if p.get("elevation") is None: p["elevation"] = 0.0
    lats = [p["latitude"] for p in pts]; lons = [p["longitude"] for p in pts]
    payload = {  # garmin_mcp _build_course_payload 와 동일, rulePK 만 공개로
        "courseName": name[:128], "description": "RunRoute Planner", "openStreetMap": False, "matchedToSegments": False,
        "userProfilePk": None, "userGroupPk": None, "rulePK": RULE_PUBLIC if public else RULE_PRIVATE,
        "geoRoutePk": None, "sourceTypeId": 3, "sourcePk": None,
        "distanceMeter": sum(hav(pts[i-1], pts[i]) for i in range(1, len(pts))),
        "elevationGainMeter": 0.0, "elevationLossMeter": 0.0,
        "startPoint": {"latitude": pts[0]["latitude"], "longitude": pts[0]["longitude"], "elevation": pts[0]["elevation"], "distance": None, "timestamp": None},
        "coursePoints": [], "boundingBox": {
            "center": {"latitude": (min(lats)+max(lats))/2, "longitude": (min(lons)+max(lons))/2},
            "lowerLeft": {"latitude": min(lats), "longitude": min(lons)}, "upperRight": {"latitude": max(lats), "longitude": max(lons)},
            "lowerLeftLatIsSet": True, "lowerLeftLongIsSet": True, "upperRightLatIsSet": True, "upperRightLongIsSet": True},
        "hasShareableEvent": False, "hasTurnDetectionDisabled": False, "activityTypePk": 1,  # running
        "virtualPartnerId": None, "includeLaps": False, "elapsedSeconds": None,
        "startBearing": bearing(pts[0], pts[-1]), "endBearing": None, "geoPoints": pts}
    saved = g.client.post("connectapi", "/course-service/course", json=payload, api=True)
    cid = saved.get("courseId")
    # 실제 저장된 공개 범위 재확인
    detail = g.client.connectapi(f"/course-service/course/{cid}")
    return {"courseId": cid, "name": saved.get("courseName"), "distance_m": round(saved.get("distanceMeter") or 0),
            "rulePK": detail.get("rulePK"), "public": detail.get("rulePK") == RULE_PUBLIC,
            "url": f"https://connect.garmin.com/modern/course/{cid}"}

CACHE = os.path.expanduser("~/.runroute/gcache"); os.makedirs(CACHE, exist_ok=True)
RUN_TYPES = {"running", "trail_running", "track_running", "street_running", "ultra_run", "obstacle_run"}
_crawl_lock, _crawling = threading.Lock(), set()
_mine = {"ids": None}

def my_course_ids(g):
    if _mine["ids"] is None:
        try:
            r = g.client.connectapi("/course-service/course")
            lst = r if isinstance(r, list) else (r.get("coursesForUser") or r.get("courses") or [])
            _mine["ids"] = {c.get("courseId") for c in lst}
        except Exception: _mine["ids"] = set()
    return _mine["ids"]

def search_cells(g, s, w, n, e, depth=0, out=None, calls=None):
    out = {} if out is None else out; calls = calls if calls is not None else [0]
    if calls[0] >= 85: return out
    calls[0] += 1
    r = g.client.connectapi("/course-service/course/search", params={"south": s, "west": w, "north": n, "east": e})
    lst = r.get("courseSummaryDTOs") or []
    for c in lst: out[c["courseId"]] = c
    if (r.get("totalSearchHits") or 0) > len(lst) and depth < 4:
        ms, mw = (s + n) / 2, (w + e) / 2
        for bb in ((s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)):
            search_cells(g, *bb, depth + 1, out, calls)
    return out

def thin(pts, step=20.0):
    o = [pts[0]]
    for p in pts[1:]:
        if hav({"latitude": o[-1][0], "longitude": o[-1][1]}, {"latitude": p[0], "longitude": p[1]}) >= step: o.append(p)
    if o[-1] != pts[-1]: o.append(pts[-1])
    return o

def cached(cid):
    f = os.path.join(CACHE, f"{cid}.json")
    if os.path.exists(f):
        try: return json.load(open(f))
        except Exception: return None
    return None

def fetch_detail(g, cid):
    d = g.client.connectapi(f"/course-service/course/{cid}")
    gp = d.get("geoPoints") or []
    pts = [[round(p["latitude"], 6), round(p["longitude"], 6)] for p in gp if p.get("latitude") is not None]
    rec = {"id": cid, "d": round(d.get("distanceMeter") or 0), "pts": thin(pts) if len(pts) > 1 else []}
    json.dump(rec, open(os.path.join(CACHE, f"{cid}.json"), "w"))
    return rec

def crawl(g, ids):
    import time
    for cid in ids:
        with _crawl_lock:
            if cid in _crawling: continue
            _crawling.add(cid)
        try:
            if cached(cid) is None: fetch_detail(g, cid); time.sleep(0.4)
        except Exception: traceback.print_exc()
        finally:
            with _crawl_lock: _crawling.discard(cid)

def popular(s, w, n, e, mx=400):
    g = garmin(); mine = my_course_ids(g)
    found = search_cells(g, s, w, n, e)
    runs = [c for c in found.values() if (c.get("activityType") or {}).get("typeKey") in RUN_TYPES
            and c["courseId"] not in mine and (c.get("distanceInMeters") or 0) <= 60000]
    runs.sort(key=lambda c: -(c.get("updatedDate") or 0)); runs = runs[:mx]
    have, todo = [], []
    for c in runs:
        r = cached(c["courseId"])
        (have if r else todo).append(r or c["courseId"])
    if todo: threading.Thread(target=crawl, args=(g, todo), daemon=True).start()
    return {"courses": [r for r in have if r.get("pts")], "found": len(runs), "cached": len(have), "crawling": len(todo)}

class H(BaseHTTPRequestHandler):
    def cors(self):
        o = self.headers.get("Origin")
        if o in ALLOWED_ORIGINS or (o or "").startswith("http://localhost") or (o or "").startswith("http://127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "content-type, x-rr-key")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
    def reply(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.cors(); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.end_headers()
    def authed(self):
        if secrets.compare_digest(self.headers.get("X-RR-Key", ""), relay_key()): return True
        self.reply(401, {"error": "중계 키가 맞지 않습니다"}); return False
    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            if not self.authed(): return
            try: self.reply(200, {"ok": True, "garmin": garmin().get_full_name()})
            except Exception as e: self.reply(502, {"error": f"Garmin 로그인 실패: {e}"})
        elif self.path.startswith("/popular"):
            if not self.authed(): return
            from urllib.parse import urlparse, parse_qs
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                s_, w_, n_, e_ = (float(q[k]) for k in ("s", "w", "n", "e"))
                if not (0 < n_ - s_ <= 0.3 and 0 < e_ - w_ <= 0.3): return self.reply(400, {"error": "범위가 너무 큽니다(0.3° 이하)"})
                self.reply(200, popular(s_, w_, n_, e_, min(int(q.get("max", 400)), 600)))
            except KeyError: self.reply(400, {"error": "s,w,n,e 필요"})
            except Exception as ex:
                traceback.print_exc(); self.reply(502, {"error": f"Garmin 코스 검색 실패: {ex}"})
        else: self.reply(404, {"error": "not found"})
    def do_POST(self):
        if self.path.rstrip("/") != "/course": return self.reply(404, {"error": "not found"})
        if not self.authed(): return
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY: return self.reply(413, {"error": "요청 크기 오류"})
        try:
            req = json.loads(self.rfile.read(n))
            res = create_course(str(req.get("name") or "RunRoute"), str(req["gpx"]), bool(req.get("public", True)))
            self.reply(200, res)
        except Exception as e:
            traceback.print_exc(); self.reply(502, {"error": f"Garmin 등록 실패: {e}"})
    def log_message(self, fmt, *a): sys.stderr.write("[relay] " + fmt % a + "\n")

if __name__ == "__main__":
    k = relay_key()
    if "--print-key" in sys.argv: print(k); sys.exit(0)
    print(f"RunRoute Garmin relay on http://{HOST}:{PORT}  (key file {KEY_FILE})", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
