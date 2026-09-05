#!/usr/bin/env python3
"""Pull NASCAR Cup data from the Motorsport Stats Core API and regenerate the
game datasets.

Auth and endpoints follow the MSS Core Api Implementation/Reference guides
v2.0.0.1 (~/mss-docs): base https://api.motorsportstats.com/core/2.0.0/, every
request a GET with the client key in the `x-api-key` header. The key is read
from .env (MSS_API_KEY). Never commit .env.

Navigation used here:
  series?size=N              paginated listing (finds the NASCAR Cup uuid)
  season/ofSeries/{u}?year=Y season lookup by year
  event/ofSeason/{u}         events in a season
  race/ofEvent/{u}           races in an event
  session/ofRace/{u}         sessions of a race (the Race session holds results)
  sessionClassification/ofSession/{u}  end-of-session classification rows

Commands (all output goes to build/mss_pull/, NEVER to data/ — diff a pull
against data/ and copy it over only after review):
  test                     one call for the current NASCAR Cup season; prints
                           HTTP status and row count
  season-results YEAR      raw winner/start rows for one season (the input
                           every dataset below is derived from)
  driver-pool FROM TO      Cross-Era driverPool records (default 1983..last
                           complete season)
  org-seasons FROM TO      Cross-Era orgSeasons records (same range)
  streak-pool              Streak pool (career totals, clean 1972+ window)
  streak-refresh           refresh w for the existing data/streak_pool.json
                           drivers from race-winner classifications
                           (1973..current, cached per season); exits 2 if any
                           driver's count decreases
  grid-facts               Daily Grid facts (drivers, orgs, tracks)

Derivation rules encoded from the shipped build's data-block comments:
  - tiers: LEGEND 13+, CHAMPION 7-12, RACE WINNER 4-6, CONTENDER 1-3, FIELD 0
  - clean window: the MSS pull is complete from 1972 on; a career total is
    "verified" (v/eligible for Streak) only when the entire recorded career
    sits inside that window (first season 1973 or later)
  - tracksWon and orgs are positive evidence only; rows lacking a venue or
    team are simply skipped, never guessed
  - org_seasons roster/seats and grid ft (full-time seasons) are derived
    mechanically below; before replacing data/, diff a regenerated file
    against the shipped one and reconcile any rule drift by hand
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "build" / "mss_pull"
CACHE_DIR = ROOT / "build" / "mss_cache"

API_BASE = "https://api.motorsportstats.com/core/2.0.0/"
NASCAR_CUP_UUID = "0f0f963a-d489-4b9a-8945-71d99bfabd62"  # "NASCAR Cup Series", verified via the series listing
PAGE_SIZE = 100

TIERS = [(13, "LEGEND"), (7, "CHAMPION"), (4, "RACE WINNER"), (1, "CONTENDER"), (0, "FIELD")]


def tier_for(wins):
    for lo, name in TIERS:
        if wins >= lo:
            return name
    return "FIELD"


def load_env():
    env = {}
    path = ROOT / ".env"
    if not path.exists():
        sys.exit(".env not found next to this repo root; it must define MSS_API_KEY")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not k.isidentifier():
            continue  # skip malformed lines (e.g. a stray pasted shell command)
        env.setdefault(k, v.strip().strip('"').strip("'"))
    if "MSS_API_KEY" not in env or not env["MSS_API_KEY"]:
        sys.exit("MSS_API_KEY missing from .env")
    return env


ENV = None


def api_key():
    global ENV
    if ENV is None:
        ENV = load_env()
    return ENV["MSS_API_KEY"]


def base():
    global ENV
    if ENV is None:
        ENV = load_env()
    return ENV.get("MSS_API_BASE", API_BASE)


def series_uuid():
    global ENV
    if ENV is None:
        ENV = load_env()
    return ENV.get("MSS_SERIES_UUID", NASCAR_CUP_UUID)


def get(path, params=None, retries=3):
    """One GET. Returns (status_code, parsed_json_or_None)."""
    url = base() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": api_key()})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return e.code, None
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return 0, None


def get_all(path, params=None):
    """Follow page/size pagination; returns the concatenated content list."""
    out = []
    page = 0
    while True:
        p = dict(params or {})
        p.update({"size": PAGE_SIZE, "page": page})
        status, j = get(path, p)
        if status != 200 or j is None:
            raise RuntimeError("GET %s page %d -> HTTP %d" % (path, page, status))
        content = j.get("content", j if isinstance(j, list) else [])
        out.extend(content)
        total_pages = j.get("totalPages")
        page += 1
        if total_pages is None or page >= total_pages:
            break
    return out


def first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def ref_name(d, key):
    v = d.get(key)
    if isinstance(v, dict):
        return first(v, "name")
    return v if isinstance(v, str) else None


def season_for_year(year):
    """(status, season_dict_or_None) for the Cup season of a given year."""
    status, j = get("season/ofSeries/" + series_uuid(), {"year": year})
    if status != 200 or j is None:
        return status, None
    rows = j.get("content", [j] if j.get("uuid") else [])
    for s in rows:
        if str(s.get("year")) == str(year):
            return status, s
    return status, rows[0] if rows else None


def season_results(year):
    """All classification rows for a season's Race sessions.
    Returns {"year", "races": [{event, venue, rows: [...]}, ...]}."""
    status, season = season_for_year(year)
    if season is None:
        raise RuntimeError("no season for %d (HTTP %d)" % (year, status))
    events = get_all("event/ofSeason/" + season["uuid"])
    races_out = []
    for ev in events:
        venue = ref_name(ev, "venue")
        races = get_all("race/ofEvent/" + ev["uuid"])
        for race in races:
            sessions = get_all("session/ofRace/" + race["uuid"])
            race_sessions = [s for s in sessions if str(first(s, "type", "sessionType", default="")).lower() == "race"] or sessions
            for sess in race_sessions:
                rows = get_all("sessionClassification/ofSession/" + sess["uuid"])
                races_out.append({
                    "event": ev.get("name"), "venue": venue,
                    "session": sess.get("name"), "rows": rows,
                })
    return {"year": year, "season": season.get("uuid"), "races": races_out}


def row_driver(row):
    return ref_name(row, "driver") or first(row, "driverName")


def row_team(row):
    return ref_name(row, "team") or first(row, "teamName", "entrantName")


def row_position(row):
    p = first(row, "position", "finishPosition", "classifiedPosition")
    try:
        return int(p)
    except (TypeError, ValueError):
        return None


def iter_season_rows(res):
    for race in res["races"]:
        for row in race["rows"]:
            yield race, row


def write_out(name, obj):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_text(json.dumps(obj), encoding="utf-8")
    print("wrote %s (%d records)" % (path, len(obj) if isinstance(obj, list) else len(obj.get("drivers", []))))


def collect_seasons(y_from, y_to):
    out = {}
    for y in range(y_from, y_to + 1):
        print("season %d ..." % y, file=sys.stderr)
        out[y] = season_results(y)
    return out


def build_driver_pool(seasons):
    pool = []
    for y in sorted(seasons):
        res = seasons[y]
        per = {}
        n_races = len(res["races"])
        for race, row in iter_season_rows(res):
            drv = row_driver(row)
            if not drv:
                continue
            d = per.setdefault(drv, {"wins": 0, "starts": 0, "teams": {}})
            d["starts"] += 1
            team = row_team(row)
            if team:
                d["teams"][team] = d["teams"].get(team, 0) + 1
            if row_position(row) == 1:
                d["wins"] += 1
        for drv, d in sorted(per.items()):
            team = max(d["teams"], key=d["teams"].get) if d["teams"] else ""
            pool.append({
                "driver": drv, "year": y, "team": team, "wins": d["wins"],
                "tier": tier_for(d["wins"]), "starts": d["starts"], "races": n_races,
            })
    return pool


def build_org_seasons(seasons):
    def slug(s):
        return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-").replace("--", "-")
    orgs = []
    for y in sorted(seasons):
        res = seasons[y]
        n_races = len(res["races"])
        teams = {}
        for race, row in iter_season_rows(res):
            team, drv = row_team(row), row_driver(row)
            if not team or not drv:
                continue
            t = teams.setdefault(team, {})
            d = t.setdefault(drv, {"wins": 0, "starts": 0})
            d["starts"] += 1
            if row_position(row) == 1:
                d["wins"] += 1
        for team, drivers in sorted(teams.items()):
            # Roster: drivers with a meaningful campaign for the org (10+ starts
            # or a win). Orgs with zero rostered drivers or zero wins are skipped.
            roster = {d: v for d, v in drivers.items() if v["starts"] >= 10 or v["wins"] > 0}
            bench = sum(v["wins"] for v in roster.values())
            if not roster or bench == 0:
                continue
            roster_sorted = sorted(roster.items(), key=lambda kv: (-kv[1]["wins"], -kv[1]["starts"], kv[0]))
            orgs.append({
                "id": "%d-%s" % (y, slug(team)), "year": y, "org": team,
                "seats": len(roster_sorted), "benchmarkWins": bench,
                "band": "DYNASTY" if bench >= 12 else "OPEN",
                "note": "Won %d of %d races across %d cars." % (bench, n_races, len(roster_sorted)),
                "realRoster": [
                    {"driver": d, "wins": v["wins"], "tier": tier_for(v["wins"])}
                    for d, v in roster_sorted
                ],
            })
    return orgs


def build_streak_pool(seasons):
    careers = {}
    for y in sorted(seasons):
        for race, row in iter_season_rows(seasons[y]):
            drv = row_driver(row)
            if not drv:
                continue
            c = careers.setdefault(drv, {"w": 0, "y0": y, "y1": y})
            c["y0"] = min(c["y0"], y)
            c["y1"] = max(c["y1"], y)
            if row_position(row) == 1:
                c["w"] += 1
    # Clean window: only careers that start after 1972 are verified-complete.
    pool = [
        {"n": d, "w": c["w"], "y0": c["y0"], "y1": c["y1"]}
        for d, c in careers.items() if c["y0"] >= 1973 and c["w"] >= 1
    ]
    pool.sort(key=lambda r: (-r["w"], r["n"]))
    return pool


def build_grid_facts(seasons):
    orgs, tracks = [], []
    org_ix, track_ix = {}, {}

    def ix(table, index, name):
        if name not in index:
            index[name] = len(table)
            table.append(name)
        return index[name]

    drivers = {}
    for y in sorted(seasons):
        n_races = len(seasons[y]["races"])
        starts_this_year = {}
        for race, row in iter_season_rows(seasons[y]):
            drv = row_driver(row)
            if not drv:
                continue
            d = drivers.setdefault(drv, {
                "n": drv, "w": 0, "y0": y, "y1": y, "ft": 0,
                "wd": set(), "sd": set(), "o": set(), "t": set(),
            })
            d["y0"] = min(d["y0"], y)
            d["y1"] = max(d["y1"], y)
            d["sd"].add(y // 10 * 10)
            starts_this_year[drv] = starts_this_year.get(drv, 0) + 1
            team = row_team(row)
            if team:
                d["o"].add(ix(orgs, org_ix, team))
            if row_position(row) == 1:
                d["w"] += 1
                d["wd"].add(y // 10 * 10)
                venue = race.get("venue")
                if venue:
                    d["t"].add(ix(tracks, track_ix, venue))
        for drv, starts in starts_this_year.items():
            if n_races and starts >= n_races:
                drivers[drv]["ft"] += 1

    out = []
    for drv in sorted(drivers):
        d = drivers[drv]
        toks = drv.split()
        short = (toks[0][0] + ". " + " ".join(toks[1:])) if len(toks) > 1 else drv
        out.append({
            "n": d["n"], "d": short, "w": d["w"], "v": d["y0"] >= 1973,
            "y0": d["y0"], "y1": d["y1"], "ft": d["ft"],
            "wd": sorted(d["wd"]), "sd": sorted(d["sd"]),
            "o": sorted(d["o"]), "t": sorted(d["t"]),
        })
    return {"orgs": orgs, "tracks": tracks, "drivers": out}


def season_winners(year, refresh=False):
    """Winners of every championship race in a season, from race classifications.

    Returns {"year", "races": N_completed, "winners": [{driver, uuid, event}]}.
    One winner (position 1) per Race session; future/unclassified races are
    skipped. Cached per season under build/mss_cache/ — pass refresh=True (used
    for the current year) to refetch instead of trusting the cache.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / ("season_winners_%d.json" % year)
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    status, season = season_for_year(year)
    if season is None:
        raise RuntimeError("no season for %d (HTTP %d)" % (year, status))
    events = get_all("event/ofSeason/" + season["uuid"])

    def event_winners(ev):
        out = []
        seen = set()  # some events list the same session uuid twice
        for race in get_all("race/ofEvent/" + ev["uuid"]):
            for sref in race.get("sessions") or []:
                if sref.get("name") != "Race" or sref["uuid"] in seen:
                    continue
                seen.add(sref["uuid"])
                status, j = get("sessionClassification/ofSession/" + sref["uuid"])
                if status != 200 or not j:
                    continue  # not yet run / not yet classified
                for row in j.get("classificationDetails") or []:
                    if row_position(row) == 1:
                        drefs = row.get("drivers") or []
                        if drefs:
                            out.append({"driver": drefs[0].get("name"),
                                        "uuid": drefs[0].get("uuid"),
                                        "event": ev.get("name")})
        return out

    winners = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for evw in pool.map(event_winners, events):
            winners.extend(evw)
    res = {"year": year, "races": len(winners), "winners": winners}
    cache.write_text(json.dumps(res))
    return res


