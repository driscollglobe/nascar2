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
  picks                    write data/race_picks.json for Race Picks: playoff
                           schedule, playoff field, and classifications for
                           completed playoff races; fails closed (exit
                           non-zero, nothing written) on missing results, a
                           thin field, or an empty future schedule
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
    # --apply: copy the refreshed pool into data/ (used by the weekly action).
    # Fails closed: any unmatched driver blocks the apply, not just decreases.
    if "--apply" in sys.argv:
        if missing:
            print("STOP: unmatched drivers block --apply.")
            sys.exit(2)
        (ROOT / "data" / "streak_pool.json").write_text(
            json.dumps(updated, separators=(",", ":")), encoding="utf-8")
        print("applied to data/streak_pool.json")


def et_to_utc(date_str, time_str):
    """A calendar session date+startTime interpreted as US Eastern, in UTC ISO.
    The MSS docs do not state the calendar timezone; the Daytona 500 shows
    14:30, the series' famous 2:30pm Eastern slot, so Eastern is the reading
    (UTC would put it mid-morning). If the feed is actually track-local time,
    the Eastern reading locks early, never late."""
    from zoneinfo import ZoneInfo
    naive = datetime.strptime(date_str + " " + time_str, "%Y-%m-%d %H:%M:%S")
    local = naive.replace(tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_picks():
    """Build data/race_picks.json for the Race Picks mode: playoff schedule,
    playoff field, and classifications for completed playoff races. Everything
    is derived from MSS; the command fails closed (exit non-zero, nothing
    written) when any completed playoff race lacks a classification, the field
    has fewer than 8 drivers, or the schedule holds no future race."""
    year = datetime.now(timezone.utc).year
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    status, season = season_for_year(year)
    if season is None:
        sys.exit("picks: no season for %d (HTTP %d)" % (year, status))

    # Playoff boundary from the standings' per-event points: the reset shows as
    # a season-points jump no single race could produce (leader gains > 500).
    status, j = get("seasonDriverRanking/ofSeason/" + season["uuid"])
    if status != 200 or not j or not j.get("content"):
        sys.exit("picks: seasonDriverRanking HTTP %d" % status)
    ranking = j["content"][0].get("ranking") or []
    if not ranking:
        sys.exit("picks: empty ranking")
    ranking.sort(key=lambda r: r.get("position") or 999)
    leader = ranking[0]
    evrows = sorted(leader.get("events") or [], key=lambda e: e.get("eventNumber") or 0)
    # The reset lands ON the regular-season finale's standings row (that row
    # already shows the 2000+ playoff points), so the playoffs start at the
    # NEXT event.
    boundary = None
    prev = None
    for e in evrows:
        sp = e.get("seasonPoints")
        if prev is not None and sp is not None and sp - prev > 500:
            boundary = (e.get("eventNumber") or 0) + 1
            break
        if sp is not None:
            prev = sp
    if boundary is None and prev is not None and (leader.get("points") or 0) - prev > 500:
        boundary = (evrows[-1].get("eventNumber") or 0) + 1
    if boundary is None:
        sys.exit("picks: no playoff reset visible in standings; not in playoffs")

    # Playoff field: the reset group, cut at the largest points gap near the top.
    top = [r for r in ranking[:24] if r.get("points") is not None]
    cut, biggest = None, 0.0
    for i in range(len(top) - 1):
        gap = top[i]["points"] - top[i + 1]["points"]
        if gap > biggest:
            biggest, cut = gap, i + 1
    field_rows = top[:cut] if cut else []
    if len(field_rows) < 8:
        sys.exit("picks: playoff field has %d drivers (need 8+); refusing to write" % len(field_rows))

    # Calendar: playoff events (number >= boundary) with race date and start time.
    status, cal = get("seasonCalendar/" + season["uuid"])
    if status != 200 or not cal:
        sys.exit("picks: seasonCalendar HTTP %d" % status)
    schedule = []
    playoff_events = sorted([e for e in cal.get("events") or [] if (e.get("number") or 0) >= boundary],
                            key=lambda e: e.get("number") or 0)
    # The calendar can list an event number twice; keep one row per number,
    # preferring the one that carries a scheduled Race session time.
    by_number = {}
    for e in playoff_events:
        n = e.get("number")
        has_time = any((s.get("type") == "Race" or s.get("name") == "Race") and s.get("startTime") not in (None, "00:00:00")
                       for s in e.get("sessions") or [])
        if n not in by_number or (has_time and not by_number[n][1]):
            by_number[n] = (e, has_time)
    playoff_events = [v[0] for k, v in sorted(by_number.items())]
    for e in playoff_events:
        race_sessions = [s for s in e.get("sessions") or [] if s.get("type") == "Race" or s.get("name") == "Race"]
        start_utc = None
        rs = race_sessions[0] if race_sessions else None
        if rs and rs.get("date") and rs.get("startTime") and rs["startTime"] != "00:00:00":
            start_utc = et_to_utc(rs["date"], rs["startTime"])
        schedule.append({
            "id": e["uuid"], "number": e.get("number"),
            "race": normws(e.get("name")), "track": normws((e.get("venue") or {}).get("name")),
            "raceDate": e.get("raceDate") or e.get("endDate"),
            "startUtc": start_utc,
        })
    if not any((r["raceDate"] or "") >= today for r in schedule):
        sys.exit("picks: schedule has zero future races; refusing to write")

    # Results for completed playoff races; also car/org per field driver from
    # the season's classifications (positive evidence, latest race wins).
    results = {}
    car_org = {}  # driver uuid -> {car, org}
    for e in playoff_events:
        if (e.get("raceDate") or e.get("endDate") or "9999") >= today:
            continue
        rows = {}
        seen = set()
        for race in get_all("race/ofEvent/" + e["uuid"]):
            for sref in race.get("sessions") or []:
                if sref.get("name") != "Race" or sref["uuid"] in seen:
                    continue
                seen.add(sref["uuid"])
                st, cj = get("sessionClassification/ofSession/" + sref["uuid"])
                if st != 200 or not cj:
                    continue
                for row in cj.get("classificationDetails") or []:
                    pos = row_position(row)
                    drefs = row.get("drivers") or []
                    if pos and drefs and drefs[0].get("name"):
                        rows[normws(drefs[0]["name"])] = pos
        if not rows:
            sys.exit("picks: completed playoff race '%s' (%s) has no classification; refusing to write"
                     % (e.get("name"), e.get("raceDate")))
        results[e["uuid"]] = rows

    field_uuids = set()
    for r in field_rows:
        d = r.get("driver") or {}
        if d.get("uuid"):
            field_uuids.add(d["uuid"])
    all_events = sorted(cal.get("events") or [], key=lambda e: e.get("number") or 0, reverse=True)
    for e in all_events:
        if len(car_org) >= len(field_uuids):
            break
        if (e.get("raceDate") or e.get("endDate") or "9999") >= today:
            continue
        seen = set()
        for race in get_all("race/ofEvent/" + e["uuid"]):
            for sref in race.get("sessions") or []:
                if sref.get("name") != "Race" or sref["uuid"] in seen:
                    continue
                seen.add(sref["uuid"])
                st, cj = get("sessionClassification/ofSession/" + sref["uuid"])
                if st != 200 or not cj:
                    continue
                for row in cj.get("classificationDetails") or []:
                    for ent in row.get("entries") or []:
                        du = (ent.get("driver") or {}).get("uuid")
                        if du in field_uuids and du not in car_org:
                            car_org[du] = {
                                "car": normws(ent.get("carNumber") or row.get("car") or ""),
                                "org": normws((ent.get("team") or {}).get("name") or ent.get("teamEntrantName") or ""),
                            }
    field = []
    for r in field_rows:
        d = r.get("driver") or {}
        extra = car_org.get(d.get("uuid")) or {}
        if not extra.get("car") or not extra.get("org"):
            sys.exit("picks: no car/org found in classifications for %s; refusing to write" % normws(d.get("name")))
        field.append({"driver": normws(d.get("name")), "car": extra["car"], "org": extra["org"]})

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seasonYear": year,
        "schedule": schedule,
        "field": field,
        "results": results,
    }
    path = ROOT / "data" / "race_picks.json"
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print("wrote %s" % path)
    print("playoff boundary: event #%d | schedule: %d races | field: %d drivers | completed with results: %d"
          % (boundary, len(schedule), len(field), len(results)))
    for r in schedule:
        print("  #%d %s | %s | race %s | green flag UTC %s" % (r["number"], r["race"], r["track"], r["raceDate"], r["startUtc"] or "not published"))
    for f in field:
        print("  field: %s | #%s | %s" % (f["driver"], f["car"], f["org"]))


