#!/usr/bin/env python3
"""The board data that updates itself: builds auto.json (run by .github/workflows/auto-data.yml).

Usage:
  python3 tools/auto.py <page.html> <auto.json>          rebuild auto.json from the sources below
  python3 tools/auto.py inline <page.html> <auto.json>   write auto.json into the page's WEEKS and BIO,
                                                         with the same rules the page uses when it loads
                                                         the file (for the Claude artifact copy, which
                                                         can't load it by itself)

The page (index.html) loads auto.json from the repo's `data` branch when it opens and merges it into
WEEKS (see "auto data" in index.html). Per week:

  build                    the weekly rebuild, for the current week and the next one: status, dates, byes,
                           GAMES (nflverse schedule; kickoffs in UTC), DEF / GP / defThrough (nflverse team
                           stats: what each defense allowed in the weeks already played), IRD (defenders on
                           injured reserve from nflverse weekly rosters; games started = games with at least
                           half the defensive snaps, nflverse snap counts), reportsFor (preview week)
  STARTERS, FILLIN         ESPN's depth chart (nflverse depth_charts: QB, RB, top three WRs, TE of the
                           3WR 1TE set), with a starter ruled out replaced by the next player and a FILLIN
                           note, and WRs / TEs swapped for a teammate who's been getting clearly more
                           targets (tools/lineup.py rules). Teams whose game has kicked off keep their lineup.
  FP, BOX, OTH, ALT, VPOS  fantasy points, box scores and defense vs position (tools/points.py, nflverse
                           player stats), every week that has finished games. nflverse posts a day's games the
                           next morning, so until then a finished game's FP, BOX and OTH come from ESPN's box
                           score (two-point conversions aren't in it; nflverse's numbers replace these when they land)
  SCORES                   finals from the nflverse schedule (backup for the browser's live feed)
  ODDS                     spread and total from the nflverse schedule (home spread = -spread_line)
  INJ, DINJ, OLINJ         the official NFL injury report (nflverse injuries: game status and latest
                           practice participation), same shapes as the page, with game-day inactives on top
  GAMEDAY                  game-day inactives for the current week (ESPN's game summary: from 2 hours before
                           kickoff, players it lists as Out). A listed player ESPN has as Out becomes O "inactive";
                           from 30 minutes before kickoff ("cleared"), a Q, D or P player it doesn't is A "active"
                           (INJ entries are removed). Defensive and line starters (ESPN depth chart) who are Out
                           without being on the report are added, and an inactive skill starter gets a fill-in.
  WX                       Open-Meteo forecast at each stadium for kickoff, roofs from the schedule
  at                       category -> when it last changed (UTC), so the page can tell whether this file
                           or its own data is newer

Plus BIO (birth dates from nflverse, hometowns from ESPN profiles) for board players the page has no
hometown for. Lines, weather and injuries are written for the current week and later only.

Needs node and curl (same as tools/points.py).
"""
import copy, csv, json, os, re, subprocess, sys, tempfile, time, urllib.request
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import points  # noqa: E402
import lineup  # noqa: E402
import bio  # noqa: E402

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download/"
STATS = NFLVERSE + "stats_player/stats_player_week_2026.csv"
INJURIES = NFLVERSE + "injuries/injuries_2026.csv"
SCHEDULE = NFLVERSE + "schedules/games.csv"
TEAM_STATS = NFLVERSE + "stats_team/stats_team_week_2026.csv"
DEPTH = NFLVERSE + "depth_charts/depth_charts_2026.csv"
ROSTERS = NFLVERSE + "weekly_rosters/roster_weekly_2026.csv"
SNAPS = NFLVERSE + "snap_counts/snap_counts_2026.csv"
TEAM_FIX = {"LA": "LAR"}
fix = lambda t: TEAM_FIX.get(t, t)
TEAMS = "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LAC LAR LV MIA MIN NE NO NYG NYJ PHI PIT SEA SF TB TEN WAS".split()

SKILL = {"QB", "RB", "FB", "WR", "TE"}
OL = {"T": "T", "G": "G", "C": "C", "OT": "T", "OG": "G", "OL": "OL"}
DEF = {"DE": "DE", "DT": "DT", "NT": "DT", "DL": "DL", "EDGE": "DE", "LB": "LB", "OLB": "LB", "ILB": "LB",
       "MLB": "LB", "CB": "CB", "S": "S", "FS": "S", "SS": "S", "DB": "DB"}
STATUS = {"Out": "O", "Doubtful": "D", "Questionable": "Q"}
PRACTICE = {"Did Not Participate In Practice": "DNP in practice", "Limited Participation in Practice": "Limited in practice",
            "Full Participation in Practice": "Full practice"}
IR_CODES = {"R01", "R48"}  # injured reserve, and IR designated to return