# Editor-specified canonical forms, copied from the CANON table in index.html
# (legal names, nickname forms, missing-period suffixes). Applied to MSS names
# before mechanical variant matching; never guessed.
CANON_FULL = {
    "Dale Arnold Jarrett": "Dale Jarrett",
    "Joseph Joe Frank Nemechek III": "Joe Nemechek",
    "Alan Dennis Kulwicki": "Alan Kulwicki",
    "Darrell Bubba Wallace Jr.": "Bubba Wallace",
    "Kenneth Dale Irwin Jr.": "Kenny Irwin Jr.",
    "Dale Earnhardt Jr": "Dale Earnhardt Jr.",
    "Martin Truex Jr": "Martin Truex Jr.",
    "Ricky Stenhouse Jr": "Ricky Stenhouse Jr.",
    "David Ray Boggs": "David Boggs",
    "Harold Bruce Jacobi": "Harold Jacobi",
}


def normws(s):
    """Whitespace-normalize the way index.html's normKey does: non-breaking
    spaces to spaces, runs collapsed, ends trimmed. MSS names carry nbsp."""
    import re
    return re.sub(r"\s+", " ", str(s).replace(" ", " ")).strip()


CANON_NORM = None


def name_variants(name):
    """Mechanical name variants for matching a pool name to an MSS name:
    normalized, periods stripped, middle names dropped, suffix normalized.
    Mirrors the canonical-name rules in index.html (no name is guessed)."""
    import re
    global CANON_NORM
    if CANON_NORM is None:
        CANON_NORM = {normws(k): v for k, v in CANON_FULL.items()}
    name = normws(name)
    name = CANON_NORM.get(name, name)
    n = re.sub(r"\s+", " ", str(name).replace(".", "")).strip().lower()
    toks = n.split(" ")
    sfx = []
    while len(toks) > 1 and toks[-1] in ("jr", "sr", "ii", "iii", "iv", "v"):
        sfx.insert(0, toks.pop())
    out = {n}
    if toks:
        base = toks[0] + " " + toks[-1] if len(toks) > 1 else toks[0]
        out.add((base + " " + " ".join(sfx)).strip())
        out.add(base)  # middles and suffix dropped
    return out


