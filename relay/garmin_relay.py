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
    요청 범위 안에 이미 저장된 코스가 ENOUGH(40)개 이상이면 Garmin 검색을 생략하고 저장분만 반환.
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
    with _idx_lock: idx_add(rec)
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

ENOUGH = 40   # 사용자 지정(2026-10-30): 그 주변에 이미 수십 개가 있으면 Garmin 검색 생략
_idx, _idx_lock = {}, threading.Lock()   # courseId -> (s, w, n, e) 저장된 코스의 범위

def idx_add(rec):
    p = rec.get("pts") or []
    if p: _idx[rec["id"]] = (min(a for a, _ in p), min(b for _, b in p), max(a for a, _ in p), max(b for _, b in p))

def idx_load():
    with _idx_lock:
        if _idx: return
        for f in os.listdir(CACHE):
            if f.endswith(".json"):
                try: idx_add(json.load(open(os.path.join(CACHE, f))))
                except Exception: pass

def near_cached(s, w, n, e):
    """요청 범위와 겹치는 저장 코스 중, 코스 범위의 절반 이상이 요청 범위 안에 있는 것"""
    idx_load(); out = []
    for cid, (a, b, c, d) in list(_idx.items()):
        if c < s or a > n or d < w or b > e: continue
        ih = max(0, min(c, n) - max(a, s)) * max(0, min(d, e) - max(b, w)); ar = max(1e-12, (c - a) * (d - b))
        if ih / ar >= 0.5 or ar < 1e-9: out.append(cid)
    return out

def popular(s, w, n, e, mx=400):
    have_ids = near_cached(s, w, n, e)
    if len(have_ids) >= ENOUGH:   # 이미 충분 → Garmin 검색 없이 저장분만 반환
        recs = [r for r in (cached(i) for i in have_ids[:mx]) if r and r.get("pts")]
        return {"courses": recs, "found": len(have_ids), "cached": len(recs), "crawling": 0, "skipped_search": True}
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

# ---- 많이 겹치는 길(인기 구간) 격자 (사용자 지정 2026-10-30: 친구 여부와 상관없이 공개 코스가 많이 겹치는 곳) ----
CELL = 0.00025          # 약 27m 격자
HEAT_MIN = 3            # 서로 다른 코스 3개 이상이 지난 칸만 '인기 구간'
_heat_memo = {}
def heat(s, w, n, e):
    k = (round(s, 3), round(w, 3), round(n, 3), round(e, 3), len(_idx))
    if k in _heat_memo: return dict(_heat_memo[k])
    r = _heat(s, w, n, e)
    if len(_heat_memo) > 50: _heat_memo.clear()
    _heat_memo[k] = r; return dict(r)
def _heat(s, w, n, e):
    """요청 범위와 겹치는 저장 코스 전부로 칸별 '서로 다른 코스 수'를 셈 → HEAT_MIN 이상인 칸만 반환"""
    idx_load(); cnt = {}; used = 0
    for cid, (a, b, c, d) in list(_idx.items()):
        if c < s or a > n or d < w or b > e: continue
        r = cached(cid)
        if not r or not r.get("pts"): continue
        used += 1; seen = set(); P = r["pts"]
        for i in range(1, len(P)):
            (la1, lo1), (la2, lo2) = P[i-1], P[i]
            k = max(1, int(max(abs(la2-la1), abs(lo2-lo1)) / (CELL/2)))
            for t in range(k + 1):
                la = la1 + (la2-la1)*t/k; lo = lo1 + (lo2-lo1)*t/k
                if s <= la <= n and w <= lo <= e: seen.add((int(math.floor(la/CELL)), int(math.floor(lo/CELL))))
        for key in seen: cnt[key] = cnt.get(key, 0) + 1
    cells = [[a, b, c] for (a, b), c in cnt.items() if c >= HEAT_MIN]
    return {"cell": CELL, "cells": cells, "courses_used": used, "min": HEAT_MIN}

