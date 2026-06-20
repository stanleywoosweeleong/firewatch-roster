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


def risk(d, frp=None, toward=False, hum=None, dsr=None):
    lvl = 3 if d < 2 else 2 if d < 5 else 1 if d < 10 else 0
    if frp is not None:
        if frp >= 100:
            lvl += 1
        elif frp >= 50 and lvl < 3:
            lvl += 1
    if toward:
        lvl += 1
    # dry conditions make any fire more dangerous (matches the app)
    if (hum is not None and hum < 40) or (dsr is not None and dsr >= 5):
        lvl += 1
    lvl = min(lvl, 3)
    return ["LOW", "MEDIUM", "HIGH", "CRITICAL"][lvl]


def fetch_wind(farm):
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={farm['lat']}&longitude={farm['lon']}"
           "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m,relative_humidity_2m"
           "&daily=precipitation_sum&past_days=7&forecast_days=1"
           "&windspeed_unit=kmh&timezone=auto")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        c = data.get("current", {})
        # days since last rain (>=1mm) from the past-7-days daily precipitation
        dsr = None
        daily = data.get("daily", {})
        arr = daily.get("precipitation_sum")
        if isinstance(arr, list) and arr:
            dsr = 0
            for v in reversed(arr):
                if v is not None and v >= 1:
                    break
                dsr += 1
            if dsr > len(arr) - 1:
                dsr = len(arr) - 1
        return {"spd": c.get("wind_speed_10m"), "dir": c.get("wind_direction_10m"),
                "gust": c.get("wind_gusts_10m"),
                "hum": c.get("relative_humidity_2m"), "dsr": dsr}
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
    whum = wind["hum"] if wind else None
    wdsr = wind["dsr"] if wind else None
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
                "rk": risk(d, frp, toward, whum, wdsr),
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


# ---- Alert message translation tables ----
# Each builds the body in ONE language. The final message = chosen lang + Malay
# (de-duplicated if the chosen language IS Malay).
DIR_NAMES = {
    "zh": {"N":"北","NE":"东北","E":"东","SE":"东南","S":"南","SW":"西南","W":"西","NW":"西北"},
    "en": {"N":"N","NE":"NE","E":"E","SE":"SE","S":"S","SW":"SW","W":"W","NW":"NW"},
    "ms": {"N":"U","NE":"TL","E":"T","SE":"TG","S":"S","SW":"BD","W":"B","NW":"BL"},
    "ta": {"N":"வ","NE":"வகி","E":"கி","SE":"தெகி","S":"தெ","SW":"தெமே","W":"மே","NW":"வமே"},
}
RK_NAMES = {
    "zh": {"CRITICAL":"紧急","HIGH":"高","MEDIUM":"中","LOW":"低"},
    "en": {"CRITICAL":"CRITICAL","HIGH":"HIGH","MEDIUM":"MEDIUM","LOW":"LOW"},
    "ms": {"CRITICAL":"KRITIKAL","HIGH":"TINGGI","MEDIUM":"SEDERHANA","LOW":"RENDAH"},
    "ta": {"CRITICAL":"மிகுந்த","HIGH":"அதிக","MEDIUM":"நடுத்தர","LOW":"குறைந்த"},
}
TXT = {
  "zh": {
    "title":"🔥 火警通报 · {farm}", "hot":"NASA 检测到 {n} 个火点",
    "near":"最近: {d} 公里 ({br})", "intensity":"强度: {w} ({mw}MW)",
    "fL":"小火","fM":"较强","fH":"猛烈",
    "wind":"风: {s} 公里/小时 来自 {dir}{gust}", "gust":"，阵风 {g}",
    "hum":"湿度 {h}%", "rain":"{d} 天没下雨", "rain6":"6+ 天没下雨",
    "dry":"⚠️ 天气干燥，火险高", "toward":"⚠️ 有火势正吹向农场",
    "level":"风险: {r}", "time":"时间: {t} UTC",
    "src":"来源: NASA VIIRS", "act":"请尽快巡查。",
  },
  "en": {
    "title":"🔥 FIRE ALERT · {farm}", "hot":"NASA detected {n} hotspot(s)",
    "near":"Nearest: {d} km ({br})", "intensity":"Intensity: {w} ({mw}MW)",
    "fL":"small","fM":"strong","fH":"intense",
    "wind":"Wind: {s} km/h from {dir}{gust}", "gust":", gust {g}",
    "hum":"Humidity {h}%", "rain":"no rain {d}d", "rain6":"6+ days no rain",
    "dry":"⚠️ Dry conditions, high fire risk", "toward":"⚠️ Fire(s) blowing toward the farm",
    "level":"Level: {r}", "time":"Time: {t} UTC",
    "src":"Source: NASA VIIRS", "act":"Inspect the perimeter ASAP.",
  },
  "ms": {
    "title":"🔥 AMARAN KEBAKARAN · {farm}", "hot":"NASA kesan {n} titik panas",
    "near":"Terdekat: {d} km ({br})", "intensity":"Keamatan: {w} ({mw}MW)",
    "fL":"kecil","fM":"kuat","fH":"sengit",
    "wind":"Angin: {s} km/j dari {dir}{gust}", "gust":", tiupan {g}",
    "hum":"Kelembapan {h}%", "rain":"{d} hari tiada hujan", "rain6":"6+ hari tiada hujan",
    "dry":"⚠️ Cuaca kering, risiko tinggi", "toward":"⚠️ Api bertiup ke arah ladang",
    "level":"Tahap: {r}", "time":"Masa: {t} UTC",
    "src":"Sumber: NASA VIIRS", "act":"Periksa kawasan ladang segera.",
  },
  "ta": {
    "title":"🔥 தீ எச்சரிக்கை · {farm}", "hot":"NASA {n} தீப்புள்ளி கண்டறிந்தது",
    "near":"அருகில்: {d} கி.மீ ({br})", "intensity":"தீவிரம்: {w} ({mw}MW)",
    "fL":"சிறிய","fM":"வலுவான","fH":"கடுமையான",
    "wind":"காற்று: {s} கி.மீ/ம {dir} இருந்து{gust}", "gust":", பலத்த {g}",
    "hum":"ஈரப்பதம் {h}%", "rain":"{d} நாட்கள் மழை இல்லை", "rain6":"6+ நாட்கள் மழை இல்லை",
    "dry":"⚠️ வறண்ட வானிலை, அதிக தீ ஆபத்து", "toward":"⚠️ தீ பண்ணையை நோக்கி வீசுகிறது",
    "level":"நிலை: {r}", "time":"நேரம்: {t} UTC",
    "src":"ஆதாரம்: NASA VIIRS", "act":"பண்ணைப் பகுதியை உடனே சரிபார்க்கவும்.",
  },
}