def cmd_streak_refresh():
    """Refresh w for the 96 drivers in data/streak_pool.json from MSS race
    results (1973..current). Legends (data/legend_pool.json) are untouched:
    official-records data, not MSS. Writes build/mss_pull/streak_pool.json and
    build/mss_pull/streak_diff.json; never writes data/."""
    pool = json.loads((ROOT / "data" / "streak_pool.json").read_text())
    now_year = datetime.now(timezone.utc).year

    wins_by_uuid = {}
    names_by_uuid = {}
    for y in range(1973, now_year + 1):
        res = season_winners(y, refresh=(y == now_year))
        print("season %d: %d races" % (y, res["races"]), file=sys.stderr)
        for w in res["winners"]:
            wins_by_uuid[w["uuid"]] = wins_by_uuid.get(w["uuid"], 0) + 1
            names_by_uuid[w["uuid"]] = w["driver"]

    # Variant-name index over MSS winners; ambiguous variants are dropped.
    index = {}
    for u, nm in names_by_uuid.items():
        for v in name_variants(nm):
            if v in index and index[v] != u:
                index[v] = None  # ambiguous, exact-only
            else:
                index.setdefault(v, u)

    updated, diff, missing = [], [], []
    for d in pool:
        match = None
        for v in name_variants(d["n"]):
            u = index.get(v)
            if u:
                match = u
                break
        if match is None:
            missing.append(d["n"])
            updated.append(dict(d))
            continue
        new_w = wins_by_uuid[match]
        if new_w != d["w"]:
            diff.append({"driver": d["n"], "mss": names_by_uuid[match], "old": d["w"], "new": new_w})
        nd = dict(d)
        nd["w"] = new_w
        updated.append(nd)

    updated.sort(key=lambda r: (-r["w"], r["n"]))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "streak_pool.json").write_text(json.dumps(updated, separators=(",", ":")), encoding="utf-8")
    (OUT_DIR / "streak_diff.json").write_text(json.dumps({"diff": diff, "unmatched": missing}, indent=2), encoding="utf-8")
    print("wrote %s (%d drivers)" % (OUT_DIR / "streak_pool.json", len(updated)))
    print("changes: %d" % len(diff))
    for c in diff:
        print("  %-24s %d -> %d" % (c["driver"], c["old"], c["new"]))
    if missing:
        print("UNMATCHED (no MSS winner row found): " + ", ".join(missing))
    decreases = [c for c in diff if c["new"] < c["old"]]
    if decreases:
        print("STOP: %d driver(s) decreased; ingest mismatch, do not replace data/." % len(decreases))
        sys.exit(2)


