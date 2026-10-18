# Team Maker: faster project-assignment check

## Where the time went

I profiled `create_team` phase by phase against the local fixture, with 27
supervisors and 12 existing teams in the center:

| Phase | Cold cache (1st team) | Warm cache (per team) |
| --- | --- | --- |
| Project-assignment check (`_read_center_assignments`) | **26.9 s** | 1.3 s |
| Blank new-team form | 0.6 s | 0.6 s |
| 27 supervisors | 0.5 s | 0.5 s |
| Campaign | 0.9 s | 0.9 s |
| Apply + confirmation | 1.0 s | 1.0 s |

Once the assignment cache is warm, almost all of what's left is the fixture's
own simulated server delays and the safety waits we keep on purpose. The slow
part is the cold check: every existing team in the center is opened one at a
time. That's the 63 s in the 14:30 log.

Two things made each team read slow:

1. **A name search for every team.** The center's team list was read with
   the center filter on, but the filter was cleared *before* the teams were
   opened. On the full 250-team list the team is usually off screen, so
   `open_team` typed its name into the search box and waited for the server
   each time.
2. **A fixed 1.5 s wait per team**, because the picker can briefly show the
   previous team's list.

## Changes (`src/team_maker_bp.py` only)

- **The center filter stays on while the center's teams are opened**, and is
  cleared once afterwards. Every team being read is already on screen, so
  it's clicked directly with no search.
- **The header must name the team before reading.** `open_team` returns as
  soon as the grid cell is visible, which with a filtered list is
  immediately. The read now waits for `Teams >> <team> >> ...` (the same
  breadcrumb the save confirmation uses) before opening Services. This also
  closes an old gap where a slow form switch could let the previous team's
  services be read.
- **Shorter settle when the list has clearly changed.** After each read, the
  Selected list is recorded in the page. The next team's list is accepted
  after two equal reads (0.15 s apart) when it is non-empty and differs from
  that record, because then it can't be the leftover list. An empty list, an
  unchanged list, or no record (the first read, after a reload, or after any
  other picker was opened) keeps the old rule: at least 1.5 s, steady for
  0.4 s.
- **Properties tab switched once** after the reads, not after every team.

Not changed: the duplicate-name and duplicate-project rules, the cache
expiry, the blank-form hold, the pre-Apply check, single Apply, and the 45 s
save confirmation.

## Measured (local fixture, 12 existing center teams, 27 supervisors)

| | Before | After |
| --- | --- | --- |
| Assignment check, cold cache (13 teams) | 26.9 s | 10.1 s |
| First team of a batch, cold cache | 28.9 s | 12.2 s |
| Each team, warm cache | 4.5 s | 4.5 s |

The fixture opens teams instantly. On BrightPattern every team also used to
pay a live name-search round trip, so the live saving per team should be
larger than the ~1.3 s measured here.

## Tests

- New in `test/test_team_maker_fast.py`:
  - `test_assignment_read_never_takes_the_previous_teams_list`: the picker
    shows the previous team's list for 0.6 s first. An empty team must still
    read as empty. It fails if the "differs from the record" condition is
    removed.
  - `test_assignment_reads_open_teams_without_a_name_search`: teams are
    opened from the center-filtered list, no search runs, and the filter is
    cleared afterwards. It fails on the previous engine.
- `test/test_staffing_accuracy.py`: the fake picker page now shows the
  "Teams >> name >>" header, as the real page does.
- `test_team_maker`, `test_team_maker_speed`, `test_team_maker_fast` (9),
  `test_staffing_accuracy` (55), `run_offline`, `test_reliability`,
  `test_ui_runtime` and `test/run.py` give the same results before and after.

No live BrightPattern sign-in or team creation was performed.

## Install

Close Partners Toolbox and copy `src/team_maker_bp.py` over the app's copy.
No new dependencies, and no settings changes.
