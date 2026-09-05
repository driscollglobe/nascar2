# The Lineup

Daily NASCAR games in one self-contained index.html: Daily Grid, Streak with
the Daily Ladder, Cross-Era Draft, and Race Picks. All data is inlined at
build time; the page makes no runtime API calls.

Build: `python3 build.py` assembles index.html from template.html plus the
JSON files under data/. `python3 build.py --check` verifies the assembled
output matches the committed index.html byte for byte.

## Weekly refresh

The GitHub Action in .github/workflows/refresh.yml runs twice a week. The
Monday 10:00 UTC run pulls the Race Picks data (playoff schedule, playoff
field, finishing positions for completed playoff races) and the refreshed
Streak career win totals from the Motorsport Stats Core API. The Friday
16:00 UTC run pulls only the calendar and picks data, so green flag lock
times that firm up late in the week reach the game before Sunday; it never
touches the Streak pool. Both runs rebuild index.html, run the Cross-Era
balance sweep as a gate, and push index.html and data/ to main only when
something changed. A manual run behaves like Monday and refreshes everything. Both pulls fail closed: a completed
race with no classification, a thin playoff field, an empty future schedule,
any Streak win count going down, or an unmatched driver name stops the job
before anything is written.

To run it by hand: GitHub repository page, Actions tab, "Weekly MSS refresh"
in the left sidebar, "Run workflow" button, keep branch main, Run workflow.

If it fails: open the failed run and read the step that went red. A failure
in either pull step is a data problem reported in plain text (which race,
which driver, what was missing); nothing has been committed, so the live game
keeps serving the last good build and there is nothing to roll back. Fix
usually means waiting for Motorsport Stats to publish the missing results and
re-running by hand. A failure in the sweep gate means a data change made a
Cross-Era matchup unwinnable; do not re-run it, open an issue with the sweep
output. A failure in the .env step means the MSS_API_KEY repository secret is
missing or empty.