def cmd_test():
    year = datetime.now(timezone.utc).year
    status, j = get("season/ofSeries/" + series_uuid(), {"year": year})
    rows = j.get("content", []) if isinstance(j, dict) else []
    print("GET season/ofSeries/<nascar-cup>?year=%d" % year)
    print("status: %d" % status)
    print("rows: %d" % len(rows))
    for s in rows:
        print("  %s (%s)" % (s.get("name"), s.get("uuid")))
    return 0 if status == 200 else 1


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "test"
    now_year = datetime.now(timezone.utc).year
    if cmd == "test":
        sys.exit(cmd_test())
    elif cmd == "season-results":
        year = int(args[1])
        res = season_results(year)
        write_out("season_results_%d.json" % year, res["races"])
    elif cmd in ("driver-pool", "org-seasons"):
        y_from = int(args[1]) if len(args) > 1 else 1983
        y_to = int(args[2]) if len(args) > 2 else now_year - 1
        seasons = collect_seasons(y_from, y_to)
        if cmd == "driver-pool":
            write_out("driver_pool.json", build_driver_pool(seasons))
        else:
            write_out("org_seasons.json", build_org_seasons(seasons))
    elif cmd == "streak-refresh":
        cmd_streak_refresh()
    elif cmd in ("streak-pool", "grid-facts"):
        seasons = collect_seasons(1972, now_year - 1)
        if cmd == "streak-pool":
            write_out("streak_pool.json", build_streak_pool(seasons))
        else:
            write_out("grid_facts.json", build_grid_facts(seasons))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
