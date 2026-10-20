#!/usr/bin/env python3
"""Build data/auto.json: the board data that updates itself (run by .github/workflows/auto-data.yml).

Usage: python3 tools/auto.py <page.html> <auto.json>

The page (index.html) loads auto.json from the repo's `data` branch when it opens and merges it
into WEEKS, so these fields stay current without anyone republishing the page:

  FP, BOX, OTH, ALT, VPOS  fantasy points, box scores and defense vs position (tools/points.py,
                           nflverse player stats), every week that has finished games
  SCORES                   finals from the nflverse schedule (backup for the browser's live feed)
  ODDS                     spread and total from the nflverse schedule (home spread = -spread_line)
  INJ, DINJ, OLINJ         the official NFL injury report (nflverse injuries: game status and
                           latest practice participation), same shapes as the page
  WX                       Open-Meteo forecast at each stadium for kickoff, roofs from the schedule

Lines, weather and injuries are written for the current week and later only; finished weeks keep
what the page has. Each week also gets `at`: category -> when that category last changed (UTC),
so the page can tell whether this file or its own data is newer (see "auto data" in index.html).
Weather for games that have kicked off is carried over from the previous file.

Needs node and curl (same as tools/points.py).
"""
import csv, json, os, re, subprocess, sys, tempfile, time, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import points  # noqa: E402

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download/"
STATS = NFLVERSE + "stats_player/stats_player_week_2026.csv"
INJURIES = NFLVERSE + "injuries/injuries_2026.csv"
SCHEDULE = NFLVERSE + "schedules/games.csv"
TEAM_FIX = {"LA": "LAR"}

SKILL = {"QB", "RB", "FB", "WR", "TE"}
OL = {"T": "T", "G": "G", "C": "C", "OT": "T", "OG": "G", "OL": "OL"}
DEF = {"DE": "DE", "DT": "DT", "NT": "DT", "DL": "DL", "EDGE": "DE", "LB": "LB", "OLB": "LB", "ILB": "LB",
       "MLB": "LB", "CB": "CB", "S": "S", "FS": "S", "SS": "S", "DB": "DB"}
STATUS = {"Out": "O", "Doubtful": "D", "Questionable": "Q"}
PRACTICE = {"Did Not Participate In Practice": "DNP in practice", "Limited Participation in Practice": "Limited in practice",
            "Full Participation in Practice": "Full practice"}

RETRACTABLE = {"ATL", "ARI", "DAL", "HOU", "IND"}
ABROAD = {"wembley": (51.556, -0.280), "tottenham": (51.604, -0.066), "stade de france": (48.924, 2.360),
          "bernab": (40.453, -3.688), "allianz": (48.219, 11.625), "azteca": (19.303, -99.150), "banorte": (19.303, -99.150),
          "melbourne": (-37.820, 144.983), "maracan": (-22.912, -43.230), "croke": (53.361, -6.251)}
WEATHER_DAYS = 7  # forecast games kicking off within this many days


def get(url, path):
    subprocess.run(["curl", "-sSfL", "--retry", "3", "--max-time", "180", "-o", path, url], check=True)
    return path


def rows(path):
    return list(csv.DictReader(open(path, encoding="utf-8")))


def kick_ms(iso):
    return points.kick_ms(iso)


def current_week(weeks):
    """Same rule as the page: the first week whose last game hasn't finished (kickoff + 4 hours)."""
    now = time.time() * 1000
    ns = sorted(weeks, key=int)
    for n in ns:
        if now < max(kick_ms(g[2]) for g in weeks[n]["GAMES"]) + 4 * 3600e3:
            return int(n)
    return int(ns[-1])


def page_const(page, name):
    m = re.search(r"const %s=(\{.*?\});\n" % name, page)
    assert m, name + " not found"
    return json.loads(subprocess.run(["node", "-e", "process.stdout.write(JSON.stringify(" + m.group(1) + "))"],
                                     capture_output=True, text=True, check=True).stdout)


# ---------- fantasy points, box scores, defense vs position ----------
def stats_fields(page, csv_path):
    """Run tools/points.py on a copy of the page and read back the fields it fills."""
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(page)
        tmp = f.name
    try:
        subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "points.py"), tmp, "--csv", csv_path],
                       check=True, capture_output=True, text=True)
        weeks = points.read_weeks(open(tmp, encoding="utf-8").read())
    finally:
        os.unlink(tmp)
    out = {}
    for n, w in weeks.items():
        d = {k: w[k] for k in ("FP", "BOX", "OTH", "ALT", "VPOS") if w.get(k)}
        if d:
            out[n] = d
    return out


