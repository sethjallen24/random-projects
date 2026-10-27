"""
Contact Center Administrator (the "/admin" app) is built on ExtJS 3 --
confirmed by hand: grid rows carry classes like "x-grid3-row" and
"x-grid3-row-selected", and the "Move to Another Team" dialog is an
"x-win" window. Unlike the GWT-based Agent Desktop, these are stable
framework class names (not per-build hashes), so it's safe to rely on them
here, combined with visible text for anything team/agent-specific.

CONFIRMED BY HAND: the Members grid (a single team's roster, up to ~178
rows in one real test) is NOT virtualized -- every row exists in the DOM
at once, just visually clipped by CSS overflow. No need to scroll to find
a row there.

CONFIRMED BY A REAL RUN (2026-08-12): the top-level TEAMS grid (the list
of all teams, 167 entries in one real tenant) is DIFFERENT -- it's a
genuinely virtualized/buffered grid. Only the rows near the current
scroll position (~24 of 167) exist in the DOM at all; a team further down
the (alphabetically sorted) list isn't there until scrolled into view.
Naive scrollTop manipulation doesn't work either: the scroller's
scrollHeight equals its clientHeight even with 167 entries (confirmed by
hand) -- ExtJS is recycling rows in place in response to actual wheel
events, not resizing a normally-scrollable container. So finding a team
in this grid means dispatching real mouse-wheel scrolls and re-checking
after each one, not a single wait_for or a scrollTop assignment.

CONFIRMED BY HAND (2026-08-12), in direct response to "did you take into
account that when selecting agents there is also a scroll bar and when
moving to the new team there is also a scroll bar": the two OTHER
scrollbars in this flow -- the (already-open) Members grid used by
select_members_by_name, and the destination-team list inside the "Move to
Another Team" dialog used by move_selected_members_to_team -- are BOTH
ordinary CSS-overflow scrollbars, not virtualized like the Teams grid.
Verified live against a 169-member team ("(InfoConnect Inactive)"): the
Members grid's scroller had scrollHeight (3650px) > clientHeight (548px)
with all 169 ".x-grid3-row" elements present in the DOM at once. The Move
dialog's destination list showed the same pattern: scrollHeight (3586px)
> clientHeight (230px) with all ~166 destination teams present in the DOM
at once. So neither select_members_by_name's plain rows.count()/.nth()
loop nor move_selected_members_to_team's get_by_text(...).last needs any
scrolling logic -- every row they might need to find is already in the
DOM, just visually clipped, regardless of team size.

ALSO DISCOVERED WHILE VERIFYING THE ABOVE (2026-08-12): the Teams grid
has its own filter input, id="gwt-debug-entity-filter-input" (class
"x-form-field x-form-text"). Typing a FULL, EXACT team name into it and
pressing Enter filters the grid and -- if that leaves exactly one exact
match -- auto-navigates straight to that team's Properties page, with no
need to scroll or click at all. This is a much faster and more direct way
to reach a team far down the alphabetized list than wheel-scrolling. It
is NOT safe with a partial name, though: searching just "Squad" matched
26 teams and Enter auto-opened whichever sorted first (the wrong one).
_find_team_cell() below tries this exact-match search first and only
falls back to wheel-scrolling if the search box is missing or doesn't
resolve to the team within a couple seconds.

SUSPECTED (real run, 2026-08-13): an agent was moved from
"(Squad Agents All)" into "(611 Agents Logged In)" instead of the
intended "(Squad Agents Logged In)". This was genuinely confirmed to
have happened for real (the user found the agent sitting in 611's team
in BrightPattern), and independently corroborated by this app's own
post-move verification, which logged "WARNING: expected a delta of 1,
but observed source delta 1 and destination delta 0" for the Squad pair
-- Squad's own destination count never actually changed, so the move
really did land somewhere else. Not reproduced live (no browser access
at diagnosis time), but there's already a confirmed precedent for
exactly this class of bug in THIS file, a few paragraphs up: the Teams
grid's OWN filter input auto-navigated to the WRONG team when Enter was
pressed against an ambiguous/still-settling match ("Squad" alone matched
26 teams). move_selected_members_to_team's destination search box was
pressing Enter immediately after typing, same as that other input --
but this one is a live-suggestion ComboBox, where Enter's well-known
behavior is "confirm whatever's currently highlighted", not "navigate to
an exact match". If the combo's own filtering is debounced/async (typing
fast, at 20ms/keystroke, makes that likely), the highlighted item at the
instant Enter fires can still be whatever was highlighted BEFORE typing
started, not the item actually matching what was just typed. Fixed two
ways: (1) removed the Enter keypress entirely -- typing alone already
filters the list (real keyup events, per the fix above it), and waiting
for + clicking the actual matching option is enough; (2) added a safety
check, right before the final (irreversible) OK click, that the search
box's displayed value really does equal the intended destination team --
if it doesn't, cancels the dialog cleanly and raises instead of
confirming a move that looks wrong, no matter what the exact mechanism
turns out to be.

SUSPECTED (real run, 2026-08-12): "(611 Agents All)" -- the FIRST team
pair processed in that run -- failed to match its one live agent,
"(PS) Seth Allen", in an otherwise-correct Members grid, while every
later pair in the same run matched fine. This wasn't caught live (no
browser access at diagnosis time), but it lines up with a bug already
confirmed and fixed once in agent_desktop.py: a grid's header/shell can
become visible before its data rows finish rendering, and reading it
exactly once, immediately, can catch that empty gap. select_members_by_name
used to do exactly that -- read row count once, right after the "First
Name" header appeared -- with no allowance for a still-loading grid, and
first-in-a-run is exactly when that race is most likely to bite (least
warmed up). It now polls for the row count to stabilize first (see
_collect_stable_member_rows), the same pattern already proven for Agent
Desktop, and -- since this diagnosis was necessarily made without a live
repro -- also saves a diagnostic snapshot (HTML + screenshot) to
debug-dumps/ whenever an agent goes unmatched, so a recurrence leaves
hard evidence instead of requiring another guess.
"""