# ---- 백그라운드 수집: 호치민 전역 공개 러닝 코스를 하루 한 번 천천히 ----
WARM_AREAS = {"호치민": (10.65, 106.55, 10.90, 106.85)}
WARM_DAILY_MAX = 1500   # 하루 최대 상세 수집 수(0.5초 간격 → 약 15~25분)
def warm_loop():
    import time
    time.sleep(60)
    while True:
        try:
            g = garmin(); mine = my_course_ids(g)
            for name, bb in WARM_AREAS.items():
                found = {}
                def rec(s_, w_, n_, e_, d=0, calls=[0]):
                    if calls[0] >= 800: return
                    calls[0] += 1
                    r = g.client.connectapi("/course-service/course/search", params={"south": s_, "west": w_, "north": n_, "east": e_})
                    L = r.get("courseSummaryDTOs") or []
                    for c in L: found[c["courseId"]] = c
                    time.sleep(0.2)
                    if (r.get("totalSearchHits") or 0) > len(L) and d < 9:
                        ms, mw = (s_+n_)/2, (w_+e_)/2
                        for b2 in ((s_, w_, ms, mw), (s_, mw, ms, e_), (ms, w_, n_, mw), (ms, mw, n_, e_)): rec(*b2, d=d+1, calls=calls)
                rec(*bb)
                todo = [c["courseId"] for c in sorted(found.values(), key=lambda c: -(c.get("updatedDate") or 0))
                        if (c.get("activityType") or {}).get("typeKey") in RUN_TYPES and c["courseId"] not in mine
                        and (c.get("distanceInMeters") or 0) <= 60000 and cached(c["courseId"]) is None][:WARM_DAILY_MAX]
                sys.stderr.write(f"[warm] {name}: 검색 {len(found)}개, 새로 받을 러닝 코스 {len(todo)}개\n")
                for cid in todo:
                    try: fetch_detail(g, cid)
                    except Exception: traceback.print_exc()
                    time.sleep(0.5)
        except Exception: traceback.print_exc()
        time.sleep(24 * 3600)

# ---- 바탕 지도(벡터 타일) Mac 보관 · 휴대폰 제공 (사용자 지정 2026-10-30: 'Mac에 저장 → 휴대폰이 가져오기, 위치 기반') ----
# Protomaps 일일 빌드(OpenStreetMap 데이터, ODbL)에서 지역만 잘라 ~/.runroute/maps/*.pmtiles 로 보관.
# 휴대폰은 /maps/index.json 으로 지역 목록을 받고, 화면이 들어 있는 지역 파일을 Range 요청으로 필요한 조각만 읽음.
MAPS = os.path.expanduser("~/.runroute/maps"); os.makedirs(MAPS, exist_ok=True)
REGION_HALF = 0.15      # 위치 기반 새 지역: 중심 ±0.15° (약 33km 사방)
_maps_busy = set()
def maps_index():
    out = []
    meta_f = os.path.join(MAPS, "regions.json")
    meta = json.load(open(meta_f)) if os.path.exists(meta_f) else {}
    for f in sorted(os.listdir(MAPS)):
        if f.endswith(".pmtiles"):
            m = meta.get(f) or ({"bbox": [10.65, 106.55, 10.90, 106.85], "name": "호치민"} if f == "hcmc.pmtiles" else None)
            if m: out.append({"file": f, "bbox": m["bbox"], "name": m.get("name", f), "size": os.path.getsize(os.path.join(MAPS, f)), "mtime": int(os.path.getmtime(os.path.join(MAPS, f)))})
    return {"regions": out, "busy": sorted(_maps_busy)}

