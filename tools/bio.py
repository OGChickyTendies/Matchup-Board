#!/usr/bin/env python3
"""Birthdays and hometowns for the Matchup Sheet's birthday-cake and house icons.

The page keeps one line per player in `const BIO={...};` (right after WEEKS):
    "Player Name":["YYYY-MM-DD","City, ST",lat,lon]
  - birth date from nflverse's players file (filled automatically for everyone on the board),
  - hometown = the birthplace on the player's ESPN profile, with coordinates from
    tools/us_cities.csv (US places only; foreign birthplaces keep the text with null coordinates),
  - an entry with only the date means the hometown hasn't been looked up yet.
The page draws a cake when a game falls on a player's birthday and a house when the game's
stadium is within 25 miles of his hometown (international games never count).

Usage:
  python3 tools/bio.py <page.html>                      birth dates for every player on the board
  python3 tools/bio.py missing <page.html>              players with no hometown yet, one per line:
                                                        name|ESPN profile URL to WebFetch
  python3 tools/bio.py apply <page.html> <towns.txt>    add hometowns from lines "Name|City, ST"
                                                        (as the ESPN profile's displayBirthPlace gives it;
                                                        "Name|NONE" records that ESPN lists none)

Needs node (to read WEEKS) and curl (nflverse download through the proxy).
"""
import csv, json, math, os, re, subprocess, sys, tempfile

PLAYERS_URL = "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"
ESPN = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{}"
HERE = os.path.dirname(os.path.abspath(__file__))
CITIES = os.path.join(HERE, "us_cities.csv")  # github.com/kelvins/US-Cities-Database (MIT)
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")
US = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
         "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split())
# places ESPN uses that the city table lacks (suburbs, regions): approximate center
EXTRA = {
    ("cleveland heights", "OH"): (41.520, -81.556), ("homewood", "AL"): (33.472, -86.801),
    ("lyndhurst", "OH"): (41.520, -81.489), ("hoover", "AL"): (33.405, -86.811),
    ("lee's summit", "MO"): (38.911, -94.382), ("lake barrington", "IL"): (42.212, -88.153),
    ("coeur d'alene", "ID"): (47.678, -116.780), ("winston-salem", "NC"): (36.100, -80.244),
    ("johns creek", "GA"): (34.029, -84.199), ("anaheim hills", "CA"): (33.840, -117.760),
    ("highlands ranch", "CO"): (39.554, -104.969), ("long island", "NY"): (40.790, -73.130),
    ("east palo alto", "CA"): (37.469, -122.141), ("weddington", "NC"): (35.022, -80.761),
    ("inland empire", "CA"): (34.106, -117.593), ("richmond heights", "FL"): (25.631, -80.369),
    ("shillington", "PA"): (40.308, -75.966), ("south hill", "WA"): (47.141, -122.270),
    ("plantation", "FL"): (26.127, -80.233), ("woodmere", "LA"): (29.858, -90.081),
}


def norm(name):
    n = name.lower().replace(".", "").replace("'", "").replace("’", "").replace("-", " ")
    return " ".join(SUFFIX.sub("", n).split())


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


def board_players(weeks):
    """Every player the page shows: starters and other scorers, with the teams he appears for."""
    out = {}
    for w in weeks.values():
        for t, s in (w.get("STARTERS") or {}).items():
            for x in [s[0], s[1], *s[2], s[3]]:
                out.setdefault(x, set()).add(t)
        for t, l in (w.get("OTH") or {}).items():
            for x in l:
                out.setdefault(x[0], set()).add(t)
    return out


def read_bio(page):
    m = re.search(r"\nconst BIO=\{\n(.*?)\};\n", page, re.S)
    if not m:
        return {}
    return json.loads("{" + m.group(1).rstrip().rstrip(",") + "}")


