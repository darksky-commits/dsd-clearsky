#!/usr/bin/env python3
"""
update_clearsky.py  (Dark Sky Dispatch, Clear Skies Tonight)

Builds clearsky.json, the data file the /clear-skies-tonight/ page reads.

Source : NOAA National Weather Service forecast API (api.weather.gov), public domain.
Needs  : Python 3.7+, standard library only. No API key.
Output : one compact JSON file, about 270 KB (60 to 80 KB gzipped by the web server).

Usage
    NWS_CONTACT="you@example.com" python3 update_clearsky.py
    python3 update_clearsky.py --out /home/USER/public_html/clear-skies-tonight/clearsky.json

NWS asks every client to identify itself. Set NWS_CONTACT to a website or email
you are happy to have in their logs. Nothing about you is ever written to the
output file.

How it works
    1. grid_points.json lists about 1,800 sample points across the lower 48.
    2. First run only: each point is looked up once (/points) to learn which NWS
       forecast cell it sits in and that cell's time zone. Saved to
       gridpoint_cache.json so later runs skip this step.
    3. Every run: each cell's hourly sky cover forecast is fetched, expanded to one
       value per hour, and packed into one base-64 character per hour (0 to 100%).
    4. Hours that have already passed are carried over from the previous
       clearsky.json, so after midnight the page can still show tonight's whole
       11 PM to 4 AM window.
    5. The file is written atomically. If too many points failed, the old file is
       left alone and the script exits with an error.
"""
import argparse, datetime as dt, gzip, json, os, random, re, sys, tempfile, threading, time
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
HOURS = 150          # hours of forecast kept, counted from t0
BACK = 12            # hours before "now" kept, so a night in progress stays whole
MIN_OK = 0.70        # refuse to publish if fewer than this share of points came back
DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")

_lock = threading.Lock()


def ua(contact):
    who = "https://darkskydispatch.com" + ("; " + contact if contact else "")
    return "DarkSkyDispatch-ClearSkies/1.0 (+%s)" % who