# ---------- schedule: finals and lines ----------
def schedule_fields(weeks, sched, cur):
    by_pair = {}
    for r in sched:
        if r["season"] == "2026" and r["game_type"] == "REG":
            by_pair[(r["week"], TEAM_FIX.get(r["away_team"], r["away_team"]), TEAM_FIX.get(r["home_team"], r["home_team"]))] = r
    out = {}
    for n, w in weeks.items():
        sc, odds = {}, {}
        for g in w["GAMES"]:
            r = by_pair.get((str(n), g[0], g[1]))
            if not r:
                continue
            if r["away_score"] != "" and r["home_score"] != "":
                sc[g[1]] = [int(r["away_score"]), int(r["home_score"])] + (["OT"] if r.get("overtime") == "1" else [])
            if int(n) >= cur and r["spread_line"] != "" and r["total_line"] != "":
                sp = -float(r["spread_line"])
                odds[g[1]] = [int(sp) if sp.is_integer() else sp, float(r["total_line"])]
        d = {}
        if sc:
            d["SCORES"] = sc
        if odds:
            d["ODDS"] = odds
        if d:
            out[n] = d
    return out


# ---------- injuries ----------
def injury_fields(weeks, inj, cur):
    out = {}
    for n, w in weeks.items():
        if int(n) < cur:
            continue
        playing = {t for g in w["GAMES"] for t in g[:2]}
        starters = {}
        for t, s in (w.get("STARTERS") or {}).items():
            for name in [s[0], s[1], *s[2], s[3]]:
                starters[(t, points.norm(name))] = name
        INJ, DINJ, OLINJ = {}, {}, {}
        for r in inj:
            if r["week"] != str(n) or r["season_type"] != "REG":
                continue
            t = TEAM_FIX.get(r["team"], r["team"])
            if t not in playing:
                continue
            st = STATUS.get(r["report_status"])
            prac = PRACTICE.get(r["practice_status"], "")
            if not st:
                if prac in ("DNP in practice", "Limited in practice"):
                    st = "P"
                else:
                    continue  # full practice and no game status: not on the report in a way the page shows
            hurt = r["report_primary_injury"] or r["practice_primary_injury"] or "Not listed"
            if r["report_secondary_injury"]:
                hurt += ", " + r["report_secondary_injury"].lower()
            note = prac if prac else ""
            pos = r["position"]
            if pos in SKILL:
                name = starters.get((t, points.norm(r["full_name"])), r["full_name"])
                INJ[name] = [st, hurt] + ([note] if note else [])
            elif pos in OL:
                OLINJ.setdefault(t, []).append([r["full_name"], OL[pos], st, hurt] + ([note] if note else []))
            elif pos in DEF:
                DINJ.setdefault(t, []).append([r["full_name"], DEF[pos], st, hurt] + ([note] if note else []))
        order = {"O": 0, "D": 1, "Q": 2, "P": 3}
        for d in (DINJ, OLINJ):
            for l in d.values():
                l.sort(key=lambda x: (order[x[2]], x[0]))
        if INJ or DINJ or OLINJ:  # nothing yet means this week's first report isn't out
            out[n] = {"INJ": INJ, "DINJ": dict(sorted(DINJ.items())), "OLINJ": dict(sorted(OLINJ.items()))}
    return out


# ---------- weather ----------
def forecast(lat, lon, start_ms):
    day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&hourly=temperature_2m,precipitation_probability,"
           "weather_code,wind_speed_10m,wind_gusts_10m&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=GMT"
           "&start_date=%s&end_date=%s") % (lat, lon, day, day)
    with urllib.request.urlopen(url, timeout=30) as resp:
        h = json.load(resp)["hourly"]
    hours = [datetime.fromisoformat(x).replace(tzinfo=timezone.utc).timestamp() * 1000 for x in h["time"]]
    idx = [i for i, t in enumerate(hours) if start_ms - 1800e3 <= t <= start_ms + 3 * 3600e3]
    if not idx:
        return None
    mid = min(idx, key=lambda i: abs(hours[i] - (start_ms + 3600e3)))
    pick = lambda k: [h[k][i] for i in idx if h[k][i] is not None]
    temp = h["temperature_2m"][mid]
    pp = max(pick("precipitation_probability") or [0])
    wind = max(pick("wind_speed_10m") or [0])
    gust = max(pick("wind_gusts_10m") or [0])
    codes = pick("weather_code")
    return temp, pp, wind, gust, codes


