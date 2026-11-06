# MoveWorld Live — the GitHub-hosted backend

This repository **is** the live backend of the
[MoveWorld](https://github.com/psfaruk/Movement) app. No VPS, no server to
run: GitHub itself acts as the computer —

| Server role | GitHub piece |
| --- | --- |
| Database | this repo (`data/pings/<device_id>.json`, one file per device) |
| App server | GitHub Actions (`.github/workflows/aggregate.yml`) |
| Read API | `raw` GitHub Contents API on `data/locations.json` |
| Write API | GitHub Contents API `PUT data/pings/<device_id>.json` |

## How it works

```
 MoveWorld app                github.com/psfaruk/moveworld-live
 ─────────────                ─────────────────────────────────
 share ON  ── PUT ping ─────▶  data/pings/<device_id>.json
                                     │  push triggers
                                     ▼
                             GitHub Actions aggregator
                             (merge · prune >5 min old · tidy)
                                     │  commit
                                     ▼
 map view  ◀─ GET poll ──────  data/locations.json
```

- **Write** — while a user has sharing enabled, the app writes its latest
  GPS fix to its own ping file roughly every 45 s / 20 m of movement
  (whichever comes first). Each device touches only its own file, so
  concurrent writers never conflict.
- **Aggregate** — every ping push starts the aggregator (fast, superseded
  runs are cancelled; a scheduled run sweeps every 15 minutes). It merges
  all still-fresh pings into `data/locations.json` and **deletes ping files
  older than 5 minutes**, so shared positions remove themselves.
- **Read** — every app polls `data/locations.json` (15 s with a token,
  ~90 s anonymous read-only). End-to-end latency is typically 30–60 s —
  fine for a live map of people, not a real-time game.

## Wire formats

Ping file `data/pings/<device_id>.json` (written by the app):

```json
{"v":1,"id":"<32-hex>","name":"Faruk","lat":52.52,"lon":13.40,
 "acc":12.5,"hdg":45.0,"spd":1.2,"src":"live_gps",
 "t":1730000000500,"sent":1730000000900}
```

Aggregate `data/locations.json` (written by the aggregator only):

```json
{"v":1,"updated_at":1730000000900,"ttl_s":300,
 "users":[{"user_id":"<32-hex>","display_name":"Faruk","lat":52.52,
           "lon":13.40,"accuracy_m":12.5,"heading_deg":45.0,
           "speed_mps":1.2,"source":"live_gps",
           "recorded_at":1730000000.5}]}
```

The `users` entries deliberately match the shape of the FastAPI WebSocket
feed (`user_id`, `display_name`, `lat`, `lon`, `accuracy_m`, `heading_deg`,
`speed_mps`, `source`, `recorded_at` in seconds) — the app reuses one code
path for both backends.

## Sharing a position (app side)

1. Install MoveWorld and open **Settings → Live sharing**.
2. Create a **fine-grained** personal access token at
   <https://github.com/settings/personal-access-tokens/new>:
   - Repository access → *Only select repositories* → `psfaruk/moveworld-live`
   - Permissions → *Contents* → **Read and write**
3. Paste the token into the app, choose a display name, turn sharing on.

The token is stored only on the device and can only write ping files in
this repository. It can never touch your code, releases or other repos.

## Privacy

- Only people who explicitly opt in appear here; the app never publishes
  without consent (sharing is OFF by default).
- Data is minimal: a random device id, a display name and the latest
  coordinate. No email, no history — one file per device, overwritten in
  place, **auto-deleted within 5 minutes** of the last ping.
- This repository is public so anonymous app installs can read the map
  without a token. Do not opt in if you do not want your (approximate,
  delay-lagged) position to be publicly visible during testing.
- "Remove my data" in the app deletes the ping file immediately.

## Scale & limits (demo-grade by design)

- ~1 read per 15 s per device (5,000–15,000/h per token — dozens of
  concurrent testers are fine; the anonymous viewer fallback is 60/h per IP).
- Update latency 30–60 s; presence TTL 5 min.
- For real-time scale (thousands of users, sub-second updates) MoveWorld
  ships a FastAPI + PostGIS + Redis backend in the main repo — deploy that
  when you outgrow GitHub.