def write_bio(page, bio):
    body = "".join(json.dumps(k, ensure_ascii=False) + ":" + json.dumps(v, ensure_ascii=False, separators=(",", ":")) + ",\n"
                   for k, v in sorted(bio.items()))
    lit = "\nconst BIO={\n" + body + "};\n"
    if re.search(r"\nconst BIO=\{\n.*?\};\n", page, re.S):
        return re.sub(r"\nconst BIO=\{\n.*?\};\n", lambda _: lit, page, count=1, flags=re.S)
    m = re.search(r"const WEEKS=\{.*?\n\};\n", page, re.S)
    assert m, "WEEKS not found"
    return page[:m.end()] + lit.lstrip("\n") + page[m.end():]


def nflverse_players():
    path = os.path.join(tempfile.gettempdir(), "nflverse_players.csv")
    if not os.path.exists(path):
        subprocess.run(["curl", "-sSfL", "--max-time", "180", "-o", path, PLAYERS_URL], check=True)
    by = {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        by.setdefault(norm(r["display_name"]), []).append(r)
    return by


def match(by, name, teams):
    c = by.get(norm(name), [])
    c = [r for r in c if r["last_season"] >= "2025"] or c
    c = [r for r in c if r["latest_team"] in teams or (r["latest_team"] == "LA" and "LAR" in teams)] or c
    return c[0] if len(c) == 1 else None


def geocode(place):
    if not place or place == "NONE" or ", " not in place:
        return None
    city, st = place.rsplit(", ", 1)
    if st not in US:
        return None
    key = re.sub(r"^st\.? ", "saint ", re.sub(r"^ft\.? ", "fort ", re.sub(r"^mt\.? ", "mount ", city.lower().strip())))
    if (key, st) in EXTRA:
        return EXTRA[(key, st)]
    if (city.lower(), st) in EXTRA:
        return EXTRA[(city.lower(), st)]
    if not _CITY:
        for r in csv.DictReader(open(CITIES, encoding="utf-8")):
            _CITY.setdefault((r["CITY"].lower(), r["STATE_CODE"]), (round(float(r["LATITUDE"]), 3), round(float(r["LONGITUDE"]), 3)))
    return _CITY.get((key, st)) or _CITY.get((city.lower(), st))


_CITY = {}


def main():
    a = sys.argv[1:]
    cmd = a[0] if a[0] in ("missing", "apply") else "dob"
    path = a[1] if cmd != "dob" else a[0]
    page = open(path, encoding="utf-8").read()
    bio = read_bio(page)
    players = board_players(read_weeks(page))
    if cmd == "missing":
        by = nflverse_players()
        for name in sorted(players):
            if len(bio.get(name, [])) >= 2:
                continue
            r = match(by, name, players[name])
            eid = (r or {}).get("espn_id", "").split(".")[0]
            print(f"{name}|{ESPN.format(eid) if eid else 'no ESPN id'}")
        return
    if cmd == "apply":
        miss = []
        for line in open(a[2], encoding="utf-8"):
            if "|" not in line:
                continue
            name, place = [x.strip() for x in line.split("|", 1)]
            if name not in players:
                print(f"skipped (not on the board): {name}")
                continue
            d = (bio.get(name) or [None])[0]
            if place == "NONE":
                bio[name] = [d, None]
                continue
            ll = geocode(place)
            if ll is None and place.rsplit(", ", 1)[-1] in US:
                miss.append(f"{name} ({place})")
            bio[name] = [d, place, *(ll or (None, None))]
        open(path, "w", encoding="utf-8").write(write_bio(page, bio))
        print(f"Hometowns: {len(bio)} players on file" + (f"; no coordinates for {', '.join(miss)} (no house icon)" if miss else ""))
        return
    by = nflverse_players()
    added = 0
    for name, teams in players.items():
        r = match(by, name, teams)
        d = (r or {}).get("birth_date") or None
        cur = bio.get(name)
        if cur is None:
            bio[name] = [d]
            added += d is not None
        elif d and cur[0] != d:
            cur[0] = d
            added += 1
    open(path, "w", encoding="utf-8").write(write_bio(page, bio))
    need = sum(1 for n in players if len(bio.get(n, [])) < 2)
    print(f"Birthdays: {added} added or updated; {need} player(s) still need a hometown (tools/bio.py missing)")


if __name__ == "__main__":
    main()