import os
import re
import time


def _normalize(text):
    return re.sub(r"\s+", "", text).lower()


def dismiss_stuck_modal_if_any(page, timeout_ms=5000):
    """
    CONFIRMED BY A REAL RUN (2026-08-12): when move_selected_members_to_team
    failed partway through (see its own comment on the ':visible' fix)
    with the "Move to Another Team" dialog still open, that dialog's
    ExtJS modal mask (a "[data-lockscreen]"/".x-modal" overlay) stayed on
    screen and blocked every SUBSEQUENT pair's clicks for the rest of the
    run -- every later pair failed identically on
    get_by_text("Teams", ...).click() with "... intercepts pointer
    events", and their failure screenshots were all literally the same
    stuck dialog from the first pair. One pair's failure must never be
    able to cascade like that. Call this at the start of every pair
    (main.py does) so a leftover mask from a previous pair's failure gets
    cleared before the next pair starts, rather than silently blocking it
    too. A clean page (the overwhelmingly common case) returns almost
    immediately: a leftover mask is already on screen, so one instant
    visibility check finds it (this used to wait up to 1s for a mask to
    appear, which cost a full second on every clean move).
    """
    mask = page.locator('[data-lockscreen], .x-modal').first
    try:
        if not mask.is_visible():
            return  # nothing stuck -- nothing to do
    except Exception:
        return

    print("    [recovery] detected a leftover modal dialog/overlay from a previous failure -- attempting to dismiss it")

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for attempt in (
            lambda: page.get_by_text("Cancel", exact=True).first.click(timeout=1000),
            lambda: page.keyboard.press("Escape"),
        ):
            try:
                attempt()
            except Exception:
                pass
        try:
            mask.wait_for(state="hidden", timeout=1000)
            print("    [recovery] cleared the leftover overlay")
            return
        except Exception:
            continue

    print("    [recovery] could not clear the leftover overlay within "
          f"{timeout_ms}ms -- this pair (and possibly later ones) may fail too")


def go_to_teams_section(page, timeout_ms=15000):
    """Click "Teams" in the left "Users & Teams" nav section.

    CONFIRMED BY A REAL RUN (2026-08-12): after switching the configured
    login to a different BrightPattern account, every pair failed with a
    generic "Locator.click: Timeout 30000ms exceeded" waiting for
    get_by_text("Teams", exact=True).first. Failure screenshots from 7
    consecutive attempts were pixel-identical: logged in as the new
    account (username "admin", Team "Administrators"), landed on
    "Users >> User Super >> Person", and the left nav's "Users & Teams"
    section listed ONLY "Users" -- "Teams", "Roles", and "Skill Levels"
    were entirely absent from the DOM (not just collapsed). That means
    the account's BrightPattern Role does not grant permission to view
    Teams in Contact Center Administrator. Without this check, that
    permissions problem is indistinguishable from a slow-loading page
    until the full default timeout (often 30s) elapses, and the resulting
    stack trace gives no hint about the real cause. We instead check for
    "Teams" quickly (capped well below a typical 30s default) and raise a
    clear, actionable RuntimeError instead of letting Playwright's own
    generic timeout fire.
    """
    teams_link = page.get_by_text("Teams", exact=True).first
    fast_check_ms = min(timeout_ms, 5000)
    try:
        teams_link.wait_for(state="visible", timeout=fast_check_ms)
    except Exception as exc:
        raise RuntimeError(
            "Could not find \"Teams\" under \"Users & Teams\" in the left navigation. "
            "This has been confirmed to happen when the logged-in BrightPattern account's "
            "Role does not grant permission to view Teams in Contact Center Administrator "
            "(the account could see \"Users\" but \"Teams\"/\"Roles\"/\"Skill Levels\" were "
            "missing from the nav entirely). Check the account's Role permissions in "
            "BrightPattern (Users & Teams >> Roles), or log in with a different account that "
            "already has access to Teams, then try again."
        ) from exc
    teams_link.click()
    page.get_by_text("Team Name", exact=True).first.wait_for(state="visible", timeout=timeout_ms)


