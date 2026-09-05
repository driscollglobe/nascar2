#!/usr/bin/env node
/* Command-line balance sweep for Cross-Era.
 *
 * Runs the same runSweep() the in-game "Difficulty check" panel runs (fresh
 * real benchmark + fresh cross-era pack per iteration, deterministic by seed),
 * plus an optional daily check that resolves the exact matchup for a range of
 * UTC days and verifies each one is winnable.
 *
 * Usage:
 *   node tools/sweep.js [n] [--gate]     balance sweep over n matchups (default 200);
 *                                        with --gate, exit 1 if any matchup is
 *                                        unwinnable or order-repaired (CI check)
 *   node tools/sweep.js --daily [days]   verify the daily matchup for the next
 *                                        <days> UTC days from today (default 365)
 */
"use strict";

const ce = require("./crossera");

function pct(x) { return (100 * x).toFixed(1) + "%"; }

function dayKeyFromMs(ms) {
  const d = new Date(ms);
  return d.getUTCFullYear() + "-" + String(d.getUTCMonth() + 1).padStart(2, "0") + "-" + String(d.getUTCDate()).padStart(2, "0");
}

function dailyCheck(days) {
  const start = Date.now();
  const today = new Date();
  const t0 = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  let unwinnable = 0, repaired = 0;
  const bad = [];
  for (let i = 0; i < days; i++) {
    const dk = dayKeyFromMs(t0 + i * 86400000);
    const r = ce.resolveDaily(dk);
    if (!r.run.winnable) { unwinnable++; bad.push(dk); }
    if (r.run.repaired) repaired++;
  }
  console.log("daily check: " + days + " days from " + dayKeyFromMs(t0));
  console.log("  unwinnable: " + unwinnable + (bad.length ? " (" + bad.join(", ") + ")" : ""));
  console.log("  order-repaired: " + repaired);
  console.log("  (" + ((Date.now() - start) / 1000).toFixed(1) + "s)");
  process.exit(unwinnable > 0 ? 1 : 0);
}

const args = process.argv.slice(2);
if (args[0] === "--daily") {
  dailyCheck(parseInt(args[1], 10) || 365);
} else {
  const n = parseInt(args.filter((a) => !a.startsWith("--"))[0], 10) || 200;
  const start = Date.now();
  const s = ce.runSweep(n);
  console.log("balance sweep: " + s.n + " cross-era matchups (" + s.playableTargets + " playable of " + s.totalTargets + " org seasons)");
  console.log("  unwinnable:            " + s.unwinnable);
  console.log("  order-repaired:        " + s.repaired);
  console.log("  optimal win rate:      " + pct(s.optimalWinRate));
  console.log("  random win rate:       " + pct(s.randomWinRate));
  console.log("  name-chaser win rate:  " + pct(s.chaserWinRate));
  console.log("  name-chaser VL rate:   " + pct(s.chaserVictoryLaneRate));
  console.log("  avg benchmark:         " + s.avgBenchmark.toFixed(1) + " wins");
  console.log("  avg chaser total:      " + s.avgChaserTotal.toFixed(1) + " (margin " + (s.avgChaserMargin >= 0 ? "+" : "") + s.avgChaserMargin.toFixed(1) + ")");
  const dist = Object.keys(s.benchDist).map(Number).sort((a, b) => a - b)
    .map((k) => k + ":" + s.benchDist[k]).join(" ");
  console.log("  benchmark distribution: " + dist);
  console.log("  (" + ((Date.now() - start) / 1000).toFixed(1) + "s)");
  if (args.includes("--gate") && (s.unwinnable > 0 || s.repaired > 0)) {
    console.error("GATE FAILED: unwinnable " + s.unwinnable + ", repaired " + s.repaired);
    process.exit(1);
  }
}
