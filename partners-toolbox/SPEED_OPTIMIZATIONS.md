# Speed optimizations

Based on partners-toolbox-project-matching-fixed.zip, with its original colors.

Almost all of the toolbox's time is spent waiting on BrightPattern pages. The
Python logic (message parsing, project matching) takes about 11 ms per batch,
so this update removes dead waiting time in the browser steps. It keeps every
wait that protects a move or a team save. These checks are unchanged: stable
two-read checks, settle windows on the Members and Agent Desktop lists, the
destination check before OK, the 1 s blank-form check, and the 45 s save
confirmation.

## Changes

| Where | Before | After | Saves |
| --- | --- | --- | --- |
| Every staffing move, Logged-In move, cleanup row and group-move source (`dismiss_stuck_modal_if_any`) | Waited up to 1 s for a leftover dialog mask to *appear* on every clean page | One instant check; a leftover mask is already on screen, so it is still found and cleared | ~1 s per move |
| Opening or counting a team that isn't on screen (`_find_team_cell`) | Waited 500 ms for the team to appear in a grid that had already drawn its rows | Goes straight to the exact-name search box when the grid already shows rows without that team; a still-empty grid keeps the 500 ms wait | ~0.5 s per lookup (several per move; one per team in Team Maker's first read of a center) |
| Team Maker waits that finish on a single read (header changed, picker filled, Apply enabled, skill prompt, team list filtered, blank form) | Polled every 0.2–0.3 s | Polls every 0.1 s. Waits that need two equal reads in a row keep their original spacing. | A little on each step |
| App startup | Imported the Google Sheets/Chat client libraries at launch | Loads them the first time a Slack or Chat update runs | ~35% of import time at launch |

## Team Maker fast path (round 2)

Benchmarked on the local fixture: a batch of 4 project teams, each with 27
supervisors and a campaign, assignment cache warm.

| | Before | After |
| --- | --- | --- |
| First team (browser just opened) | 9.94 s | 6.63 s |
| Each following team | 7.68 s | **4.60 s** |

About 1.9 s of the 4.60 s is the fixture's own simulated server delay (three
0.3 s filter responses and a 1 s save). Live times add BrightPattern's real
response time to each step.

- **Browser stays signed in between batches.** This applies in the full app
  when Team Maker's login differs from Staffing's (true of your current setup).
  Each batch used to close Staffing's browser, launch a new one for Team Maker,
  sign in, open Teams and wait for the first team to load, then close it all
  again. Jobs still run one at a time through the shared queue. With the same
  login on both tabs, the old close-and-relaunch behavior is kept.
- **Reload when idle.** A kept browser reloads the admin page from the server
  if it has been idle more than 10 minutes, so the team list is never stale.
  The standalone Team Maker window gains this protection too.
- **One duplicate check instead of two.** The center's team list, already read
  to find existing project teams, now also answers "does a team with this
  exact name exist?" A separate name search runs only when that list can't
  contain the name: holding names in parentheses, names with no center, or no
  campaign.
- **Supervisors selected in one step.** All 27 rows are selected with a
  single browser call instead of 27 separate clicks. The selection is checked
  row by row before the arrow is clicked; if it isn't exactly right, the old
  click-by-click path runs instead.
- **Center read finishes sooner.** Once the "N entries" count drops below the
  unfiltered count (proof the filter reached the server), the read finishes
  after 4 settled reads instead of waiting a fixed 1.5 s.
- **Campaign search finishes sooner.** It polls every 0.15 s, but only
  accepts a match after the list has visibly changed since typing (or 2 s
  have passed). When there's no match, it still waits the same settle time
  before giving up.
- **Blank-form hold 1.0 s → 0.5 s, with a new pre-Apply check.** Right before
  Apply, the form must still be the new-team form (no other team's name in
  the header) and the name box must still hold the typed name. Otherwise
  nothing is saved. This catches a late-loading team at any point, not just
  in the first second.
- **No fixed pauses.** The 0.2 s pause after dialog buttons and the 0.15 s
  pause after row clicks now wait for the actual change instead.

New regressions: `python test/test_team_maker_fast.py` (7 checks). Breaking
the name check or the pre-Apply check on purpose makes them fail.

## Not changed (on purpose)

- Members-grid and Agent Desktop settle windows (1.5 s / 2 s). These were added
  after real runs read a half-loaded or previous team's list.
- Team Maker's 1.5 s settle when reading a team's existing campaigns. The picker
  may still be showing the previous team's list.
- Filter-then-read pauses in the team/campaign inventories. These read once per
  load, not per move.
- The page reload before every move. It is the fix for stale member counts.

## Measured (local fixtures, no live account)

| Step | Before | After |
| --- | --- | --- |
| Stuck-dialog check on a clean page | 1003 ms | 5 ms |
| Open a team that isn't on screen (search-box path) | 611 ms | 106 ms |
| Team Maker: create with supervisors + campaign | 11.37 s | 11.06 s |
| Team Maker: second team, warm browser | 3.97 s | 3.67 s |
| App module import at startup | 300 ms | 195 ms |

The local fixtures respond instantly, so the Team Maker numbers understate the
live gain. On BrightPattern, each 0.1 s cut from a poll and each skipped 500 ms
wait applies to every move and every team.

## Verification

Same results before and after on every suite:

- Offline: `run_design_checks.py`, `run_offline.py`, `test_reliability`,
  `test_ui_runtime`, `test_final_audit` and the workspace/agent-list/U-team/
  project-matching UI checks all pass.
- Browser (local Chromium + fixtures): `test_team_maker` (all), `test_team_maker_speed`
  (13/13), `test_cache_refresh`, `test_uteams_browser`, `test_project_matching_browser`,
  and `test/run.py`'s 68 browser and offline checks all pass. These include
  "dismiss_stuck_modal clears a leftover dialog", "open_team scrolls a virtualized grid"
  and "open_team uses the search box fast path".
- These checks fail identically on the original code in this test environment. They are
  not caused by this update: 11 Staffing Moves panel GUI checks, the slow-admin-page
  sign-in check, and the Slack/Chat "Delete Last" GUI checks.

No live BrightPattern sign-in, staffing move, team creation, or message was performed.

## Install

Close Partners Toolbox and copy the `src` folder from this zip over the app's `src`
folder (`test/test_ui_runtime.py` too, if you run the tests). Keep your `config`
folder and login files. No new dependencies.
