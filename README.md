# 火警守护 · FireWatch

A free, self-service satellite fire-alert system for Malaysian farms.
Farmers register their **mobile number + farms (GPS)** and choose an alert
**radius**; the system then watches NASA FIRMS hotspots and pushes alerts to
their **Telegram** (free) and optionally **WhatsApp**.

## What each file is

| File | Where it runs | Job |
|---|---|---|
| `index.html` | GitHub Pages (the PWA) | Farmer registers: name, mobile, multiple farms via GPS, radius. Taps "Connect Telegram". Also has manual WhatsApp/Telegram share. |
| `bot-worker.js` | Cloudflare Workers (free) | The Telegram bot + `/register` endpoint. Writes each farmer into `roster.json` in your GitHub repo. |
| `roster.json` | Your GitHub repo | The openable farmer roster (phone, name, chat_id, farms). Created automatically. |
| `alerter.py` | GitHub Actions cron | Every 30 min: reads `roster.json`, checks NASA per farm, pushes alerts to each farmer's Telegram/WhatsApp. |
| `firewatch-alerter.yml` | `.github/workflows/` | The cron schedule for `alerter.py`. |

## How a farmer registers (their view)

1. Open the app → **My farms** tab → enter name + mobile (e.g. +60193824740).
2. **Add a farm**: tap "Use my location" (auto GPS) or type coordinates, set radius (km). Add as many farms as they want.
3. **Alerts** tab → **Connect Telegram** → Telegram opens → press **Start**.
4. Done. Alerts now arrive automatically, even with the app closed.

The number is used to (a) link their Telegram on Start and (b) pre-fill the
WhatsApp registration message. **Important honesty note:** a phone number
alone cannot receive auto-messages on Telegram or WhatsApp — both require the
farmer to opt in first (the Start tap). The app handles this for them.

---

## Admin setup (you, once)

### 1. NASA FIRMS key
Get a free MAP_KEY at https://firms.modaps.eosdis.nasa.gov/api/area/

### 2. Telegram bot
@BotFather → `/newbot` → save the **bot token** and **bot username**.

### 3. Two GitHub repos (or one repo, two purposes)
- **Pages repo**: host `index.html`. In its code, set at the top:
  - `CFG.TG_BOT` = your bot username (no @)
  - `CFG.ADMIN_WA` = your WhatsApp number, intl, no + (e.g. 60193824740)
  - `CFG.REGISTER_URL` = your Worker URL + `/register` (filled after step 4)
- **Roster repo**: holds `roster.json`, `alerter.py`, and the workflow.

### 4. Cloudflare Worker (the bot)
- Create a Worker, paste `bot-worker.js`.
- Set secrets (Worker → Settings → Variables):
  - `BOT_TOKEN`, `GH_TOKEN` (fine-grained, Contents read+write on roster repo),
    `GH_REPO` (e.g. `you/firewatch-roster`), `GH_FILE` (`roster.json`).
- Register the webhook (once):
  `https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<your-worker>.workers.dev`
- Copy the Worker URL back into `index.html` `CFG.REGISTER_URL` (+ `/register`).

### 5. GitHub Actions cron (the alerter)
- Put `alerter.py` and `.github/workflows/firewatch-alerter.yml` in the roster repo.
- Repo Settings → Secrets → Actions:
  - `NASA_MAP_KEY`, `TG_BOT_TOKEN` (required)
  - `WA_TOKEN`, `WA_PHONE_ID` (optional, for WhatsApp auto-push)
- Done. Runs every 30 min; alerts each registered farmer.

---

## WhatsApp auto-push (optional)
Telegram auto-push is free and instant. WhatsApp auto-push needs Meta's
Cloud API (Business account, phone number id, token) and may incur small fees
outside the 24-hour window. **Recommended:** Telegram for automatic alerts,
WhatsApp button for manual forwarding into farm groups. Both are built in.

## Tuning
- `MIN_LEVEL` in `alerter.py`: LOW / MEDIUM / HIGH / CRITICAL.
- Risk: `<2km` CRITICAL · `<5km` HIGH · `<10km` MEDIUM · else LOW.
- Cron is `*/30`. FIRMS updates a few times daily; don't go below 15 min.

## Data note
NASA FIRMS / VIIRS hotspots are near-real-time thermal detections from
satellites passing a few times daily — indicative, not continuous. A fire
between overpasses won't appear until the next pass. Always confirm on the
ground; this is a warning layer, not a replacement for patrols.