def describe(temp, pp, wind, gust, codes):
    snow = any(c in (71, 73, 75, 77, 85, 86) for c in codes) or (pp >= 20 and temp <= 33)
    storm = any(c >= 95 for c in codes)
    if pp >= 20:
        sky = f"{pp}% " + ("snow" if snow else "storms" if storm else "rain")
    else:
        c = codes[len(codes) // 2] if codes else 0
        sky = "clear" if c <= 1 else "partly cloudy" if c == 2 else "fog" if c in (45, 48) else "cloudy"
    w = round(wind)
    wtxt = ("light breeze" if w < 7 else "light wind" if w < 13 else "moderate wind" if w < 19 else
            "strong wind" if w < 25 else "high wind") + f" ({w} mph" + (f", gusts {round(gust)}" if gust >= 25 else "") + ")"
    impact = 2 if pp >= 70 or w >= 20 or gust >= 35 or (snow and pp >= 40) else \
        1 if pp >= 40 or w >= 13 or gust >= 28 or temp <= 25 or temp >= 92 else 0
    return [f"{round(temp)}°F, {sky}, {wtxt}", impact]


def weather_fields(weeks, sched, page, cur, prev):
    stadium, neutral = page_const(page, "STADIUM"), page_const(page, "NEUTRAL")
    roof = {}
    for r in sched:
        if r["season"] == "2026" and r["game_type"] == "REG":
            roof[(r["week"], TEAM_FIX.get(r["home_team"], r["home_team"]))] = (r["roof"], r["stadium"])
    now = time.time() * 1000
    out = {}
    for n, w in weeks.items():
        if int(n) < cur:
            continue
        old = ((prev.get("weeks") or {}).get(n) or {}).get("WX") or {}
        wx = {}
        for g in w["GAMES"]:
            h, k = g[1], kick_ms(g[2])
            rf, name = roof.get((str(n), h), ("", ""))
            if neutral.get(f"{n}|{h}"):
                rf = "outdoors"  # games abroad: the schedule's roof field describes the stadium loosely, forecast them
            if rf == "dome":
                wx[h] = ["Dome", "Indoors", 0]
                continue
            if rf in ("closed", "open") or (rf == "" and h in RETRACTABLE ):
                wx[h] = ["Roof", "Retractable roof" + (", open" if rf == "open" else ""), 0]
                continue
            if k <= now or k - now > WEATHER_DAYS * 86400e3:
                if h in old:
                    wx[h] = old[h]
                continue
            ll = next((v for s, v in ABROAD.items() if s in name.lower()), None) if neutral.get(f"{n}|{h}") else None
            ll = ll or stadium.get(h)
            if not ll:
                continue
            try:
                f = forecast(ll[0], ll[1], k)
            except Exception as e:  # keep the last forecast if Open-Meteo is down
                print(f"weather {h}: {e}", file=sys.stderr)
                f = None
            if f:
                text, impact = describe(*f)
                place = neutral.get(f"{n}|{h}")
                wx[h] = ["Outdoor", (place + " · " if place else "") + text, impact]
            elif h in old:
                wx[h] = old[h]
        if wx:
            out[n] = {"WX": wx}
    return out


def main():
    page_path, out_path = sys.argv[1], sys.argv[2]
    page = open(page_path, encoding="utf-8").read()
    weeks = points.read_weeks(page)
    cur = current_week(weeks)
    prev = json.load(open(out_path, encoding="utf-8")) if os.path.exists(out_path) else {}
    tmp = tempfile.mkdtemp()
    stats_csv = get(STATS, os.path.join(tmp, "stats.csv"))
    sched = [r for r in rows(get(SCHEDULE, os.path.join(tmp, "games.csv")))]
    inj = rows(get(INJURIES, os.path.join(tmp, "injuries.csv")))

    parts = [stats_fields(page, stats_csv), schedule_fields(weeks, sched, cur),
             injury_fields(weeks, inj, cur), weather_fields(weeks, sched, page, cur, prev)]
    cats = {"stats": ("FP", "BOX", "OTH", "ALT", "VPOS"), "scores": ("SCORES",), "odds": ("ODDS",),
            "injuries": ("INJ", "DINJ", "OLINJ"), "weather": ("WX",)}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for n in sorted(weeks, key=int):
        d = {}
        for p in parts:
            d.update(p.get(n, {}))
        if not d:
            continue
        old = (prev.get("weeks") or {}).get(n) or {}
        at = {}
        for cat, keys in cats.items():
            if not any(k in d for k in keys):
                continue
            same = all(d.get(k) == old.get(k) for k in keys)
            at[cat] = (old.get("at") or {}).get(cat, stamp) if same else stamp
        d["at"] = at
        out[n] = d
    result = {"generated": stamp, "currentWeek": cur, "weeks": out}
    # rewrite only when the data changed, so the branch history shows real updates
    if {k: v for k, v in result.items() if k != "generated"} == {k: v for k, v in prev.items() if k != "generated"}:
        print("No changes.")
        return
    json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    changed = sorted({f"Week {n}: {c}" for n, d in out.items() for c, t in d["at"].items() if t == stamp})
    print("\n".join(changed) or "Updated.")


if __name__ == "__main__":
    main()
