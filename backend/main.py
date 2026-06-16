"""
SloEmergencyHub v5 – Live Fire & Ambulance Dashboard
Proxy backend for FireApp (fireapp.eu) public + authenticated APIs.

DISCOVERY: running.php?P1=<id> is PUBLIC and returns crew presence for
ALL unit types (PGD fire, GZ, NMP, CZ…) – 1,266+ units across Slovenia.
No login required for GPS map + crew data.

Active alarm/intervention data (alarmMulti) is push-only (FCM) and
gated behind account auth. Login unlocks /API/uporabnikNew.php etc.
"""
import asyncio, hashlib, json, logging, os, random, re, time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hub")

FA   = "https://fireapp.eu"
PORT = int(os.environ.get("PORT", "8080"))

_USER_AGENTS = [
    "FireApp/515 (Android 13; Samsung SM-G991B)",
    "FireApp/506 (Android 12; Xiaomi 2201123G)",
    "FireApp/421 (Android 11; OnePlus LE2113)",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
]

ORG_COLORS = {
    "PGD":"#e63946","GZ":"#ff6b35","CZ":"#f4a261",
    "NMP":"#2196f3","AED":"#4caf50","PPO":"#9c27b0",
    "PIGD":"#795548","OTHER":"#9e9e9e",
}

_cache: Dict[str, Any] = {
    "orgs":       [],      # all 1022 GPS positions
    "orgs_ts":    0.0,
    "crew":       {},      # id → crew data  (refreshed background)
    "crew_ts":    0.0,
    "crew_scan":  False,   # scan in progress?
    "gps_tracks": {},
}
_ws_clients: List[WebSocket] = []

# auth state (optional – unlocks additional endpoints)
_auth: Dict[str, Any] = {
    "phone":    os.environ.get("FIREAPP_PHONE",""),
    "password": os.environ.get("FIREAPP_PASSWORD",""),
    "imei":     "",
    "cookies":  {},
    "logged_in":  False,
    "last_login": 0.0,
    "user_info":  {},
}

def _ua() -> str: return random.choice(_USER_AGENTS)

def _device_id() -> str:
    seed = os.environ.get("FIREAPP_DEVICE_SEED","slo-emergency-hub-v5")
    return hashlib.md5(seed.encode()).hexdigest()[:16].upper()