RETRACTABLE = {"ATL", "ARI", "DAL", "HOU", "IND"}
# games abroad: stadium name (from the schedule) -> city for the game note, stadium coordinates for the forecast
ABROAD = [("wembley", "London", (51.556, -0.280)), ("tottenham", "London", (51.604, -0.066)),
          ("stade de france", "Paris", (48.924, 2.360)), ("bernab", "Madrid", (40.453, -3.688)),
          ("bayern", "Munich", (48.219, 11.625)), ("allianz", "Munich", (48.219, 11.625)),
          ("azteca", "Mexico City", (19.303, -99.150)), ("banorte", "Mexico City", (19.303, -99.150)),
          ("melbourne", "Melbourne", (-37.820, 144.983)), ("maracan", "Rio de Janeiro", (-22.912, -43.230)),
          ("croke", "Dublin", (53.361, -6.251))]
WEATHER_DAYS = 7  # forecast games kicking off within this many days
BIO_FETCH_MAX = 40  # ESPN profile lookups per run
CATS = {"build": ("build",), "lineup": ("STARTERS", "FILLIN"), "stats": ("FP", "BOX", "OTH", "ALT", "VPOS"),
        "scores": ("SCORES",), "odds": ("ODDS",), "injuries": ("INJ", "DINJ", "OLINJ", "GAMEDAY"), "weather": ("WX",)}
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={}"
ESPN_FIX = {"WSH": "WAS", "LA": "LAR", "JAC": "JAX"}
GAMEDAY_FROM = 2 * 3600e3    # watch ESPN from this long before kickoff (inactives come out 90 minutes before)
GAMEDAY_CLEARED = 30 * 60e3  # from this long before kickoff, a listed player ESPN doesn't have as Out is playing
GAMEDAY_UNTIL = 2 * 3600e3   # and stop this long after kickoff (later runs keep the last result)


def get(url, path):
    subprocess.run(["curl", "-sSfL", "--retry", "3", "--max-time", "300", "-o", path, url], check=True)
    return path


def rows(path):
    return list(csv.DictReader(open(path, encoding="utf-8")))


