/* Cross-Era game logic extracted from index.html for command-line harnesses.
 *
 * Every function body here is copied verbatim from the Cross-Era closure in
 * index.html (rng, scoring, solver, pack builder, naive strategies, sweep,
 * daily resolver). Only the wrapping differs: data comes from data/*.json
 * instead of window.__LINEUP_DATA__, and the functions are exported as a
 * module. No game logic is modified; if index.html changes, re-extract.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const DATA_DIR = path.join(__dirname, "..", "data");
function loadJSON(name) {
  return JSON.parse(fs.readFileSync(path.join(DATA_DIR, name), "utf8"));
}

const ORG_SEASONS = loadJSON("org_seasons.json");
const POOL = loadJSON("driver_pool.json");

/* ---------------- config ---------------- */
var PACK = {
  minSize: 6, maxSize: 10, extra: 3,
  dynastyWins: 12, passesDynasty: 4, passesBeatable: 5,
};
var MIN_BENCHMARK = 9;   // cross-era floor: 8-win targets were a soft-target glut (266 of 1000 sweep days)
var VICTORY_MARGIN = 3;

/* ---------------- rng ---------------- */
function hashString(str) {
  var h = 2166136261 >>> 0;
  for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function mulberry32(seed) {
  var a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    var t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function rngFromSeed(seedStr) { return mulberry32(hashString(seedStr)); }
function shuffle(arr, rng) {
  var out = arr.slice();
  for (var i = out.length - 1; i > 0; i--) {
    var j = Math.floor(rng() * (i + 1));
    var tmp = out[i]; out[i] = out[j]; out[j] = tmp;
  }
  return out;
}

/* ---------------- scoring + solver ---------------- */
var OUTCOME = { VICTORY_LANE: "VICTORY_LANE", WIN: "WIN", MISS: "MISS" };
function scoreRun(signedCards, orgSeason) {
  var total = signedCards.reduce(function (s, c) { return s + (c.wins || 0); }, 0);
  var benchmark = orgSeason.benchmarkWins;
  var margin = VICTORY_MARGIN;
  var outcome = OUTCOME.MISS;
  if (total >= benchmark + margin) outcome = OUTCOME.VICTORY_LANE;
  else if (total >= benchmark) outcome = OUTCOME.WIN;
  return {
    total: total, benchmark: benchmark, diff: total - benchmark, outcome: outcome,
    win: outcome !== OUTCOME.MISS, victoryLane: outcome === OUTCOME.VICTORY_LANE,
  };
}
function solveMaxAchievable(packWins, seats, passes) {
  var n = packWins.length;
  var memo = new Map();
  function best(i, seatsLeft, passesLeft) {
    if (seatsLeft === 0) return 0;
    if (i >= n) return -Infinity;
    var k = i + "|" + seatsLeft + "|" + passesLeft;
    if (memo.has(k)) return memo.get(k);
    var signVal = packWins[i] + best(i + 1, seatsLeft - 1, passesLeft);
    var passVal = -Infinity;
    if (passesLeft > 0) passVal = best(i + 1, seatsLeft, passesLeft - 1);
    var res = Math.max(signVal, passVal);
    memo.set(k, res);
    return res;
  }
  return best(0, seats, passes);
}

/* ---------------- pool indexes ---------------- */
var _careerWinners = null;
var _fame = null;
var _byDriver = null;
function buildPoolIndexes() {
  if (_careerWinners) return;
  _careerWinners = new Set(); _fame = new Map(); _byDriver = new Map();
  for (var i = 0; i < POOL.length; i++) {
    var c = POOL[i];
    if (c.wins > 0) _careerWinners.add(c.driver);
    _fame.set(c.driver, (_fame.get(c.driver) || 0) + c.wins);
    if (!_byDriver.has(c.driver)) _byDriver.set(c.driver, []);
    _byDriver.get(c.driver).push(c);
  }
}
function fameOf(driver) { buildPoolIndexes(); return _fame.get(driver) || 0; }

/* ---------------- cross-era pack builder (buildRun) ---------------- */
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function passesForBenchmark(wins) { return wins >= PACK.dynastyWins ? PACK.passesDynasty : PACK.passesBeatable; }

function repairWinnableOrder(pack, seats, passes) {
  var windowSize = Math.min(pack.length, seats + passes);
  var winnerIdx = pack.map(function (c, i) { return { i: i, wins: c.wins }; })
    .sort(function (a, b) { return b.wins - a.wins; })
    .slice(0, seats).map(function (x) { return x.i; });
  var winnerSet = new Set(winnerIdx);
  var winners = winnerIdx.map(function (i) { return pack[i]; });
  var rest = pack.filter(function (_, i) { return !winnerSet.has(i); });
  var fillerInWindow = rest.slice(0, Math.max(0, windowSize - winners.length));
  var tail = rest.slice(fillerInWindow.length);
  return winners.concat(fillerInWindow, tail);
}

function buildCrossEraRun(orgSeason, seedStr) {
  buildPoolIndexes();
  var seats = orgSeason.seats;
  var passes = passesForBenchmark(orgSeason.benchmarkWins);
  var benchmark = orgSeason.benchmarkWins;

  var rosterKey = new Set(orgSeason.realRoster.map(function (r) { return orgSeason.year + "|" + r.driver; }));
  var pickRng = rngFromSeed(seedStr + "|cross");
  var candidates = [];
  _byDriver.forEach(function (cards, driver) {
    if (!_careerWinners.has(driver)) return;
    var seasons = cards.filter(function (c) { return !rosterKey.has(c.year + "|" + c.driver); });
    if (!seasons.length) return;
    candidates.push(seasons[Math.floor(pickRng() * seasons.length)]);
  });

  function topSum(list) {
    return list.map(function (c) { return c.wins; }).sort(function (a, b) { return b - a; })
      .slice(0, seats).reduce(function (a, b) { return a + b; }, 0);
  }
  if (topSum(candidates) < benchmark) {
    var peaks = [];
    _byDriver.forEach(function (cards2, driver) {
      if (!_careerWinners.has(driver)) return;
      var eligible = cards2.filter(function (c) { return !rosterKey.has(c.year + "|" + c.driver); });
      if (!eligible.length) return;
      peaks.push(eligible.reduce(function (a, b) { return b.wins > a.wins ? b : a; }));
    });
    peaks.sort(function (a, b) { return b.wins - a.wins; });
    for (var pi = 0; pi < peaks.length && topSum(candidates) < benchmark; pi++) {
      for (var ci = 0; ci < candidates.length; ci++) {
        if (candidates[ci].driver === peaks[pi].driver) { candidates[ci] = peaks[pi]; break; }
      }
    }
  }

  var packSize = clamp(seats + passes + (PACK.extra || 0), PACK.minSize, PACK.maxSize);
  var cards = shuffle(candidates, rngFromSeed(seedStr + "|field")).slice(0, packSize);

  var allSorted = candidates.slice().sort(function (a, b) { return b.wins - a.wins; });
  function topSeatsSum(cs) {
    return cs.map(function (c) { return c.wins; }).sort(function (a, b) { return b - a; })
      .slice(0, seats).reduce(function (a, b) { return a + b; }, 0);
  }
  var guard = 0;
  while (topSeatsSum(cards) < benchmark && guard < allSorted.length) {
    var have = new Set(cards.map(function (c) { return c.driver; }));
    var strongest = allSorted.find(function (c) { return !have.has(c.driver); });
    if (!strongest) break;
    var wi = -1, wmin = Infinity;
    cards.forEach(function (c, idx) { if (c.wins < wmin) { wmin = c.wins; wi = idx; } });
    if (wi < 0 || strongest.wins <= cards[wi].wins) break;
    cards[wi] = strongest; guard++;
  }

  function winsOf(cs) { return cs.map(function (c) { return c.wins; }); }
  var pack = shuffle(cards, rngFromSeed(seedStr + "|order"));
  var tries = 0, repaired = false;
  while (solveMaxAchievable(winsOf(pack), seats, passes) < benchmark && tries < 80) {
    tries++; pack = shuffle(cards, rngFromSeed(seedStr + "|order|" + tries));
  }
  if (solveMaxAchievable(winsOf(pack), seats, passes) < benchmark) {
    pack = repairWinnableOrder(pack, seats, passes); repaired = true;
  }
  var maxOrdered = solveMaxAchievable(winsOf(pack), seats, passes);
  return {
    orgId: orgSeason.id, year: orgSeason.year, seats: seats, passes: passes, pack: pack,
    benchmark: benchmark, maxOrdered: maxOrdered, winnable: maxOrdered >= benchmark, repaired: repaired,
  };
}

/* ---------------- naive strategies ---------------- */
function nameChaserDraft(pack, seats, passes) {
  var ranked = pack.map(function (c) { return { driver: c.driver, fame: fameOf(c.driver) }; })
    .sort(function (a, b) { return b.fame - a.fame; });
  var targets = new Set(ranked.slice(0, seats).map(function (o) { return o.driver; }));
  var seatsLeft = seats, passesLeft = passes, signed = [];
  for (var i = 0; i < pack.length && seatsLeft > 0; i++) {
    var card = pack[i];
    var cardsAfter = pack.length - (i + 1);
    var canPass = passesLeft > 0 && cardsAfter >= seatsLeft;
    var want = targets.has(card.driver);
    var sign = want || !canPass;
    if (sign) { signed.push(card); seatsLeft--; targets.delete(card.driver); }
    else passesLeft--;
  }
  return signed;
}
function randomDraft(pack, seats, passes, rnd) {
  var seatsLeft = seats, passesLeft = passes, signed = [];
  for (var i = 0; i < pack.length && seatsLeft > 0; i++) {
    var cardsAfter = pack.length - (i + 1);
    var canPass = passesLeft > 0 && cardsAfter >= seatsLeft;
    var sign = !canPass || rnd() < 0.5;
    if (sign) { signed.push(pack[i]); seatsLeft--; } else passesLeft--;
  }
  return signed;
}

/* ---------------- the balance sweep ---------------- */
function runSweep(n) {
  n = n || 200;
  var targets = ORG_SEASONS.filter(function (o) { return o.benchmarkWins >= MIN_BENCHMARK && o.seats >= 2; });
  var pickRng = mulberry32(hashString("crossera-sweep-v1"));
  var chaserWins = 0, chaserVL = 0, randWins = 0, optWins = 0, unwinnable = 0, repaired = 0;
  var benchSum = 0, chaserTotalSum = 0, chaserMarginSum = 0;
  var benchDist = {};
  for (var k = 0; k < n; k++) {
    var os = targets[Math.floor(pickRng() * targets.length)];
    var seed = "sweep|" + k + "|" + os.id;
    var run = buildCrossEraRun(os, seed);
    if (!run.winnable) unwinnable++;
    if (run.repaired) repaired++;
    var chaser = scoreRun(nameChaserDraft(run.pack, run.seats, run.passes), os);
    var rnd = mulberry32(hashString(seed + "|rnd"));
    var random = scoreRun(randomDraft(run.pack, run.seats, run.passes, rnd), os);
    if (chaser.win) chaserWins++;
    if (chaser.victoryLane) chaserVL++;
    if (random.win) randWins++;
    if (run.maxOrdered >= os.benchmarkWins) optWins++;
    benchSum += os.benchmarkWins; chaserTotalSum += chaser.total; chaserMarginSum += chaser.diff;
    benchDist[os.benchmarkWins] = (benchDist[os.benchmarkWins] || 0) + 1;
  }
  return {
    n: n,
    chaserWinRate: chaserWins / n,
    chaserVictoryLaneRate: chaserVL / n,
    randomWinRate: randWins / n,
    optimalWinRate: optWins / n,
    avgBenchmark: benchSum / n,
    avgChaserTotal: chaserTotalSum / n,
    avgChaserMargin: chaserMarginSum / n,
    unwinnable: unwinnable, repaired: repaired,
    playableTargets: targets.length, totalTargets: ORG_SEASONS.length,
    benchDist: benchDist,
  };
}

/* ---------------- daily resolver (resolveDaily) ----------------
 * Mirrors the client: THEME_CALENDAR + themeForDay come from the services
 * script, playableTargets/dailyOrgSeason from the Cross-Era closure. Given a
 * UTC day key ("YYYY-MM-DD") this returns the exact org season and run every
 * player gets that day. */
var THEME_CALENDAR = [
  { from: "2026-09-02", to: "2026-09-08", label: "Darlington week",
    gridBias: ["Darlington"], crossEraBias: { minBenchmark: 12 } },
  { from: "2026-09-16", to: "2026-09-22", label: "Bristol week",
    gridBias: ["Bristol"], crossEraBias: null },
  { from: "2026-09-30", to: "2026-10-06", label: "Talladega week",
    gridBias: ["Talladega"], crossEraBias: null },
  { from: "2026-10-21", to: "2026-10-27", label: "Martinsville week",
    gridBias: ["Martinsville"], crossEraBias: null },
  { from: "2026-11-04", to: "2026-11-10", label: "Champion week",
    gridBias: ["Won 30 or more", "Won 10 or more"], crossEraBias: { minBenchmark: 12 } },
];
function themeForDay(dayStr) {
  for (var i = 0; i < THEME_CALENDAR.length; i++) {
    var t = THEME_CALENDAR[i];
    if (dayStr >= t.from && dayStr <= t.to) return t;
  }
  return null;
}
function playableTargets() {
  return ORG_SEASONS.filter(function (o) { return o.benchmarkWins >= MIN_BENCHMARK && o.seats >= 2; });
}
function dailyOrgSeason(dk, theme) {
  var targets = playableTargets();
  var rng = rngFromSeed("crossera-daily-pick|" + dk);
  var pool = targets;
  if (theme && theme.crossEraBias) {
    var b = theme.crossEraBias;
    var filtered = targets.filter(function (o) {
      if (b.minBenchmark && o.benchmarkWins < b.minBenchmark) return false;
      if (b.orgIncludes && b.orgIncludes.indexOf(o.org) < 0) return false;
      if (b.yearFrom && o.year < b.yearFrom) return false;
      if (b.yearTo && o.year > b.yearTo) return false;
      return true;
    });
    if (filtered.length) pool = filtered;
  }
  return pool[Math.floor(rng() * pool.length)];
}
function resolveDaily(dk) {
  var theme = themeForDay(dk);
  var os = dailyOrgSeason(dk, theme);
  var run = buildCrossEraRun(os, "crossera-daily|" + dk);
  return { day: dk, theme: theme ? theme.label : null, orgSeason: os, run: run };
}

module.exports = {
  ORG_SEASONS: ORG_SEASONS, POOL: POOL,
  PACK: PACK, MIN_BENCHMARK: MIN_BENCHMARK, VICTORY_MARGIN: VICTORY_MARGIN,
  hashString: hashString, mulberry32: mulberry32, rngFromSeed: rngFromSeed, shuffle: shuffle,
  scoreRun: scoreRun, solveMaxAchievable: solveMaxAchievable,
  fameOf: fameOf, buildCrossEraRun: buildCrossEraRun,
  nameChaserDraft: nameChaserDraft, randomDraft: randomDraft,
  runSweep: runSweep,
  themeForDay: themeForDay, playableTargets: playableTargets,
  dailyOrgSeason: dailyOrgSeason, resolveDaily: resolveDaily,
};
