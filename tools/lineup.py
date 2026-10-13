#!/usr/bin/env python3
"""Usage-based starters for the Matchup Sheet page.

Depth charts (Ourlads) order players by role and snaps, not by how often they get the ball:
a blocking tight end can be "first string" while another TE gets the targets. This script
checks each team's listed WRs and TE against who has actually been used in the team's
recent games (nflverse weekly player stats) and swaps in the usage leader when the gap is clear.

  python3 tools/lineup.py <page.html> <week> [--apply] [--csv stats.csv]

Usage = opportunities per game played over the team's last 3 games before <week>, weighted
toward recent games (3, 2, 1): targets for WR and TE, carries + targets for RB.
A listed starter is replaced only when a teammate at the same position
  - played in at least one of the team's last 2 games,
  - isn't ruled out (INJ status O, or the player a FILLIN note says is out) and isn't
    already a listed starter,
  - and has at least 1.3x the listed player's weighted usage and at least 2 more
    opportunities per game.
QBs and RBs are left alone (depth chart plus injury fill-ins handle them), as is any slot whose
player has a FILLIN note (the injury fill-in logic owns it). Teams whose game has kicked off
are never changed. Without --apply it only prints the proposed swaps.
"""
import csv, json, os, re, subprocess, sys, tempfile, time
from datetime import datetime

URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2026.csv"
TEAM_FIX = {"LA": "LAR"}
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")
WEIGHTS = (3, 2, 1)
RATIO, MIN_GAP = 1.3, 2.0


def norm(name):
    n = name.lower().replace(".", "").replace("'", "").replace("’", "").replace("-", " ")
    return " ".join(SUFFIX.sub("", n).split())


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def read_weeks(page):
    m = re.search(r"const WEEKS=(\{.*?\n\});\n", page, re.S)
    assert m, "WEEKS not found"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write("const WEEKS=" + m.group(1) + ";process.stdout.write(JSON.stringify(WEEKS));")
        js = f.name
    try:
        return json.loads(subprocess.run(["node", js], capture_output=True, text=True, check=True).stdout)
    finally:
        os.unlink(js)


def week_block(page, n):
    s = page.index("const WEEKS=")
    m = re.compile(r"\n%d:\{" % n).search(page, s)
    assert m, f"week {n} not found"
    nxt = re.compile(r"\n\d+:\{|\n\};\n").search(page, m.end())
    return m.start() + 1, nxt.start()


def kick_ms(iso):
    if len(iso) == 10:
        iso += "T12:00:00Z"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000


def usage(rows, team, n):
    """{normalized name: [display name, position, weighted usage per game played, usage per game played,
    played in one of the last 2 games]} over the team's last 3 games before week n. Averaging over the
    games a player actually played keeps a starter who missed time from looking unused. A player with
    no game in that window falls back to his season average, so a starter back from injury is judged
    on his own games."""
    trows = [r for r in rows if r["team"] == team and int(r["week"]) < n]
    games = sorted({int(r["week"]) for r in trows}, reverse=True)[:3]
    if not games:
        return {}, []
    acc, season = {}, {}
    for r in trows:
        pos = {"FB": "RB"}.get(r["position"], r["position"])
        if pos not in ("RB", "WR", "TE"):
            continue
        opp = num(r["targets"]) + (num(r["carries"]) if pos == "RB" else 0)
        k = norm(r["player_display_name"])
        season.setdefault(k, [r["player_display_name"], pos, []])[2].append(opp)
        wk = int(r["week"])
        if wk in games:
            acc.setdefault(k, [r["player_display_name"], pos, []])[2].append((WEIGHTS[games.index(wk)], opp, wk in games[:2]))
    out = {}
    for k, (name, pos, xs) in season.items():
        if k in acc:
            g = acc[k][2]
            wsum = sum(w for w, _, _ in g)
            out[k] = [name, pos, sum(w * o for w, o, _ in g) / wsum, sum(o for _, o, _ in g) / len(g), any(r for _, _, r in g)]
        else:
            avg = sum(xs) / len(xs)
            out[k] = [name, pos, avg, avg, False]
    return out, games


