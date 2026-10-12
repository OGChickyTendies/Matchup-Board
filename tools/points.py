#!/usr/bin/env python3
"""Fill fantasy points for finished games on the Matchup Board page.

Usage: python3 tools/points.py <page.html> [--csv stats.csv]

Reads `const WEEKS={...}` from the page, downloads nflverse's 2026 weekly player
stats (github.com/nflverse/nflverse-data, release "stats_player"), and for every
non-preview week updates the `FP:{...}` line: starter name -> [standard pts, PPR pts,
stat line] and a `BOX:{...}` line: starter name -> box score (the player dialog). Half PPR is
computed on the page as the average of the two.

Only games that are over (kickoff + 4 hours has passed) and already present in the
stats file are scored. A starter whose team has stats but who has no row of his own
gets [0, 0, "No stats recorded"].

Backfilled weeks (backfill:true) with an empty STARTERS get a lineup built from who
actually played: the QB with the most pass attempts, the RB with the most carries,
the three WRs and the TE with the most targets.

Needs node (to read the WEEKS literal) and curl (the download goes through the proxy).
"""
import csv, json, re, subprocess, sys, tempfile, os, time
from datetime import datetime, timezone

URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2026.csv"
TEAM_FIX = {"LA": "LAR"}  # nflverse code -> page code
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm(name):
    n = name.lower().replace(".", "").replace("'", "").replace("’", "").replace("-", " ")
    n = SUFFIX.sub("", n)
    return " ".join(n.split())


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def stat_line(r):
    i = lambda k: int(num(r.get(k)))
    parts = []
    if i("attempts"):
        p = f"{i('completions')}/{i('attempts')}, {i('passing_yards')} pass yds"
        if i("passing_tds"): p += f", {i('passing_tds')} TD"
        if i("passing_interceptions"): p += f", {i('passing_interceptions')} INT"
        parts.append(p)
    if i("carries"):
        p = f"{i('carries')} car, {i('rushing_yards')} rush yds"
        if i("rushing_tds"): p += f", {i('rushing_tds')} TD"
        parts.append(p)
    if i("targets") or i("receptions"):
        p = f"{i('receptions')}/{i('targets')} rec, {i('receiving_yards')} yds"
        if i("receiving_tds"): p += f", {i('receiving_tds')} TD"
        parts.append(p)
    fl = i("rushing_fumbles_lost") + i("receiving_fumbles_lost") + i("sack_fumbles_lost")
    if fl: parts.append(f"{fl} fumble{'s' if fl > 1 else ''} lost")
    two = i("passing_2pt_conversions") + i("rushing_2pt_conversions") + i("receiving_2pt_conversions")
    if two: parts.append(f"{two} two-pt")
    if i("special_teams_tds"): parts.append(f"{i('special_teams_tds')} return TD")
    return " · ".join(parts) or "No stats recorded"


BOXMAP = [("pass_cmp", "completions"), ("pass_att", "attempts"), ("pass_yd", "passing_yards"),
          ("pass_td", "passing_tds"), ("pass_int", "passing_interceptions"), ("pass_sack", "sacks_suffered"),
          ("rush_att", "carries"), ("rush_yd", "rushing_yards"), ("rush_td", "rushing_tds"), ("rush_fd", "rushing_first_downs"),
          ("rec_tgt", "targets"), ("rec", "receptions"), ("rec_yd", "receiving_yards"), ("rec_td", "receiving_tds"),
          ("pass_2pt", "passing_2pt_conversions"), ("rush_2pt", "rushing_2pt_conversions"),
          ("rec_2pt", "receiving_2pt_conversions"), ("st_td", "special_teams_tds")]


def box_row(r):
    """Box score for the page's player dialog: page stat key -> value, nonzero values only."""
    b = {}
    for k, col in BOXMAP:
        v = num(r.get(col))
        if v:
            b[k] = int(v) if v.is_integer() else round(v, 1)
    fl = num(r.get("rushing_fumbles_lost")) + num(r.get("receiving_fumbles_lost")) + num(r.get("sack_fumbles_lost"))
    if fl:
        b["fum_lost"] = int(fl)
    if num(r.get("targets")):
        b["rec_yac"] = int(num(r.get("receiving_yards_after_catch")))
        b["tgt_share"] = round(num(r.get("target_share")), 3)
    return b


def set_line(block, key, value):
    """Replace or add the one-line `KEY:{...},` field right after the week's header line."""
    lit = f"{key}:" + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + ","
    pat = re.compile(r"\n%s:\{[^\n]*\},(?=\n)" % key)
    if pat.search(block):
        return pat.sub(lambda _: "\n" + lit, block, count=1)
    nl = block.index("\n")
    return block[:nl] + "\n" + lit + block[nl:]