def js(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def ms(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000 if iso else 0
    except ValueError:
        return 0


kick_ms = points.kick_ms


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


def abroad(stadium):
    s = (stadium or "").lower()
    return next(((c, ll) for k, c, ll in ABROAD if k in s), (None, None))


# ---------- the page's WEEKS literal ----------
HEAD = ("status", "backfill", "auto", "dates", "updated", "updatedAt", "byes", "defThrough", "yardsOnly", "reportsFor")


def week_literal(n, w):
    """One week as the page writes it: scalar fields on the first line, then one field per line
    (STARTERS one team per line), so tools/points.py and tools/lineup.py can still edit it."""
    lines = [f"{n}:{{" + "".join(f"{k}:{js(w[k])}," for k in HEAD if k in w)]
    for k, v in w.items():
        if k in HEAD:
            continue
        if k == "STARTERS" and v:
            lines.append("STARTERS:{\n" + ",\n".join(f"{t}:{js(s)}" for t, s in v.items()) + "},")
        else:
            lines.append(f"{k}:{js(v)},")
    return "\n".join(lines) + "}"


def put_week(page, n, w):
    """Replace week n's block in the page (or add it at the end of WEEKS)."""
    try:
        s0, s1 = points.week_block(page, int(n))
        tail = "," if page[s0:s1].rstrip().endswith(",") else ""
        return page[:s0] + week_literal(n, w) + tail + page[s1:]
    except AssertionError:
        s = page.index("const WEEKS=")
        e = page.index("\n};\n", s)
        sep = "" if page[:e].rstrip().endswith(",") else ","
        return page[:e] + sep + "\n" + week_literal(n, w) + page[e:]


# ---------- weekly rebuild ----------
def schedule_by_week(sched):
    out = {}
    for r in sched:
        if r["season"] == "2026" and r["game_type"] == "REG":
            out.setdefault(int(r["week"]), []).append(r)
    return out


def kickoff(r):
    et = datetime.strptime(r["gameday"] + " " + r["gametime"], "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/New_York"))
    return et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def dates_label(srows):
    ds = sorted(datetime.strptime(r["gameday"], "%Y-%m-%d") for r in srows)
    a, b = ds[0], ds[-1]
    return f"{a:%b} {a.day}–{b.day}" if a.month == b.month else f"{a:%b} {a.day}–{b:%b} {b.day}"


def defense(team_rows, before):
    """DEF and GP from what each team's opponents gained against it in weeks before `before`."""
    keys = ["carries", "rushing_yards", "rushing_tds", "attempts", "completions", "passing_yards", "passing_tds",
            "passing_interceptions", "sacks_suffered"]
    D, G = {}, Counter()
    weeks = set()
    for r in team_rows:
        if r["season_type"] != "REG" or int(r["week"]) >= before:
            continue
        d = fix(r["opponent_team"])
        a = D.setdefault(d, [0] * 9)
        for i, k in enumerate(keys):
            a[i] += int(float(r[k] or 0))
        G[d] += 1
        weeks.add(int(r["week"]))
    if not G:
        return None, None, None
    mode = Counter(G.values()).most_common(1)[0][0]
    gp = mode if len(set(G.values())) == 1 else {"default": mode, **{t: g for t, g in sorted(G.items()) if g != mode}}
    return dict(sorted(D.items())), gp, max(weeks)


def ir_defenders(roster_rows, snap_rows, n):
    wk = max((int(r["week"]) for r in roster_rows if int(r["week"]) <= n), default=None)
    if wk is None:
        return {}
    starts = Counter()
    for s in snap_rows:
        if s["game_type"] == "REG" and int(s["week"]) < n and float(s["defense_pct"] or 0) >= 0.5:
            starts[s["pfr_player_id"]] += 1
    out = {}
    for r in roster_rows:
        if int(r["week"]) != wk or r["status"] != "RES" or r["status_description_abbr"] not in IR_CODES:
            continue
        pos = DEF.get(r["depth_chart_position"]) or DEF.get(r["position"])
        if not pos:
            continue
        out.setdefault(fix(r["team"]), []).append([r["full_name"], pos, starts.get(r["pfr_id"], 0)])
    return {t: sorted(l, key=lambda x: (-x[2], x[0])) for t, l in sorted(out.items())}


def depth_orders(dc_rows):
    """Latest ESPN depth chart per team: position -> names in depth order (3WR 1TE offense)."""
    latest = {}
    for r in dc_rows:
        t = fix(r["team"])
        if r["dt"] > latest.get(t, ""):
            latest[t] = r["dt"]
    out = {}
    for r in dc_rows:
        t = fix(r["team"])
        if r["dt"] != latest[t] or r["pos_grp"] != "3WR 1TE" or r["pos_abb"] not in ("QB", "RB", "WR", "TE"):
            continue
        out.setdefault(t, {}).setdefault(r["pos_abb"], []).append((int(r["pos_rank"] or 99), r["player_name"]))
    return {t: {p: [x for _, x in sorted(l)] for p, l in d.items()} for t, d in out.items()}


def first_string(dc_rows):
    """(team, normalized name) for every offensive lineman and defender listed first on ESPN's latest depth chart."""
    latest = {}
    for r in dc_rows:
        t = fix(r["team"])
        if r["dt"] > latest.get(t, ""):
            latest[t] = r["dt"]
    return {(fix(r["team"]), points.norm(r["player_name"])) for r in dc_rows
            if r["dt"] == latest[fix(r["team"])] and r["pos_rank"] == "1" and r["pos_grp"] != "Special Teams"
            and r["pos_abb"] not in ("QB", "RB", "FB", "WR", "TE")}


# ---------- game-day inactives ----------
def espn_outs(event, k):
    """team -> [[name, position], ...] that ESPN's game summary lists as Out (entries dated after kickoff are
    in-game injuries and don't count). None when the summary has no injury lists."""
    req = urllib.request.Request(ESPN_SUMMARY.format(event), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.load(resp)
    if not d.get("injuries"):
        return None
    out = {}
    for t in d["injuries"]:
        abbr = (t.get("team") or {}).get("abbreviation") or ""
        l = out.setdefault(ESPN_FIX.get(abbr, abbr), [])
        for i in t.get("injuries") or []:
            if i.get("status") != "Out" or (i.get("date") and ms(i["date"]) > k):
                continue
            a = i.get("athlete") or {}
            pos = (a.get("position") or {}).get("abbreviation") or (i.get("position") or {}).get("abbreviation") or ""
            if a.get("displayName"):
                l.append([a["displayName"], pos])
    return {t: sorted(l) for t, l in out.items()}


def gameday(sched, cur, prev):
    """GAMEDAY for the current week: home team -> {"out": {team: [[name, pos], ...]}, "cleared": bool}."""
    now = time.time() * 1000
    old = (((prev.get("weeks") or {}).get(str(cur)) or {}).get("GAMEDAY")) or {}
    out = {}
    for r in schedule_by_week(sched).get(cur, []):
        h, k = fix(r["home_team"]), kick_ms(kickoff(r))
        if now < k - GAMEDAY_FROM:
            continue
        if now > k + GAMEDAY_UNTIL or not r.get("espn"):
            if h in old:
                out[h] = old[h]
            continue
        try:
            teams = espn_outs(r["espn"], k)
        except Exception as e:  # keep the last result if ESPN is down
            print(f"inactives {h}: {e}", file=sys.stderr)
            teams = None
        if teams is None:
            if h in old:
                out[h] = old[h]
            continue
        out[h] = {"out": teams, "cleared": now >= k - GAMEDAY_CLEARED or bool((old.get(h) or {}).get("cleared"))}
    return out


def apply_gameday(inj, starters, gd, firsts):
    """Game-day inactives on top of the injury report (INJ, DINJ, OLINJ, changed in place)."""
    INJ, DINJ, OLINJ = inj.setdefault("INJ", {}), inj.setdefault("DINJ", {}), inj.setdefault("OLINJ", {})
    for g in gd.values():
        for t, outs in g["out"].items():
            o = {points.norm(x[0]): x for x in outs}
            for L, posmap in ((DINJ, DEF), (OLINJ, OL)):
                lst, seen = L.get(t, []), set()
                for e in lst:
                    k = points.norm(e[0])
                    seen.add(k)
                    if k in o and e[2] != "O":
                        e[2:] = ["O", e[3], "inactive"]
                    elif k not in o and g["cleared"] and e[2] in ("Q", "D", "P"):
                        e[2:] = ["A", e[3], "active"]
                for k, (nm, pos) in o.items():
                    if k not in seen and (t, k) in firsts and posmap.get(pos):
                        lst.append([nm, posmap[pos], "O", "Not listed", "inactive"])
                if lst:
                    L[t] = lst
            s = starters.get(t)
            for nm in ([s[0], s[1], *s[2], s[3]] if s else []):
                e, k = INJ.get(nm), points.norm(nm)
                if k in o and not (e and e[0] == "O"):
                    INJ[nm] = ["O", e[1] if e else "Not listed", "inactive"]
                elif k not in o and g["cleared"] and e and e[0] in ("Q", "D", "P"):
                    del INJ[nm]


def week_injuries(inj, n):
    """(team, normalized name) -> [status letter, injury] for week n's report, skill players only."""
    out = {}
    for r in inj:
        if r["week"] == str(n) and r["season_type"] == "REG" and r["position"] in SKILL and STATUS.get(r["report_status"]):
            out[(fix(r["team"]), points.norm(r["full_name"]))] = [STATUS[r["report_status"]], r["report_primary_injury"] or "Not listed"]
    return out


def team_lineup(t, order, canon, hurt, stats, n):
    """[QB, RB, [WR, WR, WR], TE] and FILLIN notes for one team."""
    name = lambda x: canon.get((t, points.norm(x)), x)
    out_of = lambda x: (hurt.get((t, points.norm(x))) or [""])[0] == "O"
    fill = {}
    picks = {}
    for pos, k in (("QB", 1), ("RB", 1), ("WR", 3), ("TE", 1)):
        l = [name(x) for x in order.get(pos, [])]
        top, rest = l[:k], [x for x in l[k:] if not out_of(x)]
        got = []
        for x in top:
            if not out_of(x):
                got.append(x)
            elif rest:
                sub = rest.pop(0)
                got.append(sub)
                fill[sub] = f"Starting for {x} ({hurt[(t, points.norm(x))][1].lower()}, out)"
        picks[pos] = got
    if not (picks["QB"] and picks["RB"] and len(picks["WR"]) == 3 and picks["TE"]):
        return None, {}
    s = [picks["QB"][0], picks["RB"][0], picks["WR"], picks["TE"][0]]
    # usage check (tools/lineup.py): a WR or TE who's been clearly out-targeted by a healthy teammate
    u, games = lineup.usage(stats, t, n)
    if games:
        listed = {points.norm(x) for x in [s[1], *s[2], s[3]]}
        slots = [("WR", i) for i in range(3)] + [("TE", None)]
        for pos, i in slots:
            cur = s[2][i] if pos == "WR" else s[3]
            if cur in fill:
                continue
            mine = u.get(points.norm(cur), [cur, pos, 0.0, 0.0, False])
            pool = [d for k, d in u.items() if d[1] == pos and k not in listed and d[4] and not out_of(d[0])]
            if not pool:
                continue
            best = max(pool, key=lambda d: d[2])
            if best[2] >= lineup.RATIO * mine[2] and best[3] - mine[3] >= lineup.MIN_GAP:
                if pos == "WR":
                    s[2][i] = best[0]
                else:
                    s[3] = best[0]
                listed.discard(points.norm(cur))
                listed.add(points.norm(best[0]))
    return s, fill


def build_weeks(ctx, cur, page_weeks, prev, gd=None):
    """The weekly rebuild for the current week and the next one."""
    sw = schedule_by_week(ctx["sched"])
    canon = {}  # (team, normalized name) -> the name in the player's latest stats row
    for r in sorted(ctx["stats"], key=lambda r: int(r["week"])):
        canon[(r["team"], points.norm(r["player_display_name"]))] = r["player_display_name"]
    orders = depth_orders(ctx["depth"])
    DEFt, GP, thru = defense(ctx["team"], cur)
    now = time.time() * 1000
    out = {}
    for n in (cur, cur + 1):
        if n not in sw or DEFt is None:
            continue
        srows = sorted(sw[n], key=lambda r: (kickoff(r), r["game_id"]))
        games = []
        for r in srows:
            city, _ = abroad(r["stadium"]) if r["location"] == "Neutral" else (None, None)
            games.append([fix(r["away_team"]), fix(r["home_team"]), kickoff(r), city or ""])
        playing = {t for g in games for t in g[:2]}
        b = {"status": "live" if n == cur else "preview", "dates": dates_label(srows),
             "byes": [t for t in TEAMS if t not in playing], "defThrough": thru, "GP": GP, "DEF": DEFt,
             "GAMES": games, "IRD": ir_defenders(ctx["rosters"], ctx["snaps"], n)}
        hurt = week_injuries(ctx["inj"], n)
        if n == cur:  # game-day inactives count as out
            for g in (gd or {}).values():
                for t, outs in g["out"].items():
                    for nm, pos in outs:
                        k = (t, points.norm(nm))
                        if pos in SKILL and (hurt.get(k) or [""])[0] != "O":
                            hurt[k] = ["O", "inactive" if not hurt.get(k) else hurt[k][1]]
        kick = {t: kick_ms(g[2]) for g in games for t in g[:2]}
        old = ((prev.get("weeks") or {}).get(str(n)) or {})
        inline = page_weeks.get(str(n)) or {}
        S, F = {}, {}
        for t in sorted(playing):
            if kick[t] <= now:  # game under way or over: keep the lineup it had
                s = (old.get("STARTERS") or {}).get(t) or (inline.get("STARTERS") or {}).get(t)
                if s:
                    S[t] = s
                    for x in [s[0], s[1], *s[2], s[3]]:
                        f = (old.get("FILLIN") or {}).get(x) or (inline.get("FILLIN") or {}).get(x)
                        if f:
                            F[x] = f
                    continue
            if t not in orders:
                continue
            s, f = team_lineup(t, orders[t], canon, hurt, ctx["stats"], n)
            if s:
                S[t] = s
                F.update(f)
        out[n] = {"build": b, "STARTERS": S, "FILLIN": F}
    return out


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


# ---------- fantasy points at the final whistle (ESPN box score) ----------
def espn_box(summary):
    """team -> {name: box score with the page's BOX keys} from ESPN's game summary."""
    def num(x):
        try:
            return int(float(x))
        except (TypeError, ValueError):
            return 0
    out = {}
    for team in (summary.get("boxscore") or {}).get("players") or []:
        abbr = (team.get("team") or {}).get("abbreviation") or ""
        boxes = out.setdefault(ESPN_FIX.get(abbr, abbr), {})
        for g in team.get("statistics") or []:
            keys, kind = g.get("keys") or [], g.get("name")
            for a in g.get("athletes") or []:
                nm = (a.get("athlete") or {}).get("displayName")
                if not nm:
                    continue
                v, b = dict(zip(keys, a.get("stats") or [])), boxes.setdefault(nm, {})
                add = lambda k, x: b.__setitem__(k, b.get(k, 0) + num(x))
                if kind == "passing":
                    c, att = (str(v.get("completions/passingAttempts", "0/0")).split("/") + ["0"])[:2]
                    add("pass_cmp", c), add("pass_att", att), add("pass_yd", v.get("passingYards"))
                    add("pass_td", v.get("passingTouchdowns")), add("pass_int", v.get("interceptions"))
                    add("pass_sack", str(v.get("sacks-sackYardsLost", "0")).split("-")[0])
                elif kind == "rushing":
                    add("rush_att", v.get("rushingAttempts")), add("rush_yd", v.get("rushingYards")), add("rush_td", v.get("rushingTouchdowns"))
                elif kind == "receiving":
                    add("rec", v.get("receptions")), add("rec_yd", v.get("receivingYards"))
                    add("rec_td", v.get("receivingTouchdowns")), add("rec_tgt", v.get("receivingTargets"))
                elif kind == "fumbles":
                    add("fum_lost", v.get("fumblesLost"))
                elif kind in ("kickReturns", "puntReturns"):
                    add("st_td", v.get(kind[:-1] + "Touchdowns"))
    return {t: {nm: {k: x for k, x in b.items() if x} for nm, b in bx.items()} for t, bx in out.items()}


def box_points(b):
    """[standard, PPR] from a box score (the page's boxPts)."""
    g = lambda k: b.get(k, 0)
    std = g("pass_yd") * .04 + g("pass_td") * 4 - g("pass_int") * 2 + (g("rush_yd") + g("rec_yd")) * .1 \
        + (g("rush_td") + g("rec_td") + g("st_td")) * 6 - g("fum_lost") * 2 + (g("pass_2pt") + g("rush_2pt") + g("rec_2pt")) * 2
    return [round(std, 2), round(std + g("rec"), 2)]


def box_line(b):
    """The stat line tools/points.py writes, from a box score."""
    g = lambda k: b.get(k, 0)
    parts = []
    if g("pass_att"):
        parts.append(f"{g('pass_cmp')}/{g('pass_att')}, {g('pass_yd')} pass yds" + (f", {g('pass_td')} TD" if g("pass_td") else "")
                     + (f", {g('pass_int')} INT" if g("pass_int") else ""))
    if g("rush_att"):
        parts.append(f"{g('rush_att')} car, {g('rush_yd')} rush yds" + (f", {g('rush_td')} TD" if g("rush_td") else ""))
    if g("rec_tgt") or g("rec"):
        parts.append(f"{g('rec')}/{g('rec_tgt')} rec, {g('rec_yd')} yds" + (f", {g('rec_td')} TD" if g("rec_td") else ""))
    if g("fum_lost"):
        parts.append(f"{g('fum_lost')} fumble{'s' if g('fum_lost') > 1 else ''} lost")
    if g("st_td"):
        parts.append(f"{g('st_td')} return TD")
    return " · ".join(parts) or "No stats recorded"


def espn_points(view, sched, cur, stats_part, stats_rows, rosters):
    """Fill FP, BOX and OTH from ESPN for finished games of the current and previous week that nflverse
    hasn't posted yet (stats_part, changed in place). Returns the games filled, for the log."""
    now = time.time() * 1000
    have = {(int(r["week"]), r["team"]) for r in stats_rows}
    pos = {}
    for r in rosters:
        p = {"FB": "RB"}.get(r["position"], r["position"])
        if p in ("QB", "RB", "WR", "TE"):
            pos[(fix(r["team"]), points.norm(r["full_name"]))] = p
    espn = {(int(r["week"]), fix(r["home_team"])): r.get("espn") for r in sched if r["season"] == "2026" and r["game_type"] == "REG"}
    done = []
    for n in (cur - 1, cur):
        w = view.get(str(n))
        if not w or w.get("backfill"):
            continue
        for a, h, iso, *_ in w["GAMES"]:
            k = kick_ms(iso)
            if now < k + 2.5 * 3600e3 or now > k + 4 * 86400e3 or ((n, a) in have and (n, h) in have) or not espn.get((n, h)):
                continue
            try:
                req = urllib.request.Request(ESPN_SUMMARY.format(espn[(n, h)]), headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    d = json.load(resp)
            except Exception as e:
                print(f"box score {a}@{h}: {e}", file=sys.stderr)
                continue
            st = (((d.get("header") or {}).get("competitions") or [{}])[0].get("status") or {}).get("type") or {}
            if not st.get("completed"):
                continue
            boxes = espn_box(d)
            part = stats_part.setdefault(str(n), {})
            FP, BOX, OTH = part.setdefault("FP", {}), part.setdefault("BOX", {}), part.setdefault("OTH", {})
            for t in (a, h):
                if t not in boxes or (n, t) in have:
                    continue
                s = (w.get("STARTERS") or {}).get(t)
                listed = set()
                by = {points.norm(nm): (nm, b) for nm, b in boxes[t].items()}
                for nm in ([s[0], s[1], *s[2], s[3]] if s else []):
                    listed.add(points.norm(nm))
                    hit = by.get(points.norm(nm))
                    b = hit[1] if hit else None
                    if b and any(x in b for x in ("pass_att", "rush_att", "rec", "rec_tgt", "st_td", "fum_lost")):
                        FP[nm], BOX[nm] = [*box_points(b), box_line(b)], b
                    else:
                        FP[nm], BOX[nm] = [0, 0, "No stats recorded"], {"dnp": 1}
                oth = []
                for k2, (nm, b) in by.items():
                    p = pos.get((t, k2))
                    pts = box_points(b)
                    if k2 not in listed and p and (pts[0] or pts[1]):
                        oth.append([nm, p, *pts, box_line(b)])
                if oth:
                    OTH[t] = sorted(oth, key=lambda x: -x[3])
            done.append(f"{a} @ {h}")
    return done


# ---------- schedule: finals and lines ----------
def schedule_fields(weeks, sched, cur):
    by_pair = {}
    for r in sched:
        if r["season"] == "2026" and r["game_type"] == "REG":
            by_pair[(r["week"], fix(r["away_team"]), fix(r["home_team"]))] = r
    out = {}
    for n, w in weeks.items():
        if w.get("backfill"):  # weeks 1-4 keep their finals in the game notes
            continue
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
            t = fix(r["team"])
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
    stadium = page_const(page, "STADIUM")
    roof = {}
    for r in sched:
        if r["season"] == "2026" and r["game_type"] == "REG":
            roof[(r["week"], fix(r["home_team"]))] = (r["roof"], r["stadium"], r["location"] == "Neutral")
    now = time.time() * 1000
    out = {}
    for n, w in weeks.items():
        if int(n) < cur:
            continue
        old = ((prev.get("weeks") or {}).get(n) or {}).get("WX") or {}
        wx = {}
        for g in w["GAMES"]:
            h, k = g[1], kick_ms(g[2])
            rf, name, neutral = roof.get((str(n), h), ("", "", False))
            if neutral:
                rf = "outdoors"  # games abroad: the schedule's roof field describes the stadium loosely, forecast them
            if rf == "dome":
                wx[h] = ["Dome", "Indoors", 0]
                continue
            if rf in ("closed", "open") or (rf == "" and h in RETRACTABLE):
                wx[h] = ["Roof", "Retractable roof" + (", open" if rf == "open" else ""), 0]
                continue
            if k <= now or k - now > WEATHER_DAYS * 86400e3:
                if h in old:
                    wx[h] = old[h]
                continue
            city, ll = abroad(name) if neutral else (None, None)
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
                wx[h] = ["Outdoor", (city + " · " if city else "") + text, impact]
            elif h in old:
                wx[h] = old[h]
        if wx:
            out[n] = {"WX": wx}
    return out


# ---------- birthdays and hometowns ----------
def bio_fields(page, weeks, prev):
    have = bio.read_bio(page)
    out = dict(prev.get("BIO") or {})
    players = bio.board_players(weeks)
    need = [n for n in sorted(players) if len(have.get(n, [])) < 2 and len(out.get(n, [])) < 2]
    if not need:
        return {k: v for k, v in out.items() if k in players and len(have.get(k, [])) < 2}
    by = bio.nflverse_players()
    fetched = 0
    for name in need:
        r = bio.match(by, name, players[name])
        d = (r or {}).get("birth_date") or (have.get(name) or [None])[0]
        eid = (r or {}).get("espn_id", "").split(".")[0]
        entry = [d]
        if eid and fetched < BIO_FETCH_MAX:
            fetched += 1
            try:
                with urllib.request.urlopen(bio.ESPN.format(eid), timeout=20) as resp:
                    place = (json.load(resp).get("athlete") or {}).get("displayBirthPlace")
                if place:
                    ll = bio.geocode(place)
                    entry = [d, place, *(ll or (None, None))]
                else:
                    entry = [d, None]
            except Exception as e:  # try again next run
                print(f"hometown {name}: {e}", file=sys.stderr)
        out[name] = entry
    return {k: v for k, v in out.items() if k in players and len(have.get(k, [])) < 2}


# ---------- merging, the same rules as the page ----------
def at_max(at):
    return max((ms(v) for v in (at or {}).values()), default=0)


def mt_label(t_ms):
    d = datetime.fromtimestamp(t_ms / 1000, ZoneInfo("America/Denver"))
    return f"{d:%a %b} {d.day}, {d.strftime('%I:%M %p').lstrip('0')} MT"


def merge_week(w, d, n, cur, now):
    """Week n of the page (or None) with the auto data d applied. Mirrors mergeAuto() in index.html."""
    w = copy.deepcopy(w) if w else None
    at = d.get("at") or {}
    if d.get("build"):
        if w is None:
            w = {"INJ": {}, "FILLIN": {}, "WX": {}, "DINJ": {}, "OLINJ": {}, "ODDS": {}, "STARTERS": {}}
        if w.get("auto") or w.get("status") == "preview" or not w.get("GAMES"):
            w.update(copy.deepcopy(d["build"]))
            w["auto"] = True
            if "reportsFor" not in d["build"]:
                w.pop("reportsFor", None)
    if w is None:
        return None
    upd = ms(w.get("updatedAt"))
    newer = lambda c: c in at and ms(at[c]) > upd
    mix = lambda mine, theirs, win: {**(mine or {}), **theirs} if win else {**theirs, **(mine or {})}
    for k in ("FP", "BOX", "OTH"):
        if d.get(k):
            w[k] = {**(w.get(k) or {}), **d[k]}
    if d.get("ALT"):
        w["ALT"] = d["ALT"]
    if d.get("VPOS") and (not w.get("VPOS") or d["VPOS"]["thru"] >= w["VPOS"]["thru"]):
        w["VPOS"] = d["VPOS"]
    if d.get("SCORES"):
        w["SCORES"] = mix(w.get("SCORES"), d["SCORES"], False)
    if int(n) < cur or w.get("backfill"):
        return w
    if d.get("STARTERS"):
        S, F = w.setdefault("STARTERS", {}), w.setdefault("FILLIN", {})
        kick = {t: kick_ms(g[2]) for g in w["GAMES"] for t in g[:2]}
        win = newer("lineup")
        for t, s in d["STARTERS"].items():
            if t in S and (kick.get(t, 0) <= now or not win):
                continue
            if t in S:
                for x in [S[t][0], S[t][1], *S[t][2], S[t][3]]:
                    F.pop(x, None)
            S[t] = s
            for x in [s[0], s[1], *s[2], s[3]]:
                if (d.get("FILLIN") or {}).get(x):
                    F[x] = d["FILLIN"][x]
    if d.get("ODDS"):
        w["ODDS"] = mix(w.get("ODDS"), d["ODDS"], newer("odds"))
    if d.get("WX"):
        w["WX"] = mix(w.get("WX"), d["WX"], newer("weather"))
    if d.get("DINJ") is not None or d.get("INJ") is not None:
        if newer("injuries"):
            w["INJ"], w["DINJ"], w["OLINJ"] = d.get("INJ") or {}, d.get("DINJ") or {}, d.get("OLINJ") or {}
        else:
            for k in ("INJ", "DINJ", "OLINJ"):
                w[k] = mix(w.get(k), d.get(k) or {}, False)
    if w.get("auto") and at_max(at) > upd:
        w["updated"] = mt_label(at_max(at))
    return w


def inline(page_path, auto_path):
    page = open(page_path, encoding="utf-8").read()
    data = json.load(open(auto_path, encoding="utf-8"))
    weeks = points.read_weeks(page)
    now = time.time() * 1000
    merged = {n: w for n, w in weeks.items()}
    for n, d in (data.get("weeks") or {}).items():
        m = merge_week(weeks.get(n), d, n, 0, now)  # cur filled in below
        if m is not None:
            merged[n] = m
    cur = current_week(merged)
    changed = []
    for n, d in sorted((data.get("weeks") or {}).items(), key=lambda x: int(x[0])):
        m = merge_week(weeks.get(n), d, n, cur, now)
        if m is not None and m != weeks.get(n):
            page = put_week(page, n, m)
            changed.append(n)
    if data.get("BIO"):
        b = bio.read_bio(page)
        add = {k: v for k, v in data["BIO"].items() if len(b.get(k, [])) < len(v)}
        if add:
            b.update(add)
            page = bio.write_bio(page, b)
            changed.append(f"BIO ({len(add)})")
    open(page_path, "w", encoding="utf-8").write(page)
    print("Updated weeks: " + ", ".join(changed) if changed else "Page already has the auto data.")


# ---------- auto.json ----------
def main():
    if sys.argv[1] == "inline":
        return inline(sys.argv[2], sys.argv[3])
    page_path, out_path = sys.argv[1], sys.argv[2]
    page = open(page_path, encoding="utf-8").read()
    weeks = points.read_weeks(page)
    prev = json.load(open(out_path, encoding="utf-8")) if os.path.exists(out_path) else {}
    tmp = tempfile.mkdtemp()
    d = lambda url, name: get(url, os.path.join(tmp, name))
    stats_csv = d(STATS, "stats.csv")
    stats = [r for r in rows(stats_csv) if r["season_type"] == "REG"]
    for r in stats:
        r["team"] = fix(r["team"])
    ctx = {"stats": stats, "sched": rows(d(SCHEDULE, "games.csv")), "inj": rows(d(INJURIES, "injuries.csv")),
           "team": rows(d(TEAM_STATS, "team.csv")), "depth": rows(d(DEPTH, "depth.csv")),
           "rosters": rows(d(ROSTERS, "rosters.csv")), "snaps": rows(d(SNAPS, "snaps.csv"))}

    # the weekly rebuild: the current week (from the schedule, not just the page) and the next one
    sw = schedule_by_week(ctx["sched"])
    allw = {str(n): {"GAMES": [[r["away_team"], r["home_team"], kickoff(r)] for r in sr]} for n, sr in sw.items()}
    cur = current_week(allw)
    gd = gameday(ctx["sched"], cur, prev)
    built = build_weeks(ctx, cur, weeks, prev, gd)
    # every week as the page will see it, built weeks included, for the scripts below
    view = {n: w for n, w in weeks.items()}
    work = page
    for n, b in built.items():
        m = merge_week(weeks.get(str(n)), {**b, "at": {"build": "2999-01-01T00:00:00Z", "lineup": "2999-01-01T00:00:00Z"}}, str(n), cur, time.time() * 1000)
        view[str(n)] = m
        work = put_week(work, n, m)

    parts = [{str(n): v for n, v in built.items()}, stats_fields(work, stats_csv), schedule_fields(view, ctx["sched"], cur),
             injury_fields(view, ctx["inj"], cur), weather_fields(view, ctx["sched"], page, cur, prev)]
    filled = espn_points(view, ctx["sched"], cur, parts[1], stats, ctx["rosters"])
    if filled:
        print("Fantasy points from ESPN (nflverse not posted yet): " + ", ".join(filled))
    if gd:  # game-day inactives on top of the current week's injury report
        w = parts[3].setdefault(str(cur), {"INJ": {}, "DINJ": {}, "OLINJ": {}})
        apply_gameday(w, view[str(cur)].get("STARTERS") or {}, gd, first_string(ctx["depth"]))
        w["GAMEDAY"] = gd
    for n, b in built.items():  # preview week: the teams whose practice reports have started
        if b["build"]["status"] == "preview":
            inj = parts[3].get(str(n)) or {}
            teams = sorted({t for k in ("DINJ", "OLINJ") for t in inj.get(k, {})} |
                           {t for t, s in b["STARTERS"].items() if any(x in inj.get("INJ", {}) for x in [s[0], s[1], *s[2], s[3]])})
            if teams:
                b["build"]["reportsFor"] = teams
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for n in sorted(view, key=int):
        w = {}
        for p in parts:
            w.update(p.get(n, {}))
        if not w:
            continue
        old = (prev.get("weeks") or {}).get(n) or {}
        at = {}
        for cat, keys in CATS.items():
            if not any(k in w for k in keys):
                continue
            same = all(w.get(k) == old.get(k) for k in keys)
            at[cat] = (old.get("at") or {}).get(cat, stamp) if same else stamp
        w["at"] = at
        out[n] = w
    result = {"generated": stamp, "currentWeek": cur, "weeks": out, "BIO": bio_fields(page, view, prev)}
    # rewrite only when the data changed, so the branch history shows real updates
    if {k: v for k, v in result.items() if k != "generated"} == {k: v for k, v in prev.items() if k != "generated"}:
        print("No changes.")
        return
    json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    changed = sorted({f"Week {n}: {c}" for n, w in out.items() for c, t in w["at"].items() if t == stamp})
    print("\n".join(changed) or "Updated.")


if __name__ == "__main__":
    main()