def refresh_teams_section(page, timeout_ms=15000):
    """
    Forces a REAL reload of the admin page, then navigates back to the
    Teams section -- use this (instead of go_to_teams_section) right
    before reading Members counts that need to reflect the server's
    current state, e.g. right after a move.

    CONFIRMED BY A REAL RUN (2026-08-12): a move that genuinely succeeded
    (4 agents moved, no errors, "Move to Another Team" dialog closed
    cleanly) still logged "WARNING: expected a delta of 4, but observed
    source delta 0 and destination delta 0" -- the Members counts read
    from the Teams grid for both '(611 Agents All)' and
    '(611 Agents Logged In)' were byte-for-byte identical before and
    after the move. The user confirmed by hand that the move really had
    worked, and that a manual browser refresh (F5) showed the correct,
    updated counts immediately. That means simply re-clicking "Teams" in
    the left nav does NOT make the Teams grid refetch its data from the
    server -- it's the same ExtJS panel/store for the life of the page,
    so every "before" and "after" count read in a run was quietly
    looking at the same snapshot fetched once when the admin page first
    loaded. Only a genuine page reload (exactly what F5 does) forces a
    fresh fetch, so that's what this does before re-entering the Teams
    section.
    """
    page.reload(wait_until="domcontentloaded")
    page.get_by_text("Configuration", exact=True).first.wait_for(state="visible", timeout=timeout_ms)
    go_to_teams_section(page, timeout_ms=timeout_ms)


def disable_http_cache(page):
    """
    Disables the browser's ordinary HTTP cache for every request this page
    makes from now on, via a CDP session (Chromium only -- fine, this
    project only ever launches Chromium).

    CONFIRMED BY A REAL RUN (2026-08-13): refresh_teams_section's forced
    page.reload() (added specifically to fix a stale-count false warning --
    see its own docstring) turned out NOT to be enough on its own: a real
    run still showed the Teams grid's Members counts as unchanged right
    after a genuinely successful move. A plain reload is a real navigation,
    but HTTP caching applies per-request, not just to the top-level
    document -- if BrightPattern's own front-end doesn't send strong
    no-cache headers on whatever request populates the Teams grid, the
    browser can legitimately serve a cached (pre-move) response even
    across a real reload. Disabling the cache at the network level, once,
    right after each page is created, forces every request that page ever
    makes -- including the reloads refresh_teams_section does -- to go to
    the server fresh for the rest of that page's life.

    This is one of two changes made together for the same real-run report
    (the other is read_member_counts_after_move's retry-with-settle-delay
    below, for the other equally plausible cause: BrightPattern's own
    backend count simply lagging the move by a moment). Both are cheap
    and harmless if the "real" cause turns out to be the other one, so
    both are applied rather than picking just one theory.
    """
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
    except Exception as exc:
        print(f"  (could not disable HTTP caching for this page -- continuing without it: {exc})")


def read_member_counts_after_move(
    page, source_team, destination_team, expected_moved_count, before_source_count, before_dest_count,
    timeout_ms=15000, max_attempts=4, settle_delay_s=1.5,
):
    """
    Re-reads Members counts for source_team/destination_team after a move,
    retrying a fresh refresh_teams_section a few times (with a short pause
    in between) if the counts still show no change from before the move --
    see disable_http_cache's docstring for the real run this responds to.
    Alongside a possible browser-caching cause (handled by
    disable_http_cache), the other equally plausible cause is the same
    class of "read taken before the real update actually lands" race
    already confirmed elsewhere in this exact app (the Members grid's own
    rows, Agent Desktop's real-time table): BrightPattern's Teams grid
    Members count may just take a moment to catch up with a move
    server-side. Rather than accepting the very first post-move read as
    final, this keeps refreshing and re-reading, up to max_attempts times,
    until the counts actually reflect expected_moved_count or attempts run
    out -- returning whatever the last reading was either way, so the
    caller's existing WARNING logic still fires if it genuinely never
    settles.

    Returns (after_source_count, after_dest_count, attempts_used).
    """
    after_source_count = after_dest_count = None
    for attempt in range(1, max_attempts + 1):
        refresh_teams_section(page, timeout_ms=timeout_ms)
        after_source_count = get_member_count(page, source_team, timeout_ms=timeout_ms)
        after_dest_count = get_member_count(page, destination_team, timeout_ms=timeout_ms)

        source_delta = (
            before_source_count - after_source_count
            if before_source_count is not None and after_source_count is not None
            else None
        )
        dest_delta = (
            after_dest_count - before_dest_count
            if after_dest_count is not None and before_dest_count is not None
            else None
        )

        if source_delta == expected_moved_count and dest_delta == expected_moved_count:
            return after_source_count, after_dest_count, attempt

        if attempt < max_attempts:
            print(f"    Members counts still show no change after refreshing (attempt {attempt}/{max_attempts}) -- "
                  "BrightPattern's own counts may just need a moment to catch up with the move. Retrying...")
            time.sleep(settle_delay_s)

    return after_source_count, after_dest_count, max_attempts


