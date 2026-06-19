#!/usr/bin/env python3
"""
FireWatch alerter  —  runs on a free GitHub Actions cron.

Reads the farmer roster (roster.json, maintained by the registration bot),
checks NASA FIRMS hotspots near each farmer's farms, and pushes alerts to
their Telegram chat_id. Optionally also pushes via WhatsApp Cloud API.

roster.json format (written by bot-worker.js):
[
  {
    "phone": "60193824740",
    "name": "Stanley",
    "chat_id": 123456789,
    "farms": [{"name":"...", "lat":1.55, "lon":110.36, "rad":30}]
  }
]

---- Environment variables (GitHub repo secrets) ----
  NASA_MAP_KEY   NASA FIRMS map key                                  [required]
  TG_BOT_TOKEN   Telegram bot token (same bot as the registrar)      [required]
  WA_TOKEN       WhatsApp Cloud API token                            [optional]
  WA_PHONE_ID    WhatsApp Cloud API phone number id                  [optional]
  ROSTER_PATH    path to roster.json in the repo (default roster.json)
"""

import os, math, csv, io, json, urllib.request, urllib.parse, urllib.error

NASA_KEY = os.environ.get("NASA_MAP_KEY", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
WA_TOKEN = os.environ.get("WA_TOKEN", "")
WA_PHONE = os.environ.get("WA_PHONE_ID", "")
ROSTER_PATH = os.environ.get("ROSTER_PATH", "roster.json")

# Only alert at or above this level: LOW / MEDIUM / HIGH / CRITICAL
MIN_LEVEL = "MEDIUM"

LEVELS = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def haversine(la1, lo1, la2, lo2):
    R, p = 6371.0, math.pi / 180
    a = (math.sin((la2-la1)*p/2)**2
         + math.cos(la1*p)*math.cos(la2*p)*math.sin((lo2-lo1)*p/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def bearing_deg(la1, lo1, la2, lo2):
    p = math.pi / 180
    y = math.sin((lo2-lo1)*p) * math.cos(la2*p)
    x = (math.cos(la1*p)*math.sin(la2*p)
         - math.sin(la1*p)*math.cos(la2*p)*math.cos((lo2-lo1)*p))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def bearing(la1, lo1, la2, lo2):
    return DIRS[round(bearing_deg(la1, lo1, la2, lo2)/45) % 8]


def wind_toward(br_deg, wind_from):
    if wind_from is None:
        return False
    diff = abs(((br_deg - wind_from + 540) % 360) - 180)
    return diff <= 50


def risk(d, frp=None, toward=False):
    lvl = 3 if d < 2 else 2 if d < 5 else 1 if d < 10 else 0
    if frp is not None:
        if frp >= 100:
            lvl += 1
        elif frp >= 50 and lvl < 3:
            lvl += 1
    if toward:
        lvl += 1
    lvl = min(lvl, 3)
    return ["LOW", "MEDIUM", "HIGH", "CRITICAL"][lvl]


def fetch_wind(farm):
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={farm['lat']}&longitude={farm['lon']}"
           "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m&windspeed_unit=kmh")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        c = data.get("current", {})
        return {"spd": c.get("wind_speed_10m"), "dir": c.get("wind_direction_10m"),
                "gust": c.get("wind_gusts_10m")}
    except Exception:
        return None


def fetch_hotspots(farm):
    buf = (farm["rad"] + 10) / 111.0
    box = "{:.3f},{:.3f},{:.3f},{:.3f}".format(
        farm["lon"]-buf, farm["lat"]-buf, farm["lon"]+buf, farm["lat"]+buf)
    url = ("https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
           f"{NASA_KEY}/VIIRS_SNPP_NRT/{box}/1")
    with urllib.request.urlopen(url, timeout=60) as r:
        text = r.read().decode("utf-8", "replace")
    if "Invalid" in text or text.strip() == "":
        return [], None
    wind = fetch_wind(farm)
    wdir = wind["dir"] if wind else None
    rows = list(csv.DictReader(io.StringIO(text)))
    hits = []
    for row in rows:
        try:
            la, lo = float(row["latitude"]), float(row["longitude"])
        except (KeyError, ValueError):
            continue
        d = haversine(farm["lat"], farm["lon"], la, lo)
        if d <= farm["rad"]:
            br_deg = bearing_deg(farm["lat"], farm["lon"], la, lo)
            try:
                frp = float(row.get("frp", ""))
            except (ValueError, TypeError):
                frp = None
            toward = wind_toward(br_deg, wdir)
            hits.append({
                "d": d, "br": DIRS[round(br_deg/45) % 8],
                "frp": frp, "toward": toward,
                "rk": risk(d, frp, toward),
                "date": row.get("acq_date", ""), "time": row.get("acq_time", ""),
            })
    hits.sort(key=lambda h: h["d"])
    return hits, wind


def send_telegram(chat_id, text):
    if not (TG_TOKEN and chat_id):
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30)
        print(f"  Telegram -> {chat_id} sent")
    except urllib.error.URLError as e:
        print(f"  Telegram -> {chat_id} failed: {e}")


def send_whatsapp(phone, text):
    if not (WA_TOKEN and WA_PHONE and phone):
        return
    url = f"https://graph.facebook.com/v21.0/{WA_PHONE}/messages"
    payload = json.dumps({
        "messaging_product": "whatsapp", "to": phone,
        "type": "text", "text": {"body": text},
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", "Bearer " + WA_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=30)
        print(f"  WhatsApp -> {phone} sent")
    except urllib.error.URLError as e:
        print(f"  WhatsApp -> {phone} failed: {e}")


def load_roster():
    try:
        with open(ROSTER_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"No roster at {ROSTER_PATH}")
        return []


def alert_text(farm, hits, worst, wind):
    n = hits[0]
    lines = [f"🔥 火警通报 FIRE ALERT · {farm['name']}",
             "",
             f"NASA 检测到 {len(hits)} 个火点 / {len(hits)} hotspot(s)",
             f"最近 Nearest: {n['d']:.1f} km ({n['br']})"]
    if n.get("frp") is not None:
        if n["frp"] >= 100:
            word = "猛烈 intense"
        elif n["frp"] >= 50:
            word = "较强 strong"
        else:
            word = "小火 small"
        lines.append(f"强度 Intensity: {word} ({round(n['frp'])}MW)")
    if wind and wind.get("dir") is not None:
        gust = f", gust {round(wind['gust'])}" if wind.get("gust") is not None else ""
        lines.append(f"风 Wind: {round(wind['spd'])} km/h from {DIRS[round(wind['dir']/45)%8]}{gust}")
    if any(h.get("toward") for h in hits):
        lines.append("⚠️ 有火势正吹向农场 / Fire(s) blowing toward the farm")
    lines += [f"风险 Level: {worst}",
              f"时间 Time: {n['date']} {str(n['time']).zfill(4)} UTC",
              "",
              "来源 Source: NASA VIIRS",
              "请尽快巡查 / Inspect the perimeter ASAP."]
    return "\n".join(lines)


def main():
    if not NASA_KEY:
        print("NASA_MAP_KEY missing"); return
    roster = load_roster()
    if not roster:
        print("Roster empty — nobody registered yet."); return
    threshold = LEVELS[MIN_LEVEL]

    # cache hotspot lookups so two farmers near the same spot don't double-fetch
    for person in roster:
        chat_id = person.get("chat_id")
        phone = person.get("phone", "")
        name = person.get("name", phone)
        for farm in person.get("farms", []):
            try:
                hits, wind = fetch_hotspots(farm)
            except Exception as e:
                print(f"{name}/{farm.get('name')}: fetch error {e}")
                continue
            worst = max((h["rk"] for h in hits),
                        key=lambda r: LEVELS[r], default=None)
            if not hits or worst is None or LEVELS[worst] < threshold:
                print(f"{name}/{farm['name']}: {len(hits)} hotspot(s), below level")
                continue
            msg = alert_text(farm, hits, worst, wind)
            print(f"{name}/{farm['name']}: ALERT {worst}")
            send_telegram(chat_id, msg)
            send_whatsapp(phone, msg)


if __name__ == "__main__":
    main()
