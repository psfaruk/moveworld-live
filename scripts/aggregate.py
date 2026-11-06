#!/usr/bin/env python3
"""MoveWorld live aggregator — the "server" of the GitHub-hosted backend.

GitHub is the computer:

    app  --PUT-->  data/pings/<device_id>.json   (one file per device)
    this script -> data/locations.json           (the read model apps poll)

Runs inside GitHub Actions (`.github/workflows/aggregate.yml`) on every push
that touches `data/pings/**` and on a 15-minute schedule. It merges every
still-fresh ping into the aggregate and deletes ping files older than the
presence TTL, so shared positions disappear by themselves.

Wire contract — ping file `data/pings/<device_id>.json`:
    {"v":1,"id":"<hex>","name":"Faruk","lat":52.52,"lon":13.40,
     "acc":12.5,"hdg":45.0,"spd":1.2,"src":"live_gps",
     "t":1730000000500,"sent":1730000000900}

Wire contract — aggregate `data/locations.json` (same user shape the FastAPI
WebSocket feed emits, so the app reuses one code path):
    {"v":1,"updated_at":<ms>,"ttl_s":300,
     "users":[{"user_id":"<hex>","display_name":"Faruk","lat":52.52,
               "lon":13.40,"accuracy_m":12.5,"heading_deg":45.0,
               "speed_mps":1.2,"source":"live_gps",
               "recorded_at":1730000000.5}]}

Untrusted-input handling: ping files are written by apps holding a scoped
token, but the aggregator still treats them as untrusted — it never executes
anything, validates every field, clamps/sanitizes names and silently drops
(or deletes) malformed files.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

TTL_MS = 300_000  # 5 minutes — presence lifetime after the last ping
MAX_NAME = 64
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")
VALID_SOURCES = {
    "live_gps",
    "delayed",
    "last_known",
    "scheduled_transport",
    "simulated",
    "unavailable",
}


def sanitize_name(raw: object) -> str:
    """Strip control chars, collapse whitespace, cap length. Never empty."""
    if not isinstance(raw, str):
        return "user"
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "user"
    return cleaned[:MAX_NAME]


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def parse_ping(path: Path, now_ms: int) -> tuple[dict | None, bool]:
    """Parse one ping file.

    Returns (user_entry, keep). ``keep=False`` means the file is junk or
    expired and should be deleted. ``user_entry`` is ``None`` when the file
    does not yield a renderable user (it may still be *kept* only never —
    expired/junk files are always deleted).
    """
    device_id = path.stem
    if not DEVICE_ID_RE.match(device_id):
        return None, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, False
    if not isinstance(data, dict):
        return None, False

    lat = _num(data.get("lat"))
    lon = _num(data.get("lon"))
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0):
        return None, False
    if not (-180.0 <= lon <= 180.0):
        return None, False

    t = _num(data.get("t"))
    sent = _num(data.get("sent"))
    if t is None:
        return None, False
    freshness = sent if sent is not None else t
    if now_ms - freshness > TTL_MS:
        return None, False

    src = data.get("src")
    if src not in VALID_SOURCES:
        src = "live_gps"

    user: dict = {
        "user_id": device_id,
        "display_name": sanitize_name(data.get("name")),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "source": src,
        "recorded_at": round(t / 1000.0, 3),
    }
    acc = _num(data.get("acc"))
    if acc is not None and 0.0 <= acc <= 100_000.0:
        user["accuracy_m"] = round(acc, 1)
    hdg = _num(data.get("hdg"))
    if hdg is not None:
        user["heading_deg"] = round(hdg % 360.0, 1)
    spd = _num(data.get("spd"))
    if spd is not None and 0.0 <= spd <= 300.0:  # ~1080 km/h — junk filter
        user["speed_mps"] = round(spd, 2)
    return user, True


def aggregate(pings_dir: Path, aggregate_path: Path, now_ms: int) -> dict:
    """Merge pings into the aggregate; prune expired/junk ping files.

    The aggregate is rewritten (and ``updated_at`` bumped) only when the user
    list actually changed or files were pruned — quiet scheduled runs produce
    no commit.
    """
    users: list[dict] = []
    deleted: list[str] = []
    if pings_dir.is_dir():
        for path in sorted(pings_dir.glob("*.json")):
            user, keep = parse_ping(path, now_ms)
            if keep:
                users.append(user)
            else:
                path.unlink(missing_ok=True)
                deleted.append(path.name)
    users.sort(key=lambda u: (u["display_name"].lower(), u["user_id"]))

    old_users: list | None = None
    if aggregate_path.exists():
        try:
            old = json.loads(aggregate_path.read_text(encoding="utf-8"))
            old_users = old.get("users") if isinstance(old, dict) else None
        except Exception:
            old_users = None

    changed = old_users != users or bool(deleted)
    if changed:
        doc = {
            "v": 1,
            "updated_at": now_ms,
            "ttl_s": TTL_MS // 1000,
            "users": users,
        }
        aggregate_path.parent.mkdir(parents=True, exist_ok=True)
        aggregate_path.write_text(
            json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    return {
        "kept": len(users),
        "pruned": len(deleted),
        "pruned_files": deleted,
        "changed": changed,
    }


def selftest() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pings = root / "data" / "pings"
        pings.mkdir(parents=True)
        now = 1_730_000_000_000

        def write(name: str, doc: object) -> None:
            (pings / name).write_text(
                json.dumps(doc), encoding="utf-8"
            )

        # 1. Two valid, fresh pings.
        write(
            "a" * 32 + ".json",
            {
                "v": 1, "id": "a" * 32, "name": "  Faruk \x01\x02 ",
                "lat": 52.52, "lon": 13.40, "acc": 12.5, "hdg": 45.0,
                "spd": 1.2, "src": "live_gps", "t": now - 1000,
                "sent": now - 500,
            },
        )
        write(
            "b" * 32 + ".json",
            {
                "v": 1, "id": "b" * 32, "name": "Rahim",
                "lat": -33.87, "lon": 151.21, "src": "live_gps",
                "t": now - 2000, "sent": now - 1500,
            },
        )
        # 2. Expired ping (older than TTL) — must be pruned.
        write(
            "c" * 32 + ".json",
            {"v": 1, "id": "c" * 32, "name": "Old", "lat": 1.0, "lon": 2.0,
             "src": "live_gps", "t": now - TTL_MS - 60_000,
             "sent": now - TTL_MS - 60_000},
        )
        # 3. Junk: invalid device id (filename) — must be deleted.
        write("ZZZ.json", {"lat": 1.0, "lon": 2.0, "t": now})
        # 4. Junk: not JSON.
        write("d" * 32 + ".json", "{not json")
        # 5. Junk: lat out of range.
        write(
            "e" * 32 + ".json",
            {"v": 1, "name": "Bad", "lat": 123.0, "lon": 0.0,
             "t": now, "sent": now},
        )

        agg = root / "data" / "locations.json"
        s1 = aggregate(pings, agg, now)
        assert s1["kept"] == 2, s1
        assert s1["pruned"] == 4, s1
        assert s1["changed"] is True
        doc = json.loads(agg.read_text(encoding="utf-8"))
        assert [u["display_name"] for u in doc["users"]] == ["Faruk", "Rahim"], doc
        faruk = doc["users"][0]
        assert faruk["user_id"] == "a" * 32
        assert faruk["accuracy_m"] == 12.5 and faruk["heading_deg"] == 45.0
        assert faruk["speed_mps"] == 1.2
        assert abs(faruk["recorded_at"] - (now - 1000) / 1000.0) < 1e-6
        rahim = doc["users"][1]
        assert "accuracy_m" not in rahim and "heading_deg" not in rahim

        # 6. No change -> no rewrite, changed=False.
        s2 = aggregate(pings, agg, now)
        assert s2["changed"] is False, s2
        assert s2["kept"] == 2 and s2["pruned"] == 0

        # 7. A new ping arrives -> changed again, ordering by name stays.
        write(
            "f" * 32 + ".json",
            {"v": 1, "id": "f" * 32, "name": "Ayesha", "lat": 0.0,
             "lon": 0.0, "src": "live_gps", "t": now, "sent": now},
        )
        s3 = aggregate(pings, agg, now)
        assert s3["changed"] is True and s3["kept"] == 3
        doc3 = json.loads(agg.read_text(encoding="utf-8"))
        assert [u["display_name"] for u in doc3["users"]] == [
            "Ayesha", "Faruk", "Rahim",
        ]

        # 8. Long name is capped; empty name falls back to "user".
        write(
            "1" * 32 + ".json",
            {"v": 1, "name": "x" * 200, "lat": 1, "lon": 1,
             "t": now, "sent": now},
        )
        write(
            "2" * 32 + ".json",
            {"v": 1, "name": "   ", "lat": 2, "lon": 2, "t": now,
             "sent": now},
        )
        aggregate(pings, agg, now)
        doc4 = json.loads(agg.read_text(encoding="utf-8"))
        names = {u["user_id"]: u["display_name"] for u in doc4["users"]}
        assert len(names["1" * 32]) == 64, names
        assert names["2" * 32] == "user", names

    print("aggregate.py selftest: all checks passed")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    root = Path(__file__).resolve().parent.parent
    summary = aggregate(
        root / "data" / "pings",
        root / "data" / "locations.json",
        int(time.time() * 1000),
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