def cmd_champions():
    """Career Cup championship counts per driver, from MSS season final
    standings (seasonDriverRanking position 1 for every completed season,
    1949 through last year). Prints a JS object literal keyed by canonical
    display name, for inlining as the Cross-Era card CHAMPION table."""
    year_to = datetime.now(timezone.utc).year - 1
    counts = {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / "champions.json"
    if cache.exists():
        champs = json.loads(cache.read_text())
    else:
        champs = {}
        for y in range(1949, year_to + 1):
            status, season = season_for_year(y)
            if season is None:
                print("no season %d (HTTP %d)" % (y, status), file=sys.stderr)
                continue
            status, j = get("seasonDriverRanking/ofSeason/" + season["uuid"])
            if status != 200 or not j or not j.get("content"):
                print("no ranking %d (HTTP %d)" % (y, status), file=sys.stderr)
                continue
            ranking = j["content"][0].get("ranking") or []
            top = [r for r in ranking if r.get("position") == 1]
            if not top:
                print("no P1 for %d" % y, file=sys.stderr)
                continue
            champs[str(y)] = normws((top[0].get("driver") or {}).get("name") or "")
        cache.write_text(json.dumps(champs))
    for y in sorted(champs):
        nm = champs[y]
        canon = CANON_FULL.get(nm, nm)
        counts[canon] = counts.get(canon, 0) + 1
        print("%s  %s" % (y, canon), file=sys.stderr)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    print("{")
    for nm, n in ordered:
        print('  "%s": %d,' % (nm, n))
    print("}")


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
    elif cmd == "picks":
        cmd_picks()
    elif cmd == "champions":
        cmd_champions()
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
