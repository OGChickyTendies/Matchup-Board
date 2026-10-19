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

For finished games it also writes `ALT:{...}`: listed starter -> teammates at his position who
outscored him (PPR), so the page can say "Outscored by ..." when the depth chart picked the
wrong player. It also writes each week's `VPOS:{...}` line: how every defense has done against each
position (QB, RB, WR, TE) in the games played before that week. See vpos_for_week().

Needs node (to read the WEEKS literal) and curl (the download goes through the proxy).
"""
import csv, json, re, subprocess, sys, tempfile, os, time
from datetime import datetime, timezone

URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2026.csv"
PREV_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2025.csv"  # last season: prior for defense-vs-position
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


ALT_MIN = 5.0  # flag a passed-over teammate only when he outscored the listed starter by this many PPR points
VPOS_POS = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE"}
VPOS_KEYS = ("QB", "RB", "WR", "TE", "WR1", "WR2", "WR3")
VPOS_PRIOR_OFF = 1.0   # offense baseline: its other games plus this many league-average games
VPOS_PRIOR_LAST = 6.0  # defense rating starts from last season's rating, counted as this many games
USE_LAST_SEASON = False  # Oct 19, 2026: Roxane asked for 2026-only defense ranks (no 2025 blend)
VPOS_PRIOR_DEF = 2.0   # without last season's data: shrink toward average as if 2 average games
# Backtest on 2026 Weeks 2-5 (Spearman correlation of the pre-week rank with PPR actually allowed):
# team pass/rush yards 0.00, this season's adjusted rating alone 0.09, blended with 2025 x6 games 0.17.


def vpos_yards(r, pos):
    if pos == "QB":
        return num(r.get("passing_yards"))
    if pos == "RB":
        return num(r.get("rushing_yards")) + num(r.get("receiving_yards"))
    return num(r.get("receiving_yards"))


def vpos_for_week(rows, n, last=None):
    """Defense vs position going into week n, from every game played in weeks before n.

    For each team-game, add up the PPR points and yards scored by the offense's QBs, RBs,
    WRs and TEs. A defense's rating for a position is opponent-adjusted: in each game, how
    many PPR points that position scored above or below what the same offense usually scores
    there (its average in its other games, padded with one league-average game). Those are
    averaged together with last season's rating for that defense (`last`, counted as
    VPOS_PRIOR_LAST games), so early weeks lean on last season and later weeks on this one;
    without `last`, shrunk toward zero as if it had two average games. Rank 1 = toughest
    (allows the least relative to expectation), 32 = softest. Returns None before any games.
    Output per defense and position: [rank, yards/g allowed, PPR pts/g allowed, adjusted pts/g, games];
    each entry adds [TDs allowed/g (RB: rushing TDs by RBs; QB/WR/TE: passing TDs), PPR-only rank,
    TD rank] and its rank blends the two (see td_blend).
    Receivers are also rated by role, WR1 / WR2 / WR3: in each game the offense's receivers are
    ordered by their season targets coming into that game (in a team's first game, by that game's
    targets), so a defense that shuts down No. 1 receivers but leaks to slot/No. 3 options shows it.
    """
    prior = [r for r in rows if int(r["week"]) < n]
    if not prior:
        return None
    tg = {}  # (game_id, offense) -> {"def": team, pos: [pts, yds]}
    for r in prior:
        key = (r["game_id"], r["team"])
        g = tg.setdefault(key, {"def": r["opponent_team"], "rb_rtd": 0.0, "ptd": 0.0, **{p: [0.0, 0.0] for p in VPOS_KEYS}})
        pos = VPOS_POS.get(r["position"])
        if pos:
            g[pos][0] += num(r.get("fantasy_points_ppr"))
            g[pos][1] += vpos_yards(r, pos)
        if pos == "RB":
            g["rb_rtd"] += num(r.get("rushing_tds"))
        g["ptd"] += num(r.get("passing_tds"))
    # receiver roles: order each team's WRs in each game by season targets before that game
    season_tg = {}  # (team, week) -> {player: targets in earlier weeks}
    wrs = {}
    for r in prior:
        if r["position"] == "WR":
            wrs.setdefault((r["game_id"], r["team"]), []).append(r)
    for (gid, off), rs in wrs.items():
        wk = int(rs[0]["week"])
        before = season_tg.get((off, wk))
        if before is None:
            before = {}
            for r in prior:
                if r["team"] == off and r["position"] == "WR" and int(r["week"]) < wk:
                    before[r["player_display_name"]] = before.get(r["player_display_name"], 0) + num(r["targets"])
            season_tg[(off, wk)] = before
        rs = sorted(rs, key=lambda r: (-before.get(r["player_display_name"], 0), -num(r["targets"])) if before else (-num(r["targets"]),))
        for i, r in enumerate(rs[:3]):
            tg[(gid, off)][f"WR{i + 1}"] = [num(r.get("fantasy_points_ppr")), num(r.get("receiving_yards"))]
    out = {}
    for pos in VPOS_KEYS:
        league = sum(g[pos][0] for g in tg.values()) / len(tg)
        by_off = {}
        for (gid, off), g in tg.items():
            by_off.setdefault(off, []).append((gid, g[pos][0]))
        acc = {}
        for (gid, off), g in tg.items():
            others = [p for x, p in by_off[off] if x != gid]
            base = (sum(others) + league * VPOS_PRIOR_OFF) / (len(others) + VPOS_PRIOR_OFF)
            a = acc.setdefault(g["def"], [0.0, 0.0, 0.0, 0])
            a[0] += g[pos][0] - base
            a[1] += g[pos][0]
            a[2] += g[pos][1]
            a[3] += 1
        def blend(d, a):
            lp = (last or {}).get(d, {}).get(pos)
            if lp is not None:
                return (a[0] + lp[3] * VPOS_PRIOR_LAST) / (a[3] + VPOS_PRIOR_LAST)
            return a[0] / (a[3] + VPOS_PRIOR_DEF)
        rated = {d: (blend(d, a), a) for d, a in acc.items()}
        order = sorted(rated, key=lambda d: (rated[d][0], rated[d][1][1] / rated[d][1][3]))
        for i, d in enumerate(order):
            adj, a = rated[d]
            out.setdefault(d, {})[pos] = [i + 1, round(a[2] / a[3], 1), round(a[1] / a[3], 1), round(adj, 1), a[3]]
    td_blend(tg, out, "rb_rtd", ("RB",), RB_TD_WEIGHT)
    td_blend(tg, out, "ptd", ("QB", "WR", "TE", "WR1", "WR2", "WR3"), PASS_TD_WEIGHT)
    return {"thru": max(int(r["week"]) for r in prior), "t": dict(sorted(out.items()))}


RB_TD_WEIGHT = 1 / 3    # share of the RB rank that comes from rushing TDs allowed to RBs
PASS_TD_WEIGHT = 1 / 4  # share of the QB / WR / TE ranks that comes from passing TDs allowed
# Backtest on 2026 Weeks 2-5 (Spearman of the pre-week rank with PPR allowed to the position that week):
# RB 0.09 -> 0.11 with rushing TDs at 1/3. Passing TDs at 1/4: QB 0.15 -> 0.16, WR 0.12 -> 0.13,
# TE 0.30 -> 0.26 (small samples; TDs are noisy and already count in PPR, so their weight stays light).


def td_blend(tg, out, field, keys, weight):
    """Fold touchdowns allowed into each defense's rank for the given positions.

    The PPR rating already counts touchdowns, but the rank adds a separate touchdown rank:
    `field` per game (RB rushing TDs, or passing TDs for QB/WR/TE), fewest = 1, ties broken
    by the yards allowed to the position. New rank = re-ranked (1 - weight) x PPR rank +
    weight x TD rank. Each blended entry gains [5] TDs allowed per game, [6] the PPR-only
    rank and [7] the TD rank.
    """
    td = {}
    for g in tg.values():
        t = td.setdefault(g["def"], [0.0, 0])
        t[0] += g[field]
        t[1] += 1
    tdpg = {d: v[0] / v[1] for d, v in td.items()}
    for key in keys:
        ent = {d: v[key] for d, v in out.items() if key in v and d in tdpg}
        if not ent:
            continue
        td_rank = {d: i + 1 for i, d in enumerate(sorted(ent, key=lambda d: (tdpg[d], ent[d][1])))}
        score = {d: (1 - weight) * ent[d][0] + weight * td_rank[d] for d in ent}
        for i, d in enumerate(sorted(ent, key=lambda d: (score[d], ent[d][0]))):
            e = ent[d]
            out[d][key] = [i + 1, *e[1:5], round(tdpg[d], 2), e[0], td_rank[d]]


def wr_roles(rows, n, starters):
    """Listed WRs in role order (WR1, WR2, WR3) for week n: by season targets before week n,
    keeping the listed order for ties and for players without targets yet."""
    tg = {}
    for r in rows:
        if int(r["week"]) < n:
            k = (r["team"], norm(r["player_display_name"]))
            tg[k] = tg.get(k, 0) + num(r["targets"])
    return {t: sorted(s[2], key=lambda x: -tg.get((t, norm(x)), 0)) for t, s in starters.items()}


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
        r["opponent_team"] = TEAM_FIX.get(r["opponent_team"], r["opponent_team"])

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
        alt = dict(W.get("ALT") or {})
        oth = dict(W.get("OTH") or {})  # team -> non-starters at QB/RB/WR/TE who scored any fantasy points
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
                # teammates the depth chart passed over who outscored a listed starter at the same position
                listed = {"QB": [s[0]], "RB": [s[1]], "WR": list(s[2]), "TE": [s[3]]}
                lnorm = {norm(x) for x in [s[0], s[1], *s[2], s[3]]}
                for pos, names in listed.items():
                    for nm in names:
                        alt.pop(nm, None)
                    low = min(names, key=lambda x: fp.get(x, [0, 0])[1])
                    lowp = fp.get(low, [0, 0])[1]
                    better = sorted((r for r in trows if VPOS_POS.get(r["position"]) == pos and norm(r["player_display_name"]) not in lnorm
                                     and num(r["fantasy_points_ppr"]) >= lowp + ALT_MIN), key=lambda r: -num(r["fantasy_points_ppr"]))[:2]
                    if better:
                        alt[low] = [[r["player_display_name"], round(num(r["fantasy_points"]), 2), round(num(r["fantasy_points_ppr"]), 2)] for r in better]
                others = [r for r in trows if VPOS_POS.get(r["position"]) and norm(r["player_display_name"]) not in lnorm
                          and (num(r["fantasy_points"]) or num(r["fantasy_points_ppr"]))]
                others.sort(key=lambda r: -num(r["fantasy_points_ppr"]))
                if others:
                    oth[t] = [[r["player_display_name"], VPOS_POS[r["position"]], round(num(r["fantasy_points"]), 2),
                               round(num(r["fantasy_points_ppr"]), 2), stat_line(r)] for r in others]
                else:
                    oth.pop(t, None)
        if not fp and not made_lineups:
            continue
        s0, s1 = week_block(page, int(n))
        block = page[s0:s1]
        block = re.sub(r"\nFP:\{[^\n]*\},(?=\n)", "", block)
        if box:
            block = set_line(block, "BOX", box)
        if alt:
            block = set_line(block, "ALT", alt)
        if oth:
            block = set_line(block, "OTH", oth)
        if made_lineups:
            lit = "STARTERS:{\n" + ",\n".join(f"{t}:{json.dumps(v, ensure_ascii=False)}" for t, v in starters.items()) + "},"
            assert block.count("STARTERS:{},") == 1, f"week {n}: STARTERS literal not found"
            block = block.replace("STARTERS:{},", lit, 1)
        first_nl = block.index("\n")
        block = block[:first_nl] + "\nFP:" + json.dumps(fp, ensure_ascii=False, separators=(",", ":")) + "," + block[first_nl:]
        page = page[:s0] + block + page[s1:]
        report.append(f"Week {n}: {done} game(s) scored, {len(fp)} starters" + (", lineups built from who played" if made_lineups else ""))
    # defense vs position for every week, the preview week included (it only uses earlier weeks)
    last = None
    try:
        if not USE_LAST_SEASON:
            raise RuntimeError("off")
        prev = os.path.join(tempfile.gettempdir(), "stats_player_week_2025.csv")
        if not os.path.exists(prev):
            subprocess.run(["curl", "-sSfL", "--max-time", "180", "-o", prev, PREV_URL], check=True)
        prows = [r for r in csv.DictReader(open(prev, encoding="utf-8")) if r["season_type"] == "REG"]
        for r in prows:
            r["team"] = TEAM_FIX.get(r["team"], r["team"])
            r["opponent_team"] = TEAM_FIX.get(r["opponent_team"], r["opponent_team"])
        global VPOS_PRIOR_DEF
        keep, VPOS_PRIOR_DEF = VPOS_PRIOR_DEF, 0.0  # last season's own rating, unshrunk
        last = vpos_for_week(prows, 99)["t"]
        VPOS_PRIOR_DEF = keep
    except Exception as e:  # carry on without the prior
        if USE_LAST_SEASON:
            report.append(f"Defense vs position: no 2025 prior ({e}); using this season only")
    vrep = []
    # last week that is basically complete (most teams have played), so Week 1's hindsight rating uses whole weeks
    teams_by_week = {}
    for r in rows:
        teams_by_week.setdefault(int(r["week"]), set()).add(r["team"])
    latest = max((w for w, t in teams_by_week.items() if len(t) >= 24), default=0)
    for n in sorted(weeks, key=int):
        v = vpos_for_week(rows, int(n), last)
        hindsight = False
        if not v and latest:
            # Week 1 has no earlier games: rate it with everything played so far (hindsight, Week 1 included)
            v, hindsight = vpos_for_week(rows, latest + 1, last), True
        if not v:
            continue
        v["wr"] = wr_roles(rows, latest + 1 if hindsight else int(n), weeks[n].get("STARTERS") or {})
        if hindsight:
            v["hindsight"] = True
        s0, s1 = week_block(page, int(n))
        page = page[:s0] + set_line(page[s0:s1], "VPOS", v) + page[s1:]
        vrep.append(f"{n} (thru Wk {v['thru']})")
    if vrep:
        report.append("Defense vs position: weeks " + ", ".join(vrep))
    open(path, "w", encoding="utf-8").write(page)
    print("\n".join(report) or "No finished games with stats yet.")


if __name__ == "__main__":
    main()