def fetch(url, headers, tries=5, timeout=30):
    """GET json. Returns None on 404. Raises after repeated failures."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
            wait = 2 ** i + random.random()
            if e.code in (429, 503):
                try:
                    wait = max(wait, float(e.headers.get("Retry-After", 0)))
                except ValueError:
                    pass
            time.sleep(min(wait, 60))
        except Exception as e:  # timeouts, resets, bad json
            last = e
            time.sleep(min(2 ** i + random.random(), 30))
    raise RuntimeError("%s -> %s" % (url, last))


def hours_in(duration):
    m = DUR.match(duration)
    if not m:
        return 1
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return max(1, d * 24 + h + (1 if mi else 0))


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def enc(v):
    v = 0 if v < 0 else 100 if v > 100 else v
    return B64[int(round(v * 63 / 100.0))]


def expand(values, t0):
    """NWS gives runs like {validTime:'2026-11-12T03:00:00+00:00/PT6H', value:40}.
    Return HOURS characters, '.' where nothing is known."""
    out = ["."] * HOURS
    for item in values or []:
        v = item.get("value")
        if v is None:
            continue
        start, _, dur = item["validTime"].partition("/")
        i0 = int((parse_time(start) - t0).total_seconds() // 3600)
        ch = enc(float(v))
        for k in range(hours_in(dur)):
            i = i0 + k
            if 0 <= i < HOURS:
                out[i] = ch
    return out


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "clearsky.json"))
    ap.add_argument("--grid", default=str(HERE / "grid_points.json"))
    ap.add_argument("--cache", default=str(HERE / "gridpoint_cache.json"))
    ap.add_argument("--base", default=os.environ.get("NWS_BASE", "https://api.weather.gov"))
    ap.add_argument("--workers", type=int, default=int(os.environ.get("NWS_WORKERS", "4")))
    ap.add_argument("--limit", type=int, default=0, help="only the first N points (testing)")
    a = ap.parse_args()

    base = a.base.rstrip("/")
    headers = {"User-Agent": ua(os.environ.get("NWS_CONTACT", "").strip()),
               "Accept": "application/geo+json", "Accept-Encoding": "gzip"}

    grid = json.loads(Path(a.grid).read_text())
    nodes = grid["nodes"][: a.limit] if a.limit else grid["nodes"]
    cache = load_json(a.cache) or {}
    now = dt.datetime.now(dt.timezone.utc)
    t0 = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=BACK)

    def key(n):
        return "%.4f,%.4f" % (n[1], n[2])

    # ---- step 1: which forecast cell is each point in (first run only) ----
    todo = [n for n in nodes if key(n) not in cache]
    if todo:
        print("looking up %d points (first run, a few minutes)..." % len(todo), flush=True)

    def point(n):
        try:
            j = fetch("%s/points/%s" % (base, key(n)), headers)
        except Exception as e:
            return n, "error", str(e)
        if j is None:
            return n, "na", None
        p = j["properties"]
        return n, "ok", {"w": p["gridId"], "x": p["gridX"], "y": p["gridY"], "tz": p.get("timeZone") or ""}

    if todo:
        with ThreadPoolExecutor(a.workers) as ex:
            for n, state, val in ex.map(point, todo):
                if state == "ok":
                    cache[key(n)] = val
                elif state == "na":
                    cache[key(n)] = {"na": True}
        Path(a.cache).write_text(json.dumps(cache, separators=(",", ":")))

    live = [n for n in nodes if cache.get(key(n)) and not cache[key(n)].get("na") and cache[key(n)].get("tz")]
    if not live:
        sys.exit("no usable points")

    # ---- step 2: hourly sky cover for every cell ----
    print("fetching sky cover for %d points..." % len(live), flush=True)

    def sky(n):
        c = cache[key(n)]
        try:
            j = fetch("%s/gridpoints/%s/%d,%d" % (base, c["w"], c["x"], c["y"]), headers)
        except Exception as e:
            return n, None
        if not j:
            return n, None
        return n, expand(j["properties"].get("skyCover", {}).get("values"), t0)

    results = {}
    with ThreadPoolExecutor(a.workers) as ex:
        for n, arr in ex.map(sky, live):
            results[n[0]] = arr

    # ---- step 3: carry over hours that have passed ----
    prev = load_json(a.out)
    prev_ok = (prev and prev.get("hours") and prev.get("t0") and prev.get("idx") and
               prev.get("grid") == {k: grid[k] for k in ("lat0", "lon0", "step", "nx", "ny")})
    shift = 0
    pmap = {}
    if prev_ok:
        shift = int(round((t0 - parse_time(prev["t0"])).total_seconds() / 3600))
        pmap = {i: s for i, s in zip(prev["idx"], prev["sky"])}

    tzs, tzi, idx, rows, good = [], [], [], [], 0
    for n in live:
        arr = results.get(n[0])
        old = pmap.get(n[0])
        if arr is None and old is None:
            continue
        if arr is None:
            arr = ["."] * HOURS
        if old:
            for h in range(HOURS):
                if arr[h] == ".":
                    oh = h + shift
                    if 0 <= oh < len(old):
                        arr[h] = old[oh]
        if all(ch == "." for ch in arr):
            continue
        tz = cache[key(n)]["tz"]
        if tz not in tzs:
            tzs.append(tz)
        idx.append(n[0]); tzi.append(tzs.index(tz)); rows.append("".join(arr))
        if results.get(n[0]) is not None:
            good += 1

    share = good / float(len(live))
    print("fresh data for %d of %d points (%.0f%%)" % (good, len(live), share * 100))
    if share < MIN_OK:
        sys.exit("only %.0f%% of points returned data; leaving the old file in place" % (share * 100))

    doc = {
        "version": 1,
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "t0": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours": HOURS,
        "grid": {k: grid[k] for k in ("lat0", "lon0", "step", "nx", "ny")},
        "tz": tzs, "idx": idx, "tzi": tzi, "sky": rows,
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=".clearsky-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(tmp, str(out))
    os.chmod(str(out), 0o644)
    print("wrote %s (%d KB)" % (out, out.stat().st_size // 1024))


if __name__ == "__main__":
    main()
