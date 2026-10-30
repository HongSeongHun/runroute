/* RunRoute 지도 타일·라이브러리 캐시 (cache-first, 7일)
   - tile.openstreetmap.org 타일: 한 번 받은 조각은 7일간 휴대폰에서 바로 표시(OSM 타일 정책의 '클라이언트 캐시' 권장 사항)
   - cdnjs Leaflet 파일: 버전 고정 URL이라 캐시
   - 그 외 요청(앱 HTML, Overpass, 중계 서버)은 건드리지 않음 */
const TILE="rr-tiles-v1",LIB="rr-lib-v1",MAX_AGE=7*864e5,MAX_TILES=3000;
self.addEventListener("install",e=>self.skipWaiting());
self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));
async function trim(c,m){const k=await c.keys();if(k.length>MAX_TILES)for(const r of k.slice(0,k.length-MAX_TILES)){await c.delete(r);await m.delete(r.url);}}
self.addEventListener("fetch",e=>{const u=e.request.url;
  if(e.request.method!=="GET")return;
  if(u.startsWith("https://tile.openstreetmap.org/")||/^https:\/\/[abc]\.tile\.openstreetmap\.fr\//.test(u)){e.respondWith((async()=>{const c=await caches.open(TILE),m=await caches.open(TILE+"-t");
      const hit=await c.match(e.request),t=await m.match(u);const age=t?Date.now()-(+(await t.text())||0):Infinity;
      if(hit&&age<MAX_AGE)return hit;
      try{const r=await fetch(e.request);if(r.ok||r.type==="opaque"){c.put(e.request,r.clone()).then(()=>m.put(u,new Response(String(Date.now())))).then(()=>trim(c,m));}
        else if(hit)return hit;return r;}catch(err){if(hit)return hit;throw err;}})());}
  else if(u.startsWith("https://cdnjs.cloudflare.com/ajax/libs/leaflet/")){e.respondWith(caches.open(LIB).then(async c=>{const hit=await c.match(e.request);if(hit)return hit;const r=await fetch(e.request);if(r.ok)c.put(e.request,r.clone());return r;}));}
});
