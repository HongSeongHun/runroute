/* RunRoute 라이브러리 캐시 (cache-first)
   - cdnjs Leaflet, unpkg protomaps-leaflet: 버전 고정 URL이라 휴대폰에 보관
   - OSM 표준 타일(tile.openstreetmap.org)은 OSMF 타일 정책(오프라인 저장·미리 받기 금지, 캐시 헤더 준수)에 따라
     여기서 따로 저장하지 않고 브라우저 기본 HTTP 캐시에만 맡김
   - 바탕 지도는 Mac(중계 서버)에 저장한 벡터 지도(/maps)를 우선 사용 */
const LIB="rr-lib-v2";
self.addEventListener("install",e=>self.skipWaiting());
self.addEventListener("activate",e=>e.waitUntil((async()=>{for(const k of await caches.keys())if(k!==LIB)await caches.delete(k);await self.clients.claim();})()));
self.addEventListener("fetch",e=>{const u=e.request.url;if(e.request.method!=="GET")return;
  if(u.startsWith("https://cdnjs.cloudflare.com/ajax/libs/leaflet/")||u.startsWith("https://unpkg.com/protomaps-leaflet@")){
    e.respondWith(caches.open(LIB).then(async c=>{const hit=await c.match(e.request);if(hit)return hit;const r=await fetch(e.request);if(r.ok)c.put(e.request,r.clone());return r;}));}
});