def _grid_panel_by_header_label(page, header_label):
    """Locator for the ExtJS panel whose grid header contains the given exact column label."""
    return (
        page.get_by_text(header_label, exact=True)
        .first.locator('xpath=ancestor::*[contains(@class,"x-grid-panel") or contains(@class,"x-panel")][1]')
    )


def _scroll_teams_grid_to_top(page, teams_panel):
    """Best-effort reset to the top of the (virtualized) Teams grid before searching for a team."""
    scroller = teams_panel.locator(".x-grid3-scroller").first
    box = scroller.bounding_box()
    if not box:
        return
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    # A single large upward wheel is enough to snap a buffered grid back to
    # row 0 -- there's nothing above it to scroll past.
    page.mouse.wheel(0, -100000)


def _search_box_find_team(page, teams_panel, team_name, timeout_ms):
    """
    Fast path for _find_team_cell(): use the Teams grid's own filter
    input (see module docstring) to jump straight to team_name instead of
    wheel-scrolling through the virtualized grid. Returns the team's cell
    locator if this worked, or None if the search box isn't present or
    didn't resolve to exactly team_name in time -- callers should fall
    back to scrolling in that case, not treat None as "team doesn't
    exist".
    """
    search_input = page.locator("#gwt-debug-entity-filter-input")
    try:
        search_input.wait_for(state="visible", timeout=1000)
    except Exception:
        return None

    # The search box remembers its previous text between searches
    # (same behavior confirmed by hand on the Move-to-Another-Team
    # dialog's search box) -- always clear it first.
    search_input.fill("")
    search_input.fill(team_name)
    search_input.press("Enter")

    team_cell = teams_panel.get_by_text(team_name, exact=True).first
    try:
        team_cell.wait_for(state="visible", timeout=min(timeout_ms, 3000))
        return team_cell
    except Exception:
        # Ambiguous (multiple partial matches) or genuinely not found --
        # clear the filter so a subsequent wheel-scroll fallback starts
        # from the full, unfiltered list rather than an empty/wrong one.
        try:
            search_input.fill("")
            search_input.press("Enter")
        except Exception:
            pass
        return None


def _find_team_cell(page, teams_panel, team_name, timeout_ms):
    """
    Returns a locator for team_name's cell in the (possibly virtualized)
    Teams grid. Tries the filter-box fast path first (see
    _search_box_find_team and the module docstring); falls back to
    dispatching real wheel scrolls and re-checking after each one if that
    doesn't pan out, since that's the one approach confirmed by hand to
    work unconditionally.
    """
    team_cell = teams_panel.get_by_text(team_name, exact=True).first

    try:
        # Rows already drawn without this team: it won't appear by waiting,
        # so go straight to the search box instead of idling 500ms. Only an
        # empty (still-loading) grid gets the short wait.
        if teams_panel.locator(".x-grid3-row").count() and not team_cell.is_visible():
            raise LookupError(team_name)
        team_cell.wait_for(state="visible", timeout=500)
        return team_cell
    except Exception:
        pass

    found_via_search = _search_box_find_team(page, teams_panel, team_name, timeout_ms)
    if found_via_search is not None:
        return found_via_search

    deadline = time.monotonic() + timeout_ms / 1000
    _scroll_teams_grid_to_top(page, teams_panel)

    scroller = teams_panel.locator(".x-grid3-scroller").first
    box = scroller.bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    previous_first_row_text = None
    stuck_count = 0

    while True:
        try:
            team_cell.wait_for(state="visible", timeout=500)
            return team_cell
        except Exception:
            pass

        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Could not find team '{team_name}' in the Teams grid, even after scrolling through it. "
                "Double-check the team name matches exactly (including parentheses/spacing)."
            )

        page.mouse.wheel(0, 600)

        # If the grid's rendered content hasn't changed after a scroll,
        # we've hit the bottom without finding the team -- fail fast
        # instead of burning the rest of the timeout on no-op scrolls.
        first_row = teams_panel.locator(".x-grid3-row").first
        try:
            current_first_row_text = first_row.inner_text()
        except Exception:
            current_first_row_text = None

        if current_first_row_text == previous_first_row_text:
            stuck_count += 1
            if stuck_count >= 2:
                raise RuntimeError(
                    f"Reached the bottom of the Teams grid without finding team '{team_name}'. "
                    "Double-check the team name matches exactly (including parentheses/spacing)."
                )
        else:
            stuck_count = 0
        previous_first_row_text = current_first_row_text


def open_team(page, team_name, timeout_ms=15000):
    """Click a team by exact name in the left Teams grid, landing on its Properties tab."""
    teams_panel = _grid_panel_by_header_label(page, "Team Name")
    team_cell = _find_team_cell(page, teams_panel, team_name, timeout_ms)
    team_cell.click()

    # Confirms the breadcrumb ("Teams >> <team_name> >> Properties") has
    # actually updated to this team before returning -- team_name already
    # includes its own closing paren (e.g. "(Squad Agents All)"), so this
    # just needs to appear somewhere in the breadcrumb area.
    page.get_by_text(team_name, exact=False).first.wait_for(state="visible", timeout=timeout_ms)