def maps_ensure(lat, lon):
    for r in maps_index()["regions"]:
        s_, w_, n_, e_ = r["bbox"]
        if s_ + 0.02 <= lat <= n_ - 0.02 and w_ + 0.02 <= lon <= e_ - 0.02: return {"have": r["file"]}
    name = f"r_{lat:.2f}_{lon:.2f}.pmtiles".replace("-", "m")
    if name in _maps_busy: return {"busy": name}
    _maps_busy.add(name)
    def job():
        import subprocess, datetime
        try:
            bb = [round(lat - REGION_HALF, 3), round(lon - REGION_HALF, 3), round(lat + REGION_HALF, 3), round(lon + REGION_HALF, 3)]
            for d in (1, 2, 3):
                dt = (datetime.date.today() - datetime.timedelta(days=d)).strftime("%Y%m%d")
                src = f"https://build.protomaps.com/{dt}.pmtiles"
                tmp = os.path.join(MAPS, name + ".part")
                r = subprocess.run(["/opt/homebrew/bin/pmtiles", "extract", src, tmp, f"--bbox={bb[1]},{bb[0]},{bb[3]},{bb[2]}", "--maxzoom=15"], capture_output=True, text=True, timeout=900)
                if r.returncode == 0:
                    os.replace(tmp, os.path.join(MAPS, name))
                    meta_f = os.path.join(MAPS, "regions.json"); meta = json.load(open(meta_f)) if os.path.exists(meta_f) else {}
                    meta[name] = {"bbox": bb, "name": f"{lat:.2f},{lon:.2f} 주변", "src": dt}; json.dump(meta, open(meta_f, "w"), ensure_ascii=False)
                    sys.stderr.write(f"[maps] {name} 저장 ({os.path.getsize(os.path.join(MAPS, name))} bytes)\n"); break
        except Exception: traceback.print_exc()
        finally: _maps_busy.discard(name)
    threading.Thread(target=job, daemon=True).start()
    return {"started": name}

class H(BaseHTTPRequestHandler):
    def cors(self):
        o = self.headers.get("Origin")
        if o in ALLOWED_ORIGINS or (o or "").startswith("http://localhost") or (o or "").startswith("http://127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length, ETag")
            self.send_header("Access-Control-Allow-Headers", "content-type, x-rr-key, range, if-match")
    def reply(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.cors(); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.end_headers()
    def authed(self):
        if secrets.compare_digest(self.headers.get("X-RR-Key", ""), relay_key()): return True
        self.reply(401, {"error": "중계 키가 맞지 않습니다"}); return False
    def send_map_file(self, fn, head=False):
        fp = os.path.join(MAPS, os.path.basename(fn))
        if not fn.endswith(".pmtiles") or not os.path.exists(fp): return self.reply(404, {"error": "no map"})
        size = os.path.getsize(fp); etag = f'"{int(os.path.getmtime(fp))}-{size}"'
        rng = self.headers.get("Range"); start, end = 0, size - 1; code = 200
        if rng and rng.startswith("bytes="):
            a, b = rng[6:].split(",")[0].split("-")
            if a == "": start = max(0, size - int(b))
            else: start = int(a); end = min(size - 1, int(b)) if b else size - 1
            code = 206
        n = max(0, end - start + 1)
        self.send_response(code); self.cors()
        self.send_header("Content-Type", "application/octet-stream"); self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag); self.send_header("Cache-Control", "public, max-age=86400")
        if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(n)); self.end_headers()
        if head: return
        with open(fp, "rb") as f: f.seek(start); self.wfile.write(f.read(n))
    def do_HEAD(self):
        if self.path.startswith("/maps/") and self.path.split("?")[0].endswith(".pmtiles"): return self.send_map_file(self.path.split("?")[0][6:], head=True)
        self.send_response(404); self.end_headers()
    def do_GET(self):
        p0 = self.path.split("?")[0]
        if p0 == "/maps/index.json": return self.reply(200, maps_index())
        if p0.startswith("/maps/") and p0.endswith(".pmtiles"): return self.send_map_file(p0[6:])
        if p0 == "/maps/ensure":
            if not self.authed(): return
            from urllib.parse import urlparse, parse_qs
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try: return self.reply(200, maps_ensure(float(q["lat"]), float(q["lon"])))
            except Exception as ex: return self.reply(400, {"error": str(ex)})
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
                res = popular(s_, w_, n_, e_, min(int(q.get("max", 400)), 600))
                if q.get("mode") == "heat":   # 많이 겹치는 칸만(가벼움)
                    h = heat(s_, w_, n_, e_); h.update({k: res[k] for k in ("found", "cached", "crawling")}); res = h
                self.reply(200, res)
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
    threading.Thread(target=warm_loop, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