def _alert_block(lang, farm, hits, worst, wind):
    """Build the alert body in a single language."""
    t = TXT[lang]; n = hits[0]
    br = DIR_NAMES[lang].get(n["br"], n["br"])
    lines = [t["title"].format(farm=farm["name"]),
             "",
             t["hot"].format(n=len(hits)),
             t["near"].format(d=f"{n['d']:.1f}", br=br)]
    if n.get("frp") is not None:
        w = t["fH"] if n["frp"] >= 100 else t["fM"] if n["frp"] >= 50 else t["fL"]
        lines.append(t["intensity"].format(w=w, mw=round(n["frp"])))
    if wind and wind.get("dir") is not None:
        gust = t["gust"].format(g=round(wind["gust"])) if wind.get("gust") is not None else ""
        wdir = DIR_NAMES[lang][DIRS[round(wind["dir"]/45) % 8]]
        lines.append(t["wind"].format(s=round(wind["spd"]), dir=wdir, gust=gust))
    if wind:
        wparts = []
        if wind.get("hum") is not None:
            wparts.append(t["hum"].format(h=round(wind["hum"])))
        if wind.get("dsr") is not None:
            wparts.append(t["rain6"] if wind["dsr"] >= 6 else t["rain"].format(d=wind["dsr"]))
        if wparts:
            lines.append(" · ".join(wparts))
        is_dry = (wind.get("hum") is not None and wind["hum"] < 40) or \
                 (wind.get("dsr") is not None and wind["dsr"] >= 5)
        if is_dry:
            lines.append(t["dry"])
    if any(h.get("toward") for h in hits):
        lines.append(t["toward"])
    lines += [t["level"].format(r=RK_NAMES[lang][worst]),
              t["time"].format(t=f"{n['date']} {str(n['time']).zfill(4)}"),
              "",
              t["src"],
              t["act"]]
    return "\n".join(lines)


def alert_text(farm, hits, worst, wind, lang="zh"):
    """Compose alert in the farmer's language + Malay (deduplicated)."""
    if lang not in TXT:
        lang = "zh"
    primary = _alert_block(lang, farm, hits, worst, wind)
    if lang == "ms":
        return primary  # already Malay; no need to repeat
    malay = _alert_block("ms", farm, hits, worst, wind)
    return primary + "\n\n———\n\n" + malay


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
    # cache hotspot lookups so two farmers near the same spot don't double-fetch
    for person in roster:
        chat_id = person.get("chat_id")
        phone = person.get("phone", "")
        name = person.get("name", phone)
        # old records have no "lang" -> default to Chinese (zh)
        plang = person.get("lang", "zh")
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
            msg = alert_text(farm, hits, worst, wind, plang)
            print(f"{name}/{farm['name']}: ALERT {worst} [{plang}]")
            send_telegram(chat_id, msg)
            send_whatsapp(phone, msg)


if __name__ == "__main__":
    main()