def open_members_tab(page, timeout_ms=15000):
    """
    Click the "Members" tab for the currently open team.

    CONFIRMED BY A REAL RUN (2026-08-12): the Teams grid (the team list
    itself, in the same Teams section) has its OWN "Members" column
    header showing member counts -- get_by_text("Members", exact=True)
    matches that too, and it sits EARLIER in the DOM than the actual
    "Members" tab. ".first" was therefore clicking the grid's column
    header (which just re-sorts the grid by member count) instead of
    switching tabs, so "First Name" never appeared and every pair timed
    out. The "Properties" tab (unique elsewhere on the page, and always
    immediately before "Members" in the team detail tab strip) is used as
    an anchor -- the real "Members" tab is the next matching text after it.
    """
    properties_tab = page.get_by_text("Properties", exact=True).first
    members_tab = properties_tab.locator('xpath=following::*[normalize-space(text())="Members"][1]')
    members_tab.wait_for(state="visible", timeout=timeout_ms)
    members_tab.click()
    page.get_by_text("First Name", exact=True).first.wait_for(state="visible", timeout=timeout_ms)


def get_member_count(page, team_name, timeout_ms=15000):
    """Reads the "Members" count shown for team_name in the left Teams grid."""
    teams_panel = _grid_panel_by_header_label(page, "Team Name")
    team_cell = _find_team_cell(page, teams_panel, team_name, timeout_ms)
    row = team_cell.locator('xpath=ancestor::*[contains(@class,"x-grid3-row")][1]')
    row_text = row.inner_text().strip()
    count_text = row_text[len(team_name):].strip()
    try:
        return int(count_text)
    except ValueError:
        return None


def _collect_stable_member_rows(members_panel, timeout_ms):
    """
    Polls the (already-visible) Members grid until its row count stops
    changing, mirroring agent_desktop.py's _collect_stable_agent_names --
    see the module docstring entry on the "(611 Agents All)" mismatch for
    why: the grid's header can render before its data rows do, and this
    used to be read exactly once, immediately.
    """
    MIN_SETTLE_S = 1.5
    POLL_INTERVAL_S = 0.3

    rows = members_panel.locator(".x-grid3-row")
    start = time.monotonic()
    deadline = start + (timeout_ms / 1000)

    previous_count = -1
    unchanged_reads = 0

    while True:
        try:
            current_count = rows.count()
        except Exception:
            current_count = 0

        unchanged_reads = (unchanged_reads + 1) if current_count == previous_count else 0
        previous_count = current_count

        elapsed = time.monotonic() - start
        settled = current_count > 0 and unchanged_reads >= 2 and elapsed >= MIN_SETTLE_S
        timed_out = time.monotonic() >= deadline

        if settled or timed_out:
            return rows, current_count

        time.sleep(POLL_INTERVAL_S)


def _dump_members_debug_snapshot(page, team_name, debug_dir):
    """
    Best-effort diagnostic capture for when one or more live agents can't
    be matched in the Members grid -- writes the full page HTML and a
    screenshot to debug_dir, mirroring agent_desktop.py's
    _dump_debug_snapshot. Never raises.
    """
    try:
        os.makedirs(debug_dir, exist_ok=True)
        safe_name = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (team_name or "unknown-team"))
        timestamp = time.strftime("%Y%m%dT%H%M%S")
        base = os.path.join(debug_dir, f"members-{safe_name}-{timestamp}")

        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(page.content())

        page.screenshot(path=f"{base}.png", full_page=True)

        print(f"    [debug] could not match an agent in '{team_name}''s Members grid -- saved a diagnostic "
              f"snapshot to {base}.html / {base}.png")
    except Exception as debug_error:
        print(f"    [debug] Could not save a Members-grid diagnostic snapshot for '{team_name}': {debug_error}")


_MEMBER_NAMES_JS = """
(panel) => {
  const txt = el => (el.innerText || el.textContent || '').trim();
  const header = panel.querySelector('.x-grid3-hd-row');
  let heads = header ? [...header.querySelectorAll('td.x-grid3-hd')] : [];
  if (!heads.length && header) heads = [...header.children];
  const labels = heads.map(el => txt(el).toLowerCase());
  const fi = labels.indexOf('first name'), li = labels.indexOf('last name');
  if (fi < 0 || li < 0) throw new Error('Cannot identify First Name and Last Name columns');
  return [...panel.querySelectorAll('.x-grid3-row')].map(row => {
    let cells = [...row.querySelectorAll('td.x-grid3-cell')];
    if (!cells.length) cells = [...row.children];
    if (!cells[fi] || !cells[li]) throw new Error('Incomplete member row');
    return (txt(cells[fi]) + ' ' + txt(cells[li])).trim();
  });
}
"""