def read_weeks(page):
    m = re.search(r"const WEEKS=(\{.*?\n\});\n", page, re.S)
    assert m, "WEEKS not found"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write("const WEEKS=" + m.group(1) + ";process.stdout.write(JSON.stringify(WEEKS));")
        js = f.name
    try:
        out = subprocess.run(["node", js], capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(js)
    return json.loads(out)


def week_block(page, n):
    """(start, end) of week n's entry inside WEEKS: from '\\n<n>:{' to the next week or the closing '};'."""
    s = page.index("const WEEKS=")
    m = re.compile(r"\n%d:\{" % n).search(page, s)
    assert m, f"week {n} not found"
    nxt = re.compile(r"\n\d+:\{|\n\};\n").search(page, m.end())
    return m.start() + 1, nxt.start()


def kick_ms(iso):
    if len(iso) == 10:
        iso += "T12:00:00Z"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000


def build_lineup(rows):
    by = lambda pos, key: sorted([r for r in rows if r["position"] == pos], key=lambda r: -num(r[key]))
    qb = by("QB", "attempts")[:1]
    rb = by("RB", "carries")[:1]
    wr = by("WR", "targets")[:3]
    te = by("TE", "targets")[:1]
    if not (qb and rb and len(wr) == 3 and te):
        return None
    n = lambda r: r["player_display_name"]
    return [n(qb[0]), n(rb[0]), [n(x) for x in wr], n(te[0])]


def main():
    args = sys.argv[1:]
    path = args[0]
    src = args[args.index("--csv") + 1] if "--csv" in args else None
    if not src:
        src = os.path.join(tempfile.gettempdir(), "stats_player_week_2026.csv")
        subprocess.run(["curl", "-sSfL", "--max-time", "120", "-o", src, URL], check=True)
    rows = [r for r in csv.DictReader(open(src, encoding="utf-8")) if r["season_type"] == "REG"]
    for r in rows:
        r["team"] = TEAM_FIX.get(r["team"], r["team"])

    page = open(path, encoding="utf-8").read()
    weeks = read_weeks(page)
    now = time.time() * 1000
    report = []
    for n in sorted(weeks, key=int):
        W = weeks[n]
        if W.get("status") == "preview":
            continue
        wk = [r for r in rows if r["week"] == str(n)]
        teams_with_stats = {r["team"] for r in wk}
        starters = W.get("STARTERS") or {}
        made_lineups = False
        if W.get("backfill") and not starters:
            for g in W["GAMES"]:
                for t in g[:2]:
                    lu = build_lineup([r for r in wk if r["team"] == t])
                    if lu:
                        starters[t] = lu
            made_lineups = bool(starters)
        fp = dict(W.get("FP") or {})  # keep entries already there for games not in this stats file yet
        box = dict(W.get("BOX") or {})
        done = 0
        for g in W["GAMES"]:
            a, h, iso = g[0], g[1], g[2]
            if kick_ms(iso) + 4 * 3600e3 > now:
                continue
            if a not in teams_with_stats or h not in teams_with_stats:
                continue
            done += 1
            for t in (a, h):
                s = starters.get(t)
                if not s:
                    continue
                trows = [r for r in wk if r["team"] == t]
                idx = {norm(r["player_display_name"]): r for r in trows}
                for name in [s[0], s[1], *s[2], s[3]]:
                    r = idx.get(norm(name))
                    if r is None:  # fall back to a unique last-name match on the team
                        last = norm(name).split()[-1]
                        c = [x for k, x in idx.items() if k.split()[-1] == last]
                        r = c[0] if len(c) == 1 else None
                    if r is None:
                        fp[name] = [0, 0, "No stats recorded"]
                        box[name] = {"dnp": 1}
                    else:
                        fp[name] = [round(num(r["fantasy_points"]), 2), round(num(r["fantasy_points_ppr"]), 2), stat_line(r)]
                        box[name] = box_row(r)
        if not fp and not made_lineups:
            continue
        s0, s1 = week_block(page, int(n))
        block = page[s0:s1]
        block = re.sub(r"\nFP:\{[^\n]*\},(?=\n)", "", block)
        if box:
            block = set_line(block, "BOX", box)
        if made_lineups:
            lit = "STARTERS:{\n" + ",\n".join(f"{t}:{json.dumps(v, ensure_ascii=False)}" for t, v in starters.items()) + "},"
            assert block.count("STARTERS:{},") == 1, f"week {n}: STARTERS literal not found"
            block = block.replace("STARTERS:{},", lit, 1)
        first_nl = block.index("\n")
        block = block[:first_nl] + "\nFP:" + json.dumps(fp, ensure_ascii=False, separators=(",", ":")) + "," + block[first_nl:]
        page = page[:s0] + block + page[s1:]
        report.append(f"Week {n}: {done} game(s) scored, {len(fp)} starters" + (", lineups built from who played" if made_lineups else ""))
    open(path, "w", encoding="utf-8").write(page)
    print("\n".join(report) or "No finished games with stats yet.")


if __name__ == "__main__":
    main()