def main():
    a = sys.argv[1:]
    if len(a) < 2:
        print(__doc__); sys.exit(1)
    path, n, apply = a[0], int(a[1]), "--apply" in a
    src = a[a.index("--csv") + 1] if "--csv" in a else os.path.join(tempfile.gettempdir(), "stats_player_week_2026.csv")
    if "--csv" not in a and not os.path.exists(src):
        subprocess.run(["curl", "-sSfL", "--max-time", "120", "-o", src, URL], check=True)
    rows = [r for r in csv.DictReader(open(src, encoding="utf-8")) if r["season_type"] == "REG"]
    for r in rows:
        r["team"] = TEAM_FIX.get(r["team"], r["team"])
    page = open(path, encoding="utf-8").read()
    W = read_weeks(page)[str(n)]
    starters, inj, fillin = W.get("STARTERS") or {}, W.get("INJ") or {}, W.get("FILLIN") or {}
    kick = {}
    for g in W["GAMES"]:
        kick[g[0]] = kick[g[1]] = kick_ms(g[2])
    # players a fill-in is starting for ("Starting for X (injury, out)") are out this week
    out_names = {norm(m.group(1)) for v in fillin.values() for m in [re.match(r"Starting for (.+?) \(", v)] if m}
    out_names |= {norm(k) for k, v in inj.items() if v and v[0] == "O"}
    now = float(os.environ.get("LINEUP_NOW_MS", time.time() * 1000))  # LINEUP_NOW_MS: test against a past week
    swaps, new = [], json.loads(json.dumps(starters))
    for team, s in starters.items():
        if team not in kick or kick[team] <= now:
            continue
        u, games = usage(rows, team, n)
        if not games:
            continue
        # RBs stay with the depth chart: injuries and committees make recent carries a poor guide
        slots = [("WR", s[2][i], (lambda i: lambda v: new[team][2].__setitem__(i, v))(i)) for i in range(3)]
        slots += [("TE", s[3], lambda v: new[team].__setitem__(3, v))]
        listed = {norm(x) for x in [s[1], *s[2], s[3]]}
        for pos, name, put in slots:
            if name in fillin:
                continue
            mine = u.get(norm(name), [name, pos, 0.0, 0.0, False])
            pool = [d for k, d in u.items() if d[1] == pos and k not in listed and d[4]
                    and not (inj.get(d[0]) and inj[d[0]][0] == "O") and norm(d[0]) not in out_names]
            if not pool:
                continue
            best = max(pool, key=lambda d: d[2])
            if best[2] >= RATIO * mine[2] and best[3] - mine[3] >= MIN_GAP:
                why = "targets" if pos != "RB" else "carries + targets"
                swaps.append(f"{team} {pos}: {name} ({mine[3]:.1f} {why}/g) -> {best[0]} ({best[3]:.1f}/g), last {len(games)} games")
                put(best[0])
                listed.discard(norm(name)); listed.add(norm(best[0]))
    print("\n".join(swaps) or "No usage swaps.")
    if apply and swaps:
        s0, s1 = week_block(page, n)
        block = page[s0:s1]
        m = re.search(r"\nSTARTERS:\{\n.*?\]\},\n", block, re.S)
        assert m, "STARTERS literal not found"
        lit = "\nSTARTERS:{\n" + ",\n".join(f"{t}:" + json.dumps(v, ensure_ascii=False, separators=(",", ":")) for t, v in new.items()) + "},\n"
        block = block[:m.start()] + lit + block[m.end():]
        page = page[:s0] + block + page[s1:]
        open(path, "w", encoding="utf-8").write(page)
        print(f"Week {n}: {len(swaps)} starter(s) updated.")


if __name__ == "__main__":
    main()
