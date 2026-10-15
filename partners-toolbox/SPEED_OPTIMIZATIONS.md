# Speed optimizations

Based on partners-toolbox-project-matching-fixed.zip, with the pink/black/yellow
theme from the previous update merged in.

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