def select_members_by_name(page, agent_names, timeout_ms=15000, team_name=None, debug_dir=None):
    """
    Selects every row in the (already open) Members grid whose First+Last
    name matches one of agent_names (as produced by agent_desktop.py).
    Matching is done by stripping all whitespace and lowercasing both
    sides, so it's independent of exactly where BrightPattern splits
    "First" vs "Last" name -- e.g. Agent Desktop's "(W10) John Lenon
    Burato" matches a row with First Name "(W10) John Lenon" / Last Name
    "Burato" because both reduce to the same normalized string.

    Waits for the grid's row count to stabilize before matching (see
    _collect_stable_member_rows) rather than reading it once immediately.

    Pass team_name and debug_dir to save a diagnostic snapshot whenever an
    agent goes unmatched -- see _dump_members_debug_snapshot.

    Returns {"matched": [...], "unmatched": [...]} so callers can warn
    loudly instead of silently moving fewer people than expected.
    """
    members_panel = _grid_panel_by_header_label(page, "First Name")
    members_panel.wait_for(state="visible", timeout=timeout_ms)

    rows, row_count = _collect_stable_member_rows(members_panel, timeout_ms)

    names = members_panel.evaluate(_MEMBER_NAMES_JS)
    if len(names) != row_count:
        raise RuntimeError('Members grid changed while matching agents; refresh and retry.')
    keys = [_normalize(name) for name in names]
    targets = []
    seen = set()
    for name in agent_names:
        key = _normalize(name)
        if key in seen:
            continue
        seen.add(key)
        hits = [i for i, candidate in enumerate(keys) if key and candidate == key]
        if len(hits) > 1:
            raise RuntimeError(f'More than one member has the name {name!r}; no move was made.')
        targets.append({"original": name, "normalized": key,
                        "matched_row_index": hits[0] if hits else None})

    matched_targets = [t for t in targets if t["matched_row_index"] is not None]
    unmatched = [t["original"] for t in targets if t["matched_row_index"] is None]

    # Click order matters for ExtJS multi-select: first click plain (clears
    # any stale selection and starts a fresh set), every subsequent click
    # holds Control to add to the selection rather than replace it.
    for i, target in enumerate(matched_targets):
        row = rows.nth(target["matched_row_index"])
        row.scroll_into_view_if_needed()
        current = members_panel.evaluate(_MEMBER_NAMES_JS)
        if current != names:
            raise RuntimeError('Members grid changed during selection; no move was made.')
        if i == 0:
            row.click()
        else:
            row.click(modifiers=["Control"])

    if matched_targets:
        chosen = members_panel.evaluate(_MEMBER_NAMES_JS.replace(
            "'.x-grid3-row'", "'.x-grid3-row.x-grid3-row-selected'"))
        if sorted(_normalize(n) for n in chosen) != sorted(t['normalized'] for t in matched_targets):
            raise RuntimeError('Selected members do not match the requested agents; no move was made.')

    if unmatched and debug_dir:
        _dump_members_debug_snapshot(page, team_name, debug_dir)

    return {"matched": [t["original"] for t in matched_targets], "unmatched": unmatched}


def verify_agents_present_in_team(page, team_name, agent_names, timeout_ms=15000):
    """
    Opens team_name's Members grid and checks, BY NAME (the same
    normalized First+Last matching used by select_members_by_name),
    whether each of agent_names is actually a member there right now.

    This exists as a failsafe alongside (not instead of) get_member_count:
    a count-based check can only say "the destination's total didn't go
    up by the expected amount" -- it can't say WHICH agent is missing, or
    confirm the specific agent(s) who were supposed to move are actually
    there. See the module docstring's entry on '(Squad Agents All)' being
    moved into '(611 Agents Logged In)' instead of the intended
    '(Squad Agents Logged In)' -- this check is the direct, general-purpose
    answer to "make failsafes for all centers", since it runs identically
    for every configured pair (it's just team_name + a list of names, not
    anything center-specific) and gives a definitive per-agent answer
    instead of an aggregate number that can be right or wrong for reasons
    unrelated to whether the actual agents in question are in the right
    place.

    Returns {"found": [...], "missing": [...]}.
    """
    open_team(page, team_name, timeout_ms=timeout_ms)
    open_members_tab(page, timeout_ms=timeout_ms)

    members_panel = _grid_panel_by_header_label(page, "First Name")
    members_panel.wait_for(state="visible", timeout=timeout_ms)
    rows, row_count = _collect_stable_member_rows(members_panel, timeout_ms)

    row_names = [_normalize(n) for n in members_panel.evaluate(_MEMBER_NAMES_JS)]

    found, missing = [], []
    for name in agent_names:
        normalized = _normalize(name)
        if normalized and row_names.count(normalized) == 1:
            found.append(name)
        else:
            missing.append(name)

    return {"found": found, "missing": missing}


