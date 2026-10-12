#!/usr/bin/env python3
"""Final scores from Sleeper for the Matchup Sheet page.

The shell can't reach Sleeper, so the scheduled post-game run reads
https://api.sleeper.com/scores/nfl/regular/2026/<week> with WebFetch, writes the finished
games into a small JSON file, and this script writes them into the page.

  python3 tools/sleeper.py need <page.html>
      Prints the live week number and, for each of its games, the kickoff (UTC) and the
      final score already on the page (null if none yet).

  python3 tools/sleeper.py apply <page.html> <scores.json>
      scores.json: {"week": 5, "games": [{"away": "NYG", "home": "WAS", "away_score": 27,
                    "home_score": 27, "ot": true}]}
      List only games Sleeper marks "complete". Writes SCORES[home] = [away, home] (plus
      "OT" for overtime) in that week. Prints what changed; exits 2 and writes nothing if
      a game isn't on that week's schedule or a score isn't a whole number.

Box scores and fantasy points come from tools/points.py (nflverse), not from here.
"""
import json, os, re, subprocess, sys, tempfile


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
    s = page.index("const WEEKS=")
    m = re.compile(r"\n%d:\{" % n).search(page, s)
    assert m, f"week {n} not found"
    nxt = re.compile(r"\n\d+:\{|\n\};\n").search(page, m.end())
    return m.start() + 1, nxt.start()


def set_line(block, key, value):
    lit = f"{key}:" + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + ","
    pat = re.compile(r"\n%s:\{[^\n]*\},(?=\n)" % key)
    if pat.search(block):
        return pat.sub(lambda _: "\n" + lit, block, count=1)
    nl = block.index("\n")
    return block[:nl] + "\n" + lit + block[nl:]


def live_week(weeks):
    live = [int(n) for n, w in weeks.items() if w.get("status") == "live"]
    assert len(live) == 1, f"expected one live week, found {live}"
    return live[0]


def main():
    a = sys.argv[1:]
    if len(a) < 2 or a[0] not in ("need", "apply"):
        print(__doc__); sys.exit(1)
    path = a[1]
    page = open(path, encoding="utf-8").read()
    weeks = read_weeks(page)
    if a[0] == "need":
        n = live_week(weeks)
        W = weeks[str(n)]
        sc = W.get("SCORES") or {}
        print(json.dumps({"week": n, "games": [{"away": g[0], "home": g[1], "kickoff": g[2], "final": sc.get(g[1])} for g in W["GAMES"]]}, indent=1))
        return
    data = json.load(open(a[2], encoding="utf-8"))
    n = int(data["week"])
    W = weeks[str(n)]
    pairs = {(g[0], g[1]) for g in W["GAMES"]}
    scores = dict(W.get("SCORES") or {})
    errors, changed = [], []
    for g in data.get("games", []):
        aw, hm = g.get("away"), g.get("home")
        if (aw, hm) not in pairs:
            errors.append(f"{aw} @ {hm} is not a week {n} game"); continue
        x, y = g.get("away_score"), g.get("home_score")
        if not all(isinstance(v, (int, float)) and float(v).is_integer() and v >= 0 for v in (x, y)):
            errors.append(f"{aw} @ {hm}: bad score {x!r}-{y!r}"); continue
        val = [int(x), int(y)] + (["OT"] if g.get("ot") else [])
        if scores.get(hm) != val:
            scores[hm] = val
            changed.append(f"{aw} {int(x)}, {hm} {int(y)}" + (" (OT)" if g.get("ot") else ""))
    if errors:
        print("NOT WRITTEN:\n- " + "\n- ".join(errors)); sys.exit(2)
    if not changed:
        print("No new final scores."); return
    s0, s1 = week_block(page, n)
    page = page[:s0] + set_line(page[s0:s1], "SCORES", scores) + page[s1:]
    open(path, "w", encoding="utf-8").write(page)
    print(f"Week {n} finals added: " + "; ".join(changed))


if __name__ == "__main__":
    main()