def _client(timeout: float = 8.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent":_ua(),"Accept":"application/json, text/plain, */*","Accept-Language":"sl-SI"},
        cookies=_auth["cookies"],
        timeout=timeout, follow_redirects=True, verify=True,
    )

# ── FireApp auth ───────────────────────────────────────────────────────────────

async def _fa_login(phone: str="", password: str="", force: bool=False) -> Dict[str, Any]:
    global _auth
    phone    = phone    or _auth["phone"]
    password = password or _auth["password"]
    if not phone or not password:
        return {"logged_in":False,"error":"No credentials"}
    if not force and _auth["logged_in"] and (time.time()-_auth["last_login"]) < 3600:
        return {"logged_in":True,"source":"cache"}
    body = f"phone={phone}&password={password}&SDKver=33&device={_device_id()}&OStype=android&fcm={_auth.get('fcm','disabled')}"
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as c:
            r = await c.post(f"{FA}/API/login.php", content=body,
                             headers={"Content-Type":"application/x-www-form-urlencoded","User-Agent":"FireApp/515"})
        resp = r.json()
        if resp.get("status") == "success":
            raw  = json.loads(resp["response"]) if isinstance(resp.get("response"),str) else resp.get("response",{})
            data = json.loads(raw["data"])       if isinstance(raw.get("data"),str)      else raw.get("data",{})
            _auth.update({"phone":phone,"password":password,"imei":data.get("auth",""),
                          "cookies":dict(r.cookies),"logged_in":True,"last_login":time.time(),
                          "user_info":{k:data.get(k) for k in ("operativni","gzModul","freeUser") if k in data}})
            log.info("FA login OK imei=%s…", _auth["imei"][:4])
            return {"logged_in":True,"user_info":_auth["user_info"]}
        _auth["logged_in"]=False
        return {"logged_in":False,"error":resp.get("data",str(resp))}
    except Exception as e:
        _auth["logged_in"]=False
        return {"logged_in":False,"error":str(e)}

# ── HTTP helper ────────────────────────────────────────────────────────────────

async def _get(url: str, params=None, retries: int=3, as_text: bool=False) -> Any:
    delay=1.0
    for attempt in range(retries):
        try:
            async with _client() as c:
                r=await c.get(url,params=params); r.raise_for_status()
                return r.text if as_text else r.json()
        except (httpx.HTTPStatusError,httpx.ConnectError,httpx.TimeoutException) as e:
            log.warning("GET %s #%d: %s", url, attempt+1, e)
            if attempt<retries-1: await asyncio.sleep(delay+random.uniform(0,.5)); delay*=2
        except Exception as e: log.error("GET %s: %s", url, e); break
    return None

# ── Parsers ────────────────────────────────────────────────────────────────────

def _classify(name: str) -> str:
    n = name.upper()
    if "NMP" in n: return "NMP"
    if "AED" in n: return "AED"
    if ("ZD " in n or n.startswith("ZD")) and "PGD" not in n: return "NMP"
    if n.startswith("GZ ") or " GZ " in n: return "GZ"
    if n.startswith("CZ ") or "CIVILNA" in n: return "CZ"
    if "PIGD" in n: return "PIGD"
    if n.startswith("PPO "): return "PPO"
    if "PGD" in n: return "PGD"
    return "OTHER"

def _parse_karta(raw: str) -> List[Dict]:
    text = raw or ""
    m = re.search(r'eqfeed_callback\((\{.*\})\);?\s*$', text, re.DOTALL)
    if m:
        try:
            data=json.loads(m.group(1)); out=[]
            for it in data.get("features",[]):
                try:
                    geom=it.get("geometry") or {}; coords=geom.get("coordinates",[0,0])
                    lon=float(coords[0]) if len(coords)>0 else 0
                    lat=float(coords[1]) if len(coords)>1 else 0
                    if lat==0 and lon==0: continue
                    props=it.get("properties") or {}; name=props.get("name") or "?"; uid=int(it.get("id") or 0)
                    typ=_classify(name)
                    out.append({"id":uid,"name":name,"lat":lat,"lon":lon,"type":typ,"color":ORG_COLORS.get(typ,"#9e9e9e")})
                except Exception: continue
            return out
        except Exception: pass
    m=re.search(r"\[.*\]",text,re.DOTALL)
    if not m: return []
    try: items=json.loads(m.group())
    except Exception: return []
    out=[]
    for it in items:
        try:
            lat=float(it.get("lat") or it.get("latitude") or 0)
            lon=float(it.get("lng") or it.get("lon") or 0)
            if lat==0 and lon==0: continue
            name=it.get("naziv") or it.get("name") or "?"; uid=int(it.get("sifra") or it.get("id") or 0)
            typ=_classify(name)
            out.append({"id":uid,"name":name,"lat":lat,"lon":lon,"type":typ,"color":ORG_COLORS.get(typ,"#9e9e9e")})
        except Exception: pass
    return out

def _parse_crew(raw: Any, org_id: int=0) -> Dict[str, Any]:
    """Parse running.php response into crew summary."""
    if not isinstance(raw, dict) or not raw:
        return None
    now=datetime.now(timezone.utc); members=[]; online_count=0
    for slot, v in raw.items():
        if not isinstance(v,dict): continue
        ls=v.get("lastSeen","")
        try:
            last_dt=datetime.strptime(ls,"%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)-timedelta(hours=2)
            min_ago=max(0,int((now-last_dt).total_seconds()/60))
            online=(min_ago<=30)
        except Exception: min_ago=None; online=False
        if online: online_count+=1
        members.append({"slot":slot,"name":v.get("ime","?"),"app_version":v.get("verzija"),
                        "last_seen":ls,"minutes_ago":min_ago,"online":online})
    members.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
    return {"id":org_id,"total_members":len(members),"online_count":online_count,"crew":members}

async def _broadcast(msg: dict) -> None:
    dead=[]
    for ws in _ws_clients:
        try: await ws.send_json(msg)
        except Exception: dead.append(ws)
    for ws in dead:
        if ws in _ws_clients: _ws_clients.remove(ws)

# ── Background crew scanner ────────────────────────────────────────────────────

async def _scan_crew_for_orgs(org_ids: List[int], chunk=50) -> None:
    """Async scan running.php for a list of org IDs, updating _cache['crew']."""
    _cache["crew_scan"] = True
    updated = 0
    for i in range(0, len(org_ids), chunk):
        ids = org_ids[i:i+chunk]
        async with _client(timeout=6) as c:
            tasks = [c.get(f"{FA}/API/running.php", params={"P1":oid}) for oid in ids]
            resps = await asyncio.gather(*tasks, return_exceptions=True)
        for oid, r in zip(ids, resps):
            if isinstance(r, Exception): continue
            try:
                data = r.json()
                parsed = _parse_crew(data, oid)
                if parsed:
                    _cache["crew"][oid] = parsed
                    updated += 1
            except Exception: pass
        await asyncio.sleep(0.3)
    _cache["crew_ts"]   = time.time()
    _cache["crew_scan"] = False
    log.info("Crew scan done: %d units with crew data", updated)
    tot_online = sum(v["online_count"] for v in _cache["crew"].values())
    await _broadcast({"event":"crew_update","units_with_crew":updated,"total_online":tot_online,
                      "ts":datetime.now(timezone.utc).isoformat()})

# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="SloEmergencyHub", version="5.0", docs_url="/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── GPS Org map ────────────────────────────────────────────────────────────────

@app.get("/api/orgs")
async def api_orgs(fresh: bool=Query(False), background_tasks: BackgroundTasks=None):
    age=time.time()-_cache["orgs_ts"]
    if _cache["orgs"] and age<30 and not fresh:
        return {"orgs":_cache["orgs"],"total":len(_cache["orgs"]),"cached":True,"age_s":int(age),"ts":datetime.now(timezone.utc).isoformat()}
    raw=await _get(f"{FA}/API/kartaUporabnikov.php", as_text=True)
    orgs=_parse_karta(raw)
    if orgs:
        _cache["orgs"]=orgs; _cache["orgs_ts"]=time.time()
        # trigger background crew scan for all newly loaded org IDs
        if background_tasks and (not _cache["crew"] or fresh):
            ids=[o["id"] for o in orgs if o["id"]>0]
            background_tasks.add_task(_scan_crew_for_orgs, ids)
    return {"orgs":orgs or _cache["orgs"],"total":len(orgs or _cache["orgs"]),"cached":False,"ts":datetime.now(timezone.utc).isoformat()}

# ── Crew data ─────────────────────────────────────────────────────────────────

@app.get("/api/crew")
async def api_crew(
    type_filter: Optional[str] = Query(None, description="Filter by org type: PGD,GZ,NMP,AED,CZ,PPO"),
    min_online:  int           = Query(0,    description="Minimum online members"),
    limit:       int           = Query(500,  ge=1, le=2000),
):
    """
    Return crew presence data for all units that have crew data cached.
    Backed by running.php (public endpoint) – no auth required.
    'online' = last app heartbeat within 30 minutes.
    """
    # Merge with org info
    org_map={o["id"]:o for o in _cache["orgs"]}
    results=[]
    for oid, crew_data in _cache["crew"].items():
        if crew_data["online_count"] < min_online: continue
        org=org_map.get(oid,{})
        typ=org.get("type","OTHER")
        if type_filter and typ not in type_filter.upper().split(","): continue
        results.append({
            "id":oid,
            "name":org.get("name","?"),
            "lat":org.get("lat"),
            "lon":org.get("lon"),
            "type":typ,
            "color":org.get("color",ORG_COLORS.get(typ,"#9e9e9e")),
            "total_members":crew_data["total_members"],
            "online_count":crew_data["online_count"],
            "crew":crew_data["crew"],
        })
    results.sort(key=lambda x:-x["online_count"])
    tot_online=sum(r["online_count"] for r in results)
    return {"ts":datetime.now(timezone.utc).isoformat(),
            "total_units":len(results),"total_online":tot_online,
            "scan_age_s":int(time.time()-_cache["crew_ts"]) if _cache["crew_ts"] else None,
            "scanning":_cache["crew_scan"],
            "units":results[:limit]}

@app.get("/api/crew/scan")
async def api_crew_scan(background_tasks: BackgroundTasks, ids: Optional[str]=Query(None)):
    """Trigger background crew scan. Provide comma-separated IDs or leave blank for all orgs."""
    if _cache["crew_scan"]:
        return {"status":"already_scanning"}
    if ids:
        id_list=[int(x) for x in ids.split(",") if x.strip().isdigit()]
    else:
        id_list=[o["id"] for o in _cache["orgs"] if o["id"]>0]
        if not id_list:
            return {"status":"no_orgs_cached","hint":"Call /api/orgs first"}
    background_tasks.add_task(_scan_crew_for_orgs, id_list)
    return {"status":"scanning_started","ids_count":len(id_list)}

# ── Per-unit endpoints ─────────────────────────────────────────────────────────

@app.get("/api/unit/{unit_id}/running")
async def api_unit_running(unit_id: int):
    raw=await _get(f"{FA}/API/running.php",{"P1":unit_id})
    if raw is None: raise HTTPException(502,"Upstream timeout")
    ts=datetime.now(timezone.utc).isoformat()
    if isinstance(raw,list): return {"unit_id":unit_id,"type":"fire","active":len(raw)>0,"interventions":raw,"ts":ts}
    if isinstance(raw,dict):
        crew_data=_parse_crew(raw,unit_id)
        _cache["crew"][unit_id]=crew_data
        return {"unit_id":unit_id,"type":"crew","crew_data":crew_data,"ts":ts}
    return {"unit_id":unit_id,"raw":raw}

@app.get("/api/unit/{unit_id}/check")
async def api_unit_check(unit_id: int):
    raw=await _get(f"{FA}/API/preveriIntervencijo.php",{"P1":unit_id})
    if raw is None: raise HTTPException(502,"Upstream timeout")
    active=isinstance(raw,dict) and not raw.get("error",True) and len(raw)>1
    return {"unit_id":unit_id,"active":active,"raw":raw,"ts":datetime.now(timezone.utc).isoformat()}

@app.get("/api/unit/{unit_id}/lastseen")
async def api_unit_lastseen(unit_id: int):
    return {"unit_id":unit_id,"data":await _get(f"{FA}/API/lastSeen.php",{"P1":unit_id}),"ts":datetime.now(timezone.utc).isoformat()}

@app.get("/api/unit/{unit_id}/address")
async def api_unit_address(unit_id: int):
    raw=await _get(f"{FA}/API/novNaslov.php",{"P1":unit_id})
    rawg=await _get(f"{FA}/API/novNaslovGZ.php",{"P1":unit_id},as_text=True)
    return {"unit_id":unit_id,"address":raw,"gz_address":rawg,"ts":datetime.now(timezone.utc).isoformat()}

@app.get("/api/unit/{unit_id}/members")
async def api_unit_members(unit_id: int):
    raw=await _get(f"{FA}/API/seznamClanov.php",{"dID":unit_id})
    members=[str(x) for x in raw if x] if isinstance(raw,list) else []
    return {"unit_id":unit_id,"members":members,"count":len(members),"ts":datetime.now(timezone.utc).isoformat()}

@app.get("/api/unit/{unit_id}/vehicles")
async def api_unit_vehicles(unit_id: int):
    raw=await _get(f"{FA}/API/vozilaStatus.php",{"uID":unit_id})
    return {"unit_id":unit_id,"vehicles":raw if isinstance(raw,list) else [],"ts":datetime.now(timezone.utc).isoformat()}

@app.get("/api/scan")
async def api_scan(start: int=Query(0,ge=0,le=3100), end: int=Query(200,ge=1,le=3100)):
    if end-start>500: raise HTTPException(400,"Max 500")
    active=[]
    for uid in range(start,end):
        raw=await _get(f"{FA}/API/running.php",{"P1":uid},retries=1)
        if isinstance(raw,list) and len(raw)>0:
            active.append({"unit_id":uid,"interventions":raw})
            await _broadcast({"event":"intervention_found","unit_id":uid,"ts":datetime.now(timezone.utc).isoformat()})
        elif isinstance(raw,dict) and len(raw)>0:
            crew=_parse_crew(raw,uid)
            _cache["crew"][uid]=crew
            if crew["online_count"]>0: active.append({"unit_id":uid,"type":"crew_online","crew":crew})
        await asyncio.sleep(0.3+random.uniform(0,.15))
    return {"scanned":end-start,"active_count":len(active),"active":active,"ts":datetime.now(timezone.utc).isoformat()}

# ── GPS tracks ─────────────────────────────────────────────────────────────────

@app.post("/api/gps/{unit_id}")
async def api_gps(unit_id:int,lat:float=Query(...),lon:float=Query(...),speed:float=Query(0.0),bearing:float=Query(0.0),acc:float=Query(5.0),alt:float=Query(250.0)):
    async with _client() as c:
        r=await c.post(f"{FA}/API/vnosLokacije.php",params={"P1":unit_id},data={"lat":lat,"lon":lon,"speed":speed,"bearing":bearing,"acc":acc,"alt":alt})
    pt={"lat":lat,"lon":lon,"speed":speed,"bearing":bearing,"acc":acc,"alt":alt,"ts":datetime.now(timezone.utc).isoformat()}
    tr=_cache["gps_tracks"].setdefault(str(unit_id),[])
    tr.append(pt)
    if len(tr)>200: _cache["gps_tracks"][str(unit_id)]=tr[-200:]
    await _broadcast({"event":"gps_update","unit_id":unit_id,"point":pt})
    return {"unit_id":unit_id,"stored":True,"track_len":len(tr),"ts":pt["ts"]}

@app.get("/api/tracks")
async def api_tracks():
    return {"tracks":_cache["gps_tracks"],"units":list(_cache["gps_tracks"].keys()),"ts":datetime.now(timezone.utc).isoformat()}

# ── Auth ───────────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def api_auth_login(phone: str=Query(...), password: str=Query(...)):
    """Login to FireApp mobile API. Password = 8-char alphanumeric from SMS (not web PIN)."""
    result=await _fa_login(phone,password,force=True)
    if result.get("logged_in"):
        return {"status":"ok","logged_in":True,"user_info":result.get("user_info"),"imei_prefix":_auth["imei"][:4]+"****" if _auth["imei"] else None}
    raise HTTPException(401, result)

@app.get("/api/auth/status")
async def api_auth_status():
    return {"logged_in":_auth["logged_in"],"phone":_auth["phone"][:6]+"****" if _auth["phone"] else None,
            "last_login":datetime.fromtimestamp(_auth["last_login"],tz=timezone.utc).isoformat() if _auth["last_login"] else None,
            "user_info":_auth["user_info"],"imei_set":bool(_auth["imei"])}

@app.post("/api/auth/logout")
async def api_auth_logout():
    _auth.update({"imei":"","cookies":{},"logged_in":False,"last_login":0.0,"user_info":{}})
    return {"status":"ok","logged_out":True}

@app.post("/api/auth/refresh")
async def api_auth_refresh():
    return await _fa_login(force=True)

# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_ep(ws: WebSocket):
    await ws.accept(); _ws_clients.append(ws)
    try:
        await ws.send_json({"event":"connected","orgs_cached":len(_cache["orgs"]),"crew_units":len(_cache["crew"]),"ts":datetime.now(timezone.utc).isoformat()})
        while True:
            await asyncio.sleep(30)
            await ws.send_json({"event":"ping","ts":datetime.now(timezone.utc).isoformat()})
    except WebSocketDisconnect: pass
    finally:
        if ws in _ws_clients: _ws_clients.remove(ws)

# ── Health / static ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status":"ok","orgs_cached":len(_cache["orgs"]),"crew_units":len(_cache["crew"]),
            "total_online":sum(v["online_count"] for v in _cache["crew"].values()),
            "crew_scan_active":_cache["crew_scan"],"ws_clients":len(_ws_clients),
            "auth_logged_in":_auth["logged_in"],"ts":datetime.now(timezone.utc).isoformat()}

app.mount("/static",StaticFiles(directory=os.path.join(os.path.dirname(__file__),"..","frontend","static")),name="static")

@app.get("/",response_class=HTMLResponse,include_in_schema=False)
async def idx():
    p=os.path.normpath(os.path.join(os.path.dirname(__file__),"..","frontend","index.html"))
    if os.path.exists(p):
        with open(p,encoding="utf-8") as f: return f.read()
    return HTMLResponse("<h1>SloEmergencyHub v5</h1><a href='/docs'>API</a>")

@app.on_event("startup")
async def startup_event():
    if _auth["phone"] and _auth["password"]:
        log.info("Auto-login FireApp...")
        asyncio.create_task(_fa_login(force=True))

if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:app",host="0.0.0.0",port=PORT,reload=False)