def locate_agents_in_teams(page, agent_names, team_names, timeout_ms=15000):
    """
    Searches each team in team_names (by name, via verify_agents_present_in_team's
    own First+Last matching) for any of agent_names, and returns
    {team_name: [agent_names found there]} -- only for teams where at
    least one agent actually turned up, so a call that finds nothing
    returns an empty dict rather than a dict full of empty lists.

    This is a DIAGNOSTIC, not part of every pair's normal flow -- it's
    only ever called from process_pair, and only after
    verify_agents_present_in_team has already found one or more expected
    agents missing from their intended destination team (see the
    CRITICAL branch there). Its purpose is to answer the obvious next
    question once that's already happened -- "well, where DID they go?"
    -- by checking the OTHER configured centers' own destination
    ("Logged In") teams, so a report like "moved agents into teams not in
    the pairs" (a real incident, 2026-08-15: an agent from '(611 Agents
    All)' ended up in '(InfoConnect Agents Logged In)' instead of '(611
    Agents Logged In)') can name the actual wrong team on the spot,
    instead of just saying "missing" and leaving a manual hunt across
    every other team to whoever has to fix it.

    Deliberately only run when something has already gone wrong: opening
    every OTHER center's Members grid on every single successful move
    would add real navigation time to the common case, for a check
    that's only useful once a move has already been flagged as bad. This
    keeps the normal (successful) path exactly as fast as before --
    nothing about the existing move/verify logic changes; this only adds
    detail to a failure that was already going to be reported.
    """
    locations = {}
    for team_name in team_names:
        result = verify_agents_present_in_team(page, team_name, agent_names, timeout_ms=timeout_ms)
        if result["found"]:
            locations[team_name] = result["found"]
    return locations


def _dump_move_dialog_debug_snapshot(page, destination_team, debug_dir):
    """
    Best-effort diagnostic capture for when clicking a destination team in
    the "Move to Another Team" dialog is blocked (a real run, 2026-08-12,
    showed a modal mask permanently intercepting that click, for reasons
    not yet confirmed live -- see move_selected_members_to_team's
    docstring). In addition to the usual full-page HTML + screenshot
    (mirroring agent_desktop.py's _dump_debug_snapshot), this also
    inlines whatever's actually intercepting the click at that point, and
    the search input's current value, directly into the printed message
    -- so if this happens again, the report contains hard evidence
    instead of needing another round of guessing. Never raises.
    """
    try:
        os.makedirs(debug_dir, exist_ok=True)
        safe_name = "".join(c if (c.isalnum() or c in "-_") else "_" for c in destination_team)
        timestamp = time.strftime("%Y%m%dT%H%M%S")
        base = os.path.join(debug_dir, f"move-dialog-{safe_name}-{timestamp}")

        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        page.screenshot(path=f"{base}.png", full_page=True)

        blocker_info = page.evaluate(
            """(text) => {
                const spans = Array.from(document.querySelectorAll('*'))
                    .filter((el) => el.textContent.trim() === text && el.children.length === 0);
                if (spans.length === 0) return { error: 'target span not found' };
                const target = spans[spans.length - 1];
                const rect = target.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                const topEl = document.elementFromPoint(cx, cy);
                const describe = (el) => el ? {
                    tag: el.tagName, id: el.id, className: el.className,
                    zIndex: window.getComputedStyle(el).zIndex,
                    position: window.getComputedStyle(el).position,
                } : null;
                return {
                    matchCount: spans.length,
                    targetRect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                    elementAtTargetCenter: describe(topEl),
                    targetDescribe: describe(target),
                };
            }""",
            destination_team,
        )
        search_value = page.locator('input[type="text"]:visible').last.input_value()

        print(f"    [debug] click on '{destination_team}' in the Move dialog was blocked -- saved a diagnostic "
              f"snapshot to {base}.html / {base}.png")
        print(f"    [debug] search box value at time of failure: {search_value!r}")
        print(f"    [debug] element actually receiving clicks at the target's location: {blocker_info}")
    except Exception as debug_error:
        print(f"    [debug] Could not save a Move-dialog diagnostic snapshot: {debug_error}")


def move_selected_members_to_team(page, destination_team, dry_run=False, timeout_ms=15000, debug_dir=None):
    """
    Opens the "Move to Another Team" dialog for the currently-selected
    members, picks destination_team, and confirms. Pass dry_run=True to do
    everything up to (but not including) the final OK click. Pass
    debug_dir to save a diagnostic snapshot if the destination click ever
    gets blocked -- see _dump_move_dialog_debug_snapshot.
    """
    page.get_by_text("Move to another team", exact=True).click()

    dialog_title = page.get_by_text("Move to Another Team", exact=True).first
    dialog_title.wait_for(state="visible", timeout=timeout_ms)

    # CONFIRMED BY A REAL RUN (2026-08-12): plain 'input[type="text"]'.last
    # can resolve to a hidden ExtJS focus-trap input (seen live as
    # <input type="text" class="_focus"/>) instead of the dialog's real,
    # visible search box, if that hidden input happens to sit later in
    # DOM order at that moment. fill() then hangs for the full timeout
    # waiting for a hidden element to become visible, which never
    # happens -- and because nothing closed the dialog afterward, it (and
    # its modal mask) stayed open and blocked every later pair's clicks
    # for the rest of that run. ":visible" scopes this to only the
    # actually-visible text input.
    search_input = page.locator('input[type="text"]:visible').last
    search_input.wait_for(state="visible", timeout=timeout_ms)

    # SUSPECTED (real run, 2026-08-12): a failure screenshot showed the
    # destination list still fully unfiltered right after .fill() -- e.g.
    # "(611 Holding 1)", "(611 Holding 2)" etc. still all present, not
    # narrowed to destination_team at all -- right before the click on
    # the (unfiltered-list) destination span got permanently blocked by a
    # modal mask. Not yet confirmed live for this exact dialog (no
    # browser access at diagnosis time), but there's a well-known reason
    # this class of bug happens: Playwright's .fill() sets the input's
    # value directly and fires one 'input'/'change' event -- it does NOT
    # dispatch a per-character keyup, which is exactly the event an ExtJS
    # live-search box typically debounces on to fire its AJAX filter
    # request. That would explain both symptoms at once: the list never
    # actually filters, and (if the half-triggered search leaves the
    # combo's own loading state stuck) something stays modal forever.
    # press_sequentially() dispatches real per-character key events
    # (same as a person typing, and the same as the manual, live-verified
    # testing that DID work earlier in this project's history) instead of
    # one bulk value assignment.
    #
    # SUSPECTED (real run, 2026-08-13): an agent was moved from
    # '(Squad Agents All)' into '(611 Agents Logged In)' instead of the
    # intended '(Squad Agents Logged In)' -- confirmed for real (the user
    # found the agent sitting in 611's team), and independently confirmed
    # by this app's own post-move count check (Squad's own destination
    # count never changed). Not reproduced live, but this is textbook
    # Ext.form.ComboBox behavior: Enter selects whatever item is
    # CURRENTLY highlighted right then, and if the combo's own filtering
    # is debounced/async (typing this fast, one keystroke every 20ms,
    # makes that likely), the highlighted item at the moment Enter fires
    # can still be whatever was highlighted BEFORE typing even started --
    # not the item that actually matches what was just typed. The Enter
    # keypress that used to be here (added earlier to mirror the Teams
    # grid's OWN filter input) was a bad generalization: that's a
    # different, GWT-based, non-suggestion-list input where Enter means
    # "navigate", not this dialog's live-suggestion ComboBox where Enter
    # means "confirm whatever's highlighted". Removed entirely -- typing
    # already filters the list (via real keyup events, per the fix
    # above); waiting for and clicking the actually-matching option
    # directly is enough, and doesn't risk confirming a stale highlight.
    search_input.click()
    search_input.press("Control+A")
    search_input.press("Backspace")
    search_input.press_sequentially(destination_team, delay=20)

    # Give any transient "Loading" state in the dialog a moment to clear
    # before attempting the click -- best-effort, non-fatal if it's not
    # there or doesn't disappear (the click attempt below will still
    # retry via Playwright's own actionability checks either way).
    try:
        page.get_by_text("Loading", exact=False).first.wait_for(state="hidden", timeout=min(timeout_ms, 5000))
    except Exception:
        pass

    destination_option = page.get_by_text(destination_team, exact=True).last
    destination_option.wait_for(state="visible", timeout=timeout_ms)
    try:
        destination_option.click(timeout=timeout_ms)
    except Exception:
        if debug_dir:
            _dump_move_dialog_debug_snapshot(page, destination_team, debug_dir)
        raise

    # Safety net for the same real run above: whatever the exact
    # mechanism turns out to be, moving someone to the wrong team is bad
    # enough (and was bad enough for real) that this checks, right before
    # the irreversible OK click, that the search box now actually shows
    # the destination we intend to confirm -- not just that *some* click
    # landed somewhere. A combo box conventionally updates its own
    # display text to the selected item's label once one is chosen, so a
    # mismatch here means the wrong team is about to be confirmed. If it
    # doesn't match, cancel (closing the dialog cleanly, the same as a
    # normal dry run) instead of leaving it stuck open, save a debug
    # snapshot, and raise -- refusing to move anyone rather than risk
    # moving them to the wrong place again.
    selected_value = search_input.input_value().strip()
    if selected_value != destination_team:
        if debug_dir:
            _dump_move_dialog_debug_snapshot(page, destination_team, debug_dir)
        try:
            page.get_by_text("Cancel", exact=True).first.click(timeout=min(timeout_ms, 5000))
        except Exception:
            pass
        raise RuntimeError(
            f"Refusing to confirm this move: after selecting '{destination_team}', the destination "
            f"field shows '{selected_value}' instead. Aborting before clicking OK so nobody gets moved "
            "to the wrong team -- check the debug snapshot (if debug_dir was set) and verify manually."
        )

    if dry_run:
        page.get_by_text("Cancel", exact=True).first.click()
        return

    page.get_by_text("OK", exact=True).first.click()
    dialog_title.wait_for(state="hidden", timeout=timeout_ms)
