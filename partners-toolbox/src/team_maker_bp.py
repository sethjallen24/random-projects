"""
Team Maker engine: drives BrightPattern's Contact Center Administrator in a
dedicated browser to create a team -- name it, assign supervisors, and add
it to its campaign (Services tab) -- then Apply once. Used by
team_maker_gui.py (the "Team Maker" tab).

Everything here was confirmed against the live tenant:

- Element IDs (gwt-debug-*) are stable across visits; grids are ExtJS
  (`.x-grid3-row`, `.x-grid3-td-<column>`), dialogs are `.x-window` OR
  `.x-win` (the skill-level prompt uses the latter).
- A NEW team's Apply button is `gwt-debug-entity-apply`; editing an existing
  team uses `gwt-debug-entity-update`. Only the new-team one is ever clicked,
  so this code cannot change an existing team.
- Right after opening Teams, BrightPattern auto-loads the first team; clicking
  "add" too early lets that team's data land in the new form. We wait for
  that selection to load and for a form that STAYS blank before typing.
- The Services tab works on an unsaved team once it has a name, so name +
  supervisors + campaign are all saved by a single Apply.
- Supervisor rows can be Ctrl-clicked and moved together with one arrow click.

If BrightPattern's screens ever change, this is the only file to update.
"""

import difflib
import json
import os
import re
import time
import unicodedata
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from team_maker_config import ASSIGNMENT_CACHE_PATH, PROFILE_DIR, SCREENSHOT_DIR

# Preflight cap on the number of teams in the tenant. 0 (the default) means
# "don't check" -- Team Maker will go ahead and let BrightPattern itself be the
# authority on whether another team can be created. Set "team_limit" in
# config/teamMaker.json to a positive number to get the old early, clean
# "delete an old team first" refusal back.
TEAM_LIMIT = 0

# ---- Selectors (from the live admin site) ---------------------------------
SEL_LOGIN_USER = "#gwt-debug-authLogin-input"
SEL_LOGIN_PASS = "#gwt-debug-authPassword-input"
SEL_LOGIN_SUBMIT = "#gwt-debug-authSubmit"
SEL_NAV = "#gwt-debug-navigation"
SEL_NAV_TEAMS = "#gwt-debug-navigation_groups"
SEL_TEAMS_VIEW = "#gwt-debug-view-TEAMS"
SEL_TEAMS_GRID = "#gwt-debug-grid-TEAMS"
SEL_ADD_TEAM = "#gwt-debug-addTeam"
SEL_TEAM_NAME = "#gwt-debug-teamName-input"
SEL_NEW_APPLY = "#gwt-debug-entity-apply"           # new team only -- never entity-update
SEL_ASSIGN_WIN = "#gwt-debug-assignWindow"
SEL_LEFT_GRID = f"{SEL_ASSIGN_WIN} #gwt-debug-assignLeftGrid"
SEL_RIGHT_GRID = f"{SEL_ASSIGN_WIN} #gwt-debug-assignRightGrid"
SEL_TO_RIGHT = f"{SEL_ASSIGN_WIN} #gwt-debug-toRightButton"
SEL_FILTER_INPUT = "#gwt-debug-entity-filter-input"
SEL_DIALOG_BUTTONS = ".x-window:visible button, .x-win:visible button"

# Text on the grid cell that identifies a row -- supervisors show first/last
# name in two cells; services show the full name in the cell's title.
JS_NAMES = {
    "people": """
        (sel) => [...document.querySelectorAll(sel + ' .x-grid3-row')].map(r => {
          const c = k => (r.querySelector('.x-grid3-td-' + k) || {}).textContent || '';
          return (c('firstName') + ' ' + c('lastName')).replace(/\\s+/g, ' ').trim();
        })""",
    "services": """
        (sel) => [...document.querySelectorAll(sel + ' .x-grid3-row')].map(r => {
          const td = r.querySelector('.x-grid3-td-name'); if (!td) return '';
          const t = td.querySelector('.x-grid3-cell-inner[title], [title]');
          return (td.getAttribute('title') || (t && t.getAttribute('title')) || td.textContent).replace(/\\s+/g, ' ').trim();
        })""",
}
JS_CLICK_CELL = {"people": ".x-grid3-td-firstName", "services": ".x-grid3-td-name"}

JS_VISIBLE = """(e) => {
  if (!e.getClientRects().length || e.closest('.x-hide-offsets, .x-hide-display, .x-hide-visibility')) return false;
  for (let node = e; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse'
        || Number(style.opacity) === 0) return false;
  }
  const r = e.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0
    && r.top < innerHeight && r.left < innerWidth;
}"""

JS_DIALOGS = f"""
() => {{
  const visible = ({JS_VISIBLE});
  const all = [...document.querySelectorAll('.x-window, .x-win')]
    .filter(w => visible(w)
                 && w.id !== 'gwt-debug-assignWindow' && !w.closest('#gwt-debug-assignWindow'));
  return all.filter(w => !all.some(o => o !== w && o.contains(w)))
    .map(w => w.innerText.replace(/\\s+/g, ' ').trim())
    .filter(t => t.length && !/^(loading|saving|validating)/i.test(t));
}}"""

JS_TEAM_COUNT = """
() => {
  const v = document.getElementById('gwt-debug-view-TEAMS');
  if (!v) return null;
  for (const e of v.querySelectorAll('*')) {
    if (e.children.length === 0) {
      const m = /^(\\d+)\\s+entries$/.exec(e.textContent.trim());
      if (m) return parseInt(m[1], 10);
    }
  }
  return null;
}"""

JS_VIEW_HEADER = """
() => { const v = document.getElementById('gwt-debug-view-TEAMS');
        return v ? v.innerText.split('\\n')[0].trim() : ''; }"""

JS_TEAM_ROW_NAMES = """
() => [...document.querySelectorAll('#gwt-debug-grid-TEAMS .x-grid3-td-name')].map(td => td.textContent.trim())"""

# Read confirmation, errors and progress in one browser round trip. A row
# inserted during a save must not count as success while a saving mask is up.
JS_SAVE_STATE = f"""
() => {{
  const visible = ({JS_VISIBLE});
  const busy = [...document.querySelectorAll('.x-window, .x-win')]
    .some(e => visible(e) && /^(loading|saving|validating)/i.test(e.innerText.trim()))
    || [...document.querySelectorAll('.ext-el-mask, .ext-el-mask-msg, .x-mask, .x-mask-loading')].some(visible);
  return {{header: ({JS_VIEW_HEADER})(), names: ({JS_TEAM_ROW_NAMES})(),
           dialogs: ({JS_DIALOGS})(), busy}};
}}"""

# Selects grid rows the way a mouse does (mousedown/mouseup/click; Ctrl held
# after the first row) in one browser round trip, then reports which rows
# show as selected so the caller can verify before moving anything.
JS_SELECT_ROWS = """
([grid, indexes, cellSel]) => {
  const rows = [...document.querySelectorAll(grid + ' .x-grid3-row')];
  indexes.forEach((i, n) => {
    const cell = rows[i] && (rows[i].querySelector(cellSel) || rows[i]);
    if (!cell) return;
    const r = cell.getBoundingClientRect();
    const opts = {bubbles: true, cancelable: true, view: window, button: 0, ctrlKey: n > 0,
                  clientX: r.left + Math.min(4, r.width / 2), clientY: r.top + Math.min(4, r.height / 2)};
    for (const type of ['mousedown', 'mouseup', 'click']) cell.dispatchEvent(new MouseEvent(type, opts));
  });
  return rows.map(r => r.classList.contains('x-grid3-row-selected'));
}"""

# Tags the visible "edit" link that belongs to the row whose label starts
# with `labelText` ("Supervisors:" / "Service and campaigns...").
JS_MARK_EDIT = """
(labelText) => {
  document.querySelectorAll('[data-tm-edit]').forEach(e => e.removeAttribute('data-tm-edit'));
  const v = document.getElementById('gwt-debug-view-TEAMS') || document.body;
  const leaves = [...v.querySelectorAll('*')].filter(e => e.children.length === 0 && e.offsetParent !== null);
  const label = leaves.find(e => e.textContent.replace(/\\s+/g, ' ').trim().startsWith(labelText));
  let edits = leaves.filter(e => e.textContent.trim() === 'edit');
  if (!edits.length || !label) return false;
  {
    const top = label.getBoundingClientRect().top;
    const below = edits.filter(e => e.getBoundingClientRect().top >= top - 5)
                       .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    if (!below.length) return false;
    edits = below;
  }
  edits[0].setAttribute('data-tm-edit', '1');
  return true;
}"""


class BPError(Exception):
    """A problem the user should see in plain words."""


class TeamAlreadyExists(BPError):
    def __init__(self, team_name):
        self.team_name = team_name
        super().__init__(f'A team for this center and project already exists: "{team_name}".')


class ExistingTeamResult(list):
    def __init__(self, team_name):
        super().__init__(["already existed"])
        self.existing_team = team_name


def _norm(s):
    text = unicodedata.normalize("NFKC", s or "")
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return " ".join(text.split()).casefold()


def _person_key(name):
    """A supervisor's name without its leading "(tag)" and punctuation.

    The centers file and BrightPattern drift apart in exactly these ways --
    "(S) Ana Maria Cruz" in the file while BrightPattern now says
    "(InfoC) Ana Maria Cruz", "(S)Ana Maria Cruz" or "Ana-Maria
    Cruz" -- so this is the second-chance key when no exact name matches.
    """
    text = re.sub(r"^(\([^)]*\)\s*)+", "", _norm(name))
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def _match_person(name, names):
    """Index of supervisor `name` in `names` (already _norm'd): the exact
    name, else the ONE entry with the same _person_key. -1 when neither."""
    target = _norm(name)
    if target in names:
        return names.index(target)
    key = _person_key(name)
    hits = sorted({n for n in names if key and _person_key(n) == key})
    if len(hits) > 1:
        raise BPError(f'"{name}" is not in BrightPattern exactly, and more than one person is close: '
                      + "; ".join(hits) + ". Nothing was saved; put the right one in the centers file.")
    return names.index(hits[0]) if hits else -1


def _campaign_key(name):
    """Allow typographic spacing around hyphens; keep all project identifiers."""
    return re.sub(r"\s*-\s*", "-", _norm(name))


def _team_center(name):
    import staffing_moves as sm
    text = _norm(name).strip("()")
    for alias, center in sorted(sm.CENTER_ALIASES.items(), key=lambda item: -len(item[0])):
        if text == alias or text.endswith(" " + alias):
            return center
    return sm.team_center(name)


def _resolve_campaign_phrase(phrase, names):
    """Exact full name, or one structured short name with an explicit channel."""
    import staffing_moves as sm
    from dataclasses import replace
    names = list(dict.fromkeys(names))
    direct = [n for n in names if _campaign_key(n) == _campaign_key(phrase)]
    if len(direct) > 1:
        raise BPError(f'Multiple campaigns match "{phrase}"; use a unique full name.')
    if direct:
        return direct[0]
    if not set(sm._tokens(phrase)) & set(sm._CHANNELS) and not sm.project_codes(phrase):
        return None
    records = [replace(t, center="611") for t in (sm.parse_campaign_name(n) for n in names) if t]
    result = sm.match_project(phrase, "611", records)
    if result.problem.startswith("more than one") or result.problem == "Cell or Land?":
        raise BPError(f'Campaign "{phrase}" matches multiple projects; include its full code or district.')
    return result.team


class BrightPatternBot:
    def __init__(self, settings, log=print):
        self.settings = settings
        self.log = log
        self._pw = None
        self._ctx = None
        self.page = None
        self.step = "starting"
        self._fresh_at = 0.0          # when the admin page last loaded from the server
        self._last_inventory = None   # (center, team names) from the latest center read

    # ---------------------------------------------------------------- browser
    def start(self):
        if self.page is not None:
            return
        os.makedirs(PROFILE_DIR, exist_ok=True)
        headless = not self.settings.get("show_browser", False)
        forced = os.environ.get("TEST_CHROMIUM_PATH")  # same override the rest of the toolbox honors
        self._pw = sync_playwright().start()
        last_err = None
        # Installed Chrome first (its own profile, separate from the user's),
        # then Edge, then the Chromium that Install.bat downloads.
        for channel in ((None,) if forced else ("chrome", "msedge", None)):
            kwargs = dict(user_data_dir=str(PROFILE_DIR), headless=headless,
                          viewport={"width": 1600, "height": 900},
                          args=["--disable-session-crashed-bubble"])
            if forced:
                kwargs["executable_path"] = forced
            elif channel:
                kwargs["channel"] = channel
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
                self.log(f"Opened dedicated browser ({channel or 'built-in Chromium'}"
                         f"{', hidden' if headless else ''}).")
                break
            except Exception as e:
                last_err = e
        if self._ctx is None:
            self._pw.stop()
            self._pw = None
            raise BPError(f"Could not start a browser: {last_err}")
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(20000)
        self.page.on("dialog", lambda d: d.accept())  # native "leave page?" prompts

    def close(self):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            try:
                if self._pw:
                    self._pw.stop()
            finally:
                self._ctx = self._pw = self.page = None

    def _check_stop(self):
        stop = getattr(self, "stop_event", None)
        if stop is not None and stop.is_set():
            raise BPError("Team creation cancelled before saving.")

    def screenshot(self, tag):
        try:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            path = os.path.join(SCREENSHOT_DIR, f"team-maker-{datetime.now():%Y%m%d-%H%M%S}-{tag}.png")
            self.page.screenshot(path=path)
            return path
        except Exception:
            return None

    # -------------------------------------------------------------- helpers
    def _wait(self, condition, timeout, interval=0.1):
        """Poll `condition()` until it returns a truthy value or `timeout` seconds pass.
        Returns the value (or None on timeout). The 0.1 s default suits checks
        that are true as soon as one read says so; checks that need two equal
        reads in a row pass their own, longer interval."""
        end = time.monotonic() + timeout
        while True:
            self._check_stop()
            value = condition()
            if value:
                return value
            if time.monotonic() >= end:
                return None
            time.sleep(interval)

    def _dialogs(self):
        try:
            texts = self.page.evaluate(JS_DIALOGS)
        except Exception:
            return []
        return [re.sub(r"(\s*(OK|Cancel|Save|Discard|Yes|No))+\s*$", "", t).strip() for t in texts]

    def _click_dialog_button(self, text):
        btn = self.page.locator(SEL_DIALOG_BUTTONS, has_text=re.compile(rf"^\s*{text}\s*$"))
        if not btn.count():
            return False
        btn.first.click()
        try:   # the dialog closing is the signal to go on (was a fixed 0.2 s pause)
            btn.first.wait_for(state="hidden", timeout=500)
        except Exception:
            pass
        return True

    def _dismiss_save_prompt(self):
        """Discard BrightPattern's 'Do you want to Save the changes?' if it's up."""
        if any("save the changes" in t.lower() for t in self._dialogs()):
            self.log("Discarding unsaved changes left on the screen.")
            self._click_dialog_button("Discard")

    def _fail_on_dialog(self, prefix):
        """Raise (after closing it) if BrightPattern popped an error dialog."""
        for text in self._dialogs():
            self._click_dialog_button("OK")
            raise BPError(f"{prefix}: {text}")

    def _type_into(self, target, text):
        loc = self.page.locator(target) if isinstance(target, str) else target
        loc.click()
        loc.press("Control+A")
        loc.press("Backspace")
        if text:
            loc.press_sequentially(text, delay=0)

    def _header(self):
        return _norm(self.page.evaluate(JS_VIEW_HEADER))

    def team_count(self):
        return self.page.evaluate(JS_TEAM_COUNT)

    # --------------------------------------------------------------- login
    def ensure_logged_in(self):
        self.start()
        base = self.settings["admin_url"].strip().split("#")[0]
        if not self.page.url.startswith(base):
            self.log("Opening Contact Center Administrator...")
            self.page.goto(base)
            self._fresh_at = time.monotonic()
        nav, login = self.page.locator(SEL_NAV), self.page.locator(SEL_LOGIN_USER)
        if not self._wait(lambda: nav.is_visible() or login.is_visible(), 60, 0.5):
            self.screenshot("load-failed")
            raise BPError("The admin site did not load. Check the address in Login Settings.")
        if nav.is_visible():
            return

        self.log(f"Signing in as {self.settings['username']}...")
        self._type_into(SEL_LOGIN_USER, self.settings["username"])
        self._type_into(SEL_LOGIN_PASS, self.settings["password"])
        self.page.locator(SEL_LOGIN_SUBMIT).click()

        def outcome():
            if nav.is_visible():
                return "ok"
            errors = [m for m in self._dialogs() if "login" not in m.lower() or "error" in m.lower()]
            return errors[0] if errors and login.is_visible() else None

        result = self._wait(outcome, 30, 0.4)
        if result == "ok":
            self.log("Signed in.")
            return
        self.screenshot("login-failed")
        self._click_dialog_button("OK")
        raise BPError("Sign-in failed. Check the username and password in Login Settings."
                      + (f" BrightPattern said: {result}" if result else ""))

    def warm_up(self):
        """Open the browser, sign in and load Teams ahead of time."""
        self.ensure_logged_in()
        self.open_teams()

    # A browser kept open between batches must not work from an old copy of
    # the Teams list: reload from the server when the page is older than this.
    MAX_PAGE_AGE_S = 600

    def _reload_if_stale(self):
        if self.page is None:
            return
        if not self._fresh_at:          # page already open before tracking began: start the clock
            self._fresh_at = time.monotonic()
        if time.monotonic() - self._fresh_at <= self.MAX_PAGE_AGE_S:
            return
        from admin_teams import disable_http_cache
        self.log("Reloading Contact Center Administrator (page was idle)...")
        disable_http_cache(self.page)
        self.page.reload(wait_until="domcontentloaded")
        self._fresh_at = time.monotonic()
        self.ensure_logged_in()

    # --------------------------------------------------------------- teams
    def open_teams(self):
        entered = not self.page.locator(SEL_TEAMS_GRID).is_visible()
        if entered:
            nav = self.page.locator(SEL_NAV_TEAMS)
            if nav.count() and nav.first.is_visible():
                nav.first.click()
            else:
                self.page.evaluate("() => { location.hash = 'GROUPS'; }")
            self._dismiss_save_prompt()
            self.page.wait_for_selector(SEL_TEAMS_GRID, state="visible", timeout=30000)
        self._wait(lambda: self.team_count() is not None, 15)
        if entered:
            # Wait for the actual first-team selection, once on entry. Network
            # idle may never arrive on this site's persistent connection.
            def selection_ready():
                if self.team_count() == 0:
                    return True
                box = self.page.locator(SEL_TEAM_NAME)
                return box.is_visible() and bool(box.input_value().strip())
            if not self._wait(selection_ready, 15):
                raise BPError("The first team did not finish loading. Nothing was created; try again.")

    def _team_limit(self):
        """
        The preflight team cap: "team_limit" from config/teamMaker.json, else
        TEAM_LIMIT. Anything missing, non-numeric, zero or negative means no
        check at all, so a bad value can never block a create.
        """
        try:
            limit = int(self.settings.get("team_limit", TEAM_LIMIT) or 0)
        except (TypeError, ValueError):
            return 0
        return limit if limit > 0 else 0

    def team_exists(self, name):
        """Filter the Teams list by name and look for an exact match. Leaves the list unfiltered."""
        target = _norm(name)
        before = self.team_count()
        box = self.page.locator(f"{SEL_FILTER_INPUT}:visible").first
        self._type_into(box, name)
        box.press("Enter")

        def result():
            rows = [_norm(n) for n in self.page.evaluate(JS_TEAM_ROW_NAMES)]
            if target in rows:
                return "found"
            return "absent" if self.team_count() != before else None  # filter applied, no match

        found = self._wait(result, 6) == "found"
        self._type_into(box, "")
        box.press("Enter")
        self._wait(lambda: self.team_count() == before, 6)
        return found

    def read_u_teams(self):
        """Fresh, complete U-name inventory; never infer a number from a cache."""
        from admin_teams import disable_http_cache
        from uteams import parse_u_team
        self._check_stop()
        self.ensure_logged_in()
        disable_http_cache(self.page)
        self.page.reload(wait_until="domcontentloaded")
        self._fresh_at = time.monotonic()
        self.ensure_logged_in()
        self.open_teams()
        self._dismiss_save_prompt()
        box = self.page.locator(f"{SEL_FILTER_INPUT}:visible").first
        self._type_into(box, "U")
        box.press("Enter")
        nodes = self.page.locator(f"{SEL_TEAMS_GRID} .x-grid3-scroller")
        scroll = nodes.first if nodes.count() else self.page.locator(SEL_TEAMS_GRID)
        scroll.evaluate("e => {e.scrollTop=0; e.dispatchEvent(new Event('scroll', {bubbles:true}));}")
        found, previous, stable = set(), None, 0
        started = time.monotonic()
        try:
            while time.monotonic() - started < 30:
                self._check_stop()
                names = tuple(self.page.evaluate(JS_TEAM_ROW_NAMES))
                if any("u" not in _norm(name) for name in names):
                    found.clear()
                    stable = 0
                else:
                    found.update(name for name in names if name.strip())
                    count = self.team_count()
                    pos = scroll.evaluate("e => ({top:e.scrollTop, height:e.scrollHeight, view:e.clientHeight})")
                    end = pos["top"] + pos["view"] >= pos["height"] - 1
                    stable = stable + 1 if names == previous and end and count == len(found) else 0
                    if stable >= 4 and time.monotonic() - started >= 1.5:
                        return sorted(name for name in found if parse_u_team(name))
                    scroll.evaluate("e => {e.scrollTop += Math.max(100,e.clientHeight*.8); e.dispatchEvent(new Event('scroll', {bubbles:true}));}")
                previous = names
                time.sleep(.3)
            raise BPError("Could not confirm the complete U-team list. No U team was created; refresh and try again.")
        finally:
            self._type_into(box, "")
            box.press("Enter")

    def create_u_team(self, center, supervisors):
        """Allocate the next per-center number and use the verified single-save path."""
        from uteams import next_u_team
        names = self.read_u_teams()
        name = next_u_team(center, names)
        self._check_stop()
        try:
            warnings = self.create_team(name, supervisors, campaign=None)
        except TeamAlreadyExists as error:
            raise BPError(f'"{error.team_name}" was created by someone else. Refresh the U-team list before trying again.') from error
        if warnings:
            raise BPError("The U team was saved with warnings: " + "; ".join(warnings) + ". Refresh and check it before assigning agents.")
        return name, names + [name]

    def existing_project_team(self, name, campaign):
        """Check this center's actual assignments, reusing valid scoped reads."""
        center = _team_center(name)
        if center is None or not campaign:
            return None
        cache = self._read_center_assignments(center)
        target = _campaign_key(campaign)
        matches = [team for team in cache if target in cache[team]]
        if not matches:
            resolved = _resolve_campaign_phrase(campaign, [service for team in cache for service in cache[team]])
            if resolved:
                matches = [team for team in cache if _campaign_key(resolved) in cache[team]]
        if len(matches) > 1:
            raise BPError("More than one existing team serves this center and project: " + "; ".join(matches)
                          + ". Nothing was created; choose the team to use in Staffing Moves.")
        return matches[0] if matches else None

    def refresh_assignments(self, center):
        """Read one center's assignments again; never add a team or click Apply."""
        center = _team_center("Refresh " + center)
        if center is None:
            raise BPError("Choose a known center before refreshing assignments.")
        self._check_stop()
        self.ensure_logged_in()
        from admin_teams import disable_http_cache
        disable_http_cache(self.page)
        self.page.reload(wait_until="domcontentloaded")
        self._fresh_at = time.monotonic()
        self.ensure_logged_in()
        self.open_teams()
        self._dismiss_save_prompt()
        from assignment_cache import AssignmentCache
        AssignmentCache(ASSIGNMENT_CACHE_PATH, self.settings.get("admin_url", "")).invalidate(center)
        self._assignment_cache = {team: services for team, services in getattr(self, "_assignment_cache", {}).items()
                                  if _team_center(team) != center}
        return self._read_center_assignments(center)

    @staticmethod
    def _center_filter(center):
        import staffing_moves as sm
        return min((alias for alias, value in sm.CENTER_ALIASES.items() if value == center and len(alias) >= 3), key=len)

    def _inventory_covers(self, name, campaign):
        """True when the center read in existing_project_team() is guaranteed to list a
        team called `name` if one exists, so a separate name search is redundant."""
        center = _team_center(name)
        return bool(campaign and center and not name.lstrip().startswith("(")
                    and _norm(self._center_filter(center)) in _norm(name))

    def _read_center_assignments(self, center):
        import staffing_moves as sm
        from admin_teams import open_team
        started = time.monotonic()
        box = self.page.locator(f"{SEL_FILTER_INPUT}:visible").first
        center_filter = self._center_filter(center)
        unfiltered = self.team_count()
        self._type_into(box, center_filter)
        box.press("Enter")
        scroll_nodes = self.page.locator(f"{SEL_TEAMS_GRID} .x-grid3-scroller")
        scroll = scroll_nodes.first if scroll_nodes.count() else self.page.locator(SEL_TEAMS_GRID)
        scroll.evaluate("e => {e.scrollTop=0; e.dispatchEvent(new Event('scroll', {bubbles:true}));}")
        found, previous, stale = set(), None, 0
        inventory_started = time.monotonic()
        deadline = inventory_started + 20
        complete = narrowed = False
        while time.monotonic() < deadline:
            self._check_stop()
            names = tuple(self.page.evaluate(JS_TEAM_ROW_NAMES))
            found.update(n for n in names if _team_center(n) == center and not n.startswith("("))
            if names == previous:
                pos = scroll.evaluate("e => ({top:e.scrollTop, height:e.scrollHeight, view:e.clientHeight})")
                end = pos["top"] + pos["view"] >= pos["height"] - 1
                stale = stale + 1 if end else 0
                # A smaller "N entries" than before typing proves the center
                # filter reached the server and the grid; then four settled reads
                # (0.15 s apart) at the end of the list suffice. Otherwise keep the
                # original 1.5 s minimum.
                count = self.team_count()
                narrowed = count is not None and unfiltered is not None and count < unfiltered
                if stale >= 4 and (narrowed or time.monotonic() - inventory_started >= 1.5):
                    complete = True
                    break
                scroll.evaluate("e => {e.scrollTop += Math.max(100,e.clientHeight*.8); e.dispatchEvent(new Event('scroll', {bubbles:true}));}")
            previous = names
            time.sleep(.15 if narrowed else .3)
        self._type_into(box, "")
        box.press("Enter")
        if not complete:
            raise BPError("Could not finish reading this center's existing teams. Nothing was created; try again.")
        self._last_inventory = (center, {_norm(n) for n in found})
        cache = getattr(self, "_assignment_cache", {})
        self._assignment_cache = cache
        remembered_data = self._load_remembered() if self._cache_hours() else {}
        newly_read, reused = {}, 0
        self.log(f"Checking existing {center} project assignments ({len(found)} team(s))...")
        for number, team in enumerate(sorted(found), 1):
            self._check_stop()
            if team not in cache:
                remembered = self._remembered_services(team, data=remembered_data)
                if remembered is not None:
                    cache[team] = remembered
            if team not in cache:
                self.log(f'  Reading assignments {number}/{len(found)}: "{team}"')
                open_team(self.page, team, timeout_ms=20000)
                self._open_form_tab("Services")
                self._open_picker("Service and")
                loaded = self._wait(lambda: self._names("services", SEL_LEFT_GRID) or
                                    self._names("services", SEL_RIGHT_GRID), 10)
                if not loaded:
                    self._click_dialog_button("Cancel")
                    self.page.wait_for_selector(SEL_ASSIGN_WIN, state="hidden")
                    raise BPError(f'Project lists did not load for "{team}". Nothing was created.')
                previous_services = None
                opened_at = time.monotonic()
                def settled():
                    nonlocal previous_services
                    services = self._names("services", SEL_RIGHT_GRID)
                    stable = services == previous_services and time.monotonic() - opened_at >= 1.5
                    previous_services = services
                    return ("ready", services) if stable else None
                result = self._wait(settled, 8, .4)
                self._click_dialog_button("Cancel")
                self.page.wait_for_selector(SEL_ASSIGN_WIN, state="hidden")
                self._open_form_tab("Properties")
                if not result:
                    raise BPError(f'Could not read project assignments for "{team}". Nothing was created.')
                cache[team] = {_campaign_key(s) for s in result[1]}
                newly_read[team] = cache[team]
            else:
                reused += 1
        if newly_read:
            self._remember_services_batch(newly_read)
        self.log(f"Project assignment check finished in {time.monotonic() - started:.1f}s "
                 f"({len(newly_read)} read, {reused} cached).")
        return {team: cache[team] for team in found}

    # ------------------------------------------------- remembered services
    def _cache_hours(self):
        try:
            return max(float(self.settings.get("assignment_cache_hours", 24) or 0), 0)
        except (TypeError, ValueError):
            return 0

    def _load_remembered(self):
        from assignment_cache import AssignmentCache
        return AssignmentCache(ASSIGNMENT_CACHE_PATH, self.settings.get("admin_url", "")).entries()

    def _remembered_services(self, team, data=None):
        """The team's campaign keys as read within assignment_cache_hours, else None."""
        hours = self._cache_hours()
        entry = (self._load_remembered() if data is None else data).get(team) if hours else None
        try:
            if entry and time.time() - float(entry["read_at"]) < hours * 3600:
                return set(entry["services"])
        except (KeyError, TypeError, ValueError):
            pass
        return None

    def _remember_services(self, team, services):
        self._remember_services_batch({team: services})

    def _remember_services_batch(self, assignments):
        if not self._cache_hours():
            return
        try:
            from assignment_cache import AssignmentCache
            AssignmentCache(ASSIGNMENT_CACHE_PATH, self.settings.get("admin_url", "")).remember(assignments, _team_center)
        except OSError as error:
            self.log(f"Could not remember project assignments for the next run: {error}")

    # ------------------------------------------------------- the main job
    def create_team(self, name, supervisors, campaign=None, skill_level=100):
        """Create the team. Returns a list of warnings (empty when everything went in)."""
        name = " ".join(name.split())
        campaign = " ".join((campaign or "").split())
        unique = {}
        for supervisor in supervisors:
            if supervisor.strip():
                unique.setdefault(_norm(supervisor), " ".join(supervisor.split()))
        supervisors = list(unique.values())
        if not name:
            raise BPError("Team name is empty.")
        started = time.monotonic()
        warnings = []
        try:
            self._check_stop()
            self.step = "signing in"
            self.ensure_logged_in()
            self._reload_if_stale()
            self.step = "opening Teams"
            self.open_teams()
            self._dismiss_save_prompt()

            count = self.team_count()
            limit = self._team_limit()
            self.log(f"Teams in BrightPattern: {count}" + (f" of {limit}." if limit else "."))
            if limit and count is not None and count >= limit:
                raise BPError(f"BrightPattern is at its {limit}-team limit. Delete an old team first.")

            self.step = "checking the name"
            covered = self._inventory_covers(name, campaign)
            named_exists = None if covered else self.team_exists(name)
            self.step = "checking existing projects"
            self._last_inventory = None
            existing = self.existing_project_team(name, campaign)
            if existing:
                raise TeamAlreadyExists(existing)
            if named_exists is None:
                inventory = self._last_inventory
                if inventory is None or inventory[0] != _team_center(name):
                    named_exists = self.team_exists(name)   # center read didn't run; search by name
                else:
                    named_exists = _norm(name) in inventory[1]
            if named_exists:
                if campaign and _team_center(name):
                    raise BPError(f'Team "{name}" already exists but is not assigned to "{campaign}". '
                                  'Nothing was created; check its Services tab.')
                raise TeamAlreadyExists(name)

            self.step = "adding the team"
            self.log("Adding a new team...")
            name_box = self._open_new_team_form()
            self.log(f'Setting name: "{name}"')
            self._type_into(name_box, name)
            name_box.press("Tab")

            if supervisors:
                self.step = "adding supervisors"
                self._assign_supervisors(supervisors)

            if campaign:
                self.step = "adding the campaign"
                if not self._assign_campaign(campaign, skill_level):
                    raise BPError(f'No campaign named "{campaign}" was found. Team was not saved; '
                                  'check the project name and retry.')

            self._check_stop()
            self._verify_draft(name)
            self.step = "clicking Apply"
            self.log(f"Team ready to save in {time.monotonic() - started:.1f}s.")
            self.log("Saving (Apply)...")
            saving_started = time.monotonic()
            self._click_apply()
            self.log("Apply clicked once; waiting for BrightPattern to confirm...")
            self.step = "confirming the save"
            self._wait_for_saved(name)
            self.log(f"Save confirmed in {time.monotonic() - saving_started:.1f}s.")
            if not hasattr(self, "_assignment_cache"):
                self._assignment_cache = {}
            confirmed = getattr(self, "_assigned_campaign", None)
            confirmed = confirmed if isinstance(confirmed, str) and confirmed else campaign
            self._assignment_cache[name] = {_campaign_key(confirmed)} if campaign and not warnings else set()
            self._remember_services(name, self._assignment_cache[name])

            # A lagging count is cosmetic; never hold up the next team for it.
            if count is not None:
                after = self.team_count()
                if after is not None and after > count:
                    self.log(f"Teams in BrightPattern now: {after}" + (f" of {limit}." if limit else "."))
            self.log(f'Team "{name}" created with {len(supervisors)} supervisor(s)'
                     + (f' and campaign "{campaign}".' if campaign and not warnings else "."))
            return warnings
        except TeamAlreadyExists:
            self._recover()
            raise
        except BPError:
            self.screenshot("error")
            self._recover()
            raise
        except PWTimeout:
            shot = self.screenshot("timeout")
            self._recover()
            raise BPError(f"BrightPattern took too long to respond while {self.step}."
                          + (f" Screenshot saved: {shot}" if shot else ""))

    # ------------------------------------------------------------ new form
    def _new_form_ready(self):
        try:
            name_box = self.page.locator(SEL_TEAM_NAME)
            return (name_box.is_visible() and not name_box.input_value().strip()
                    and self.page.locator(SEL_NEW_APPLY).is_visible()
                    and self._header() == _norm("Teams >> Properties"))
        except Exception:
            return False

    def _open_new_team_form(self):
        """Click add-team and wait for a blank new-team form that stays blank (one retry)."""
        for _attempt in range(2):
            self.page.locator(SEL_ADD_TEAM).click()

            def blank():
                self._dismiss_save_prompt()
                self._fail_on_dialog("BrightPattern would not add a team")
                return self._new_form_ready()

            if self._wait(blank, 10):
                blank_since = time.monotonic()
                def stays_blank():
                    if not blank():
                        return "changed"
                    return "ready" if time.monotonic() - blank_since >= 0.5 else None
                if self._wait(stays_blank, 2) == "ready":
                    return self.page.locator(SEL_TEAM_NAME)
            self.log("New-team form wasn't blank yet; trying add-team again...")
        raise BPError("The blank new-team form did not open, so nothing was typed or saved.")

    def _verify_draft(self, name):
        """Last check before Apply: still the NEW-team form (no existing team's
        name in the header) and the name box still holds what was typed. A late
        load of another team into the form is caught here, before anything saves."""
        parts = [p.strip() for p in self._header().split(">>")]
        other_team = len(parts) >= 3 and parts[1] != _norm(name)
        typed = _norm(self.page.locator(SEL_TEAM_NAME).input_value())
        if other_team or typed != _norm(name):
            raise BPError("The new-team form changed before saving (another team's details loaded into it). "
                          "Nothing was saved; try again.")

    # ------------------------------------------------------- picker dialogs
    def _open_picker(self, label):
        """Click the 'edit' link under `label` and wait for the two-list picker to fill."""
        if not self.page.evaluate(JS_MARK_EDIT, label):
            raise BPError(f'Could not find the "edit" link for {label.rstrip(":")}.')
        self.page.locator("[data-tm-edit]").click()
        self.page.wait_for_selector(SEL_ASSIGN_WIN, state="visible")

    def _names(self, kind, grid):
        return [_norm(n) for n in self.page.evaluate(JS_NAMES[kind], grid)]

    def _find_row(self, kind, target, timeout=6):
        """Index of `target` in the Available list once the (filtered) list has settled, else -1."""
        prev = []

        def settled():
            nonlocal prev
            names = self._names(kind, SEL_LEFT_GRID)
            if names == prev and names.count(target) > 1:
                raise BPError(f'More than one {kind} entry matches "{target}". Nothing was saved; use a unique name.')
            hit = names == prev and names.count(target) == 1
            prev = names
            return names.index(target) + 1 if hit else None  # +1 so index 0 is truthy

        found = self._wait(settled, timeout, 0.25)
        return found - 1 if found else -1

    def _find_campaign(self, campaign):
        """Search progressively, then scroll virtual rows; never choose a near name."""
        import staffing_moves as sm
        target = _campaign_key(campaign)
        box = self.page.locator(f"{SEL_LEFT_GRID} {SEL_FILTER_INPUT}")
        client = (sm.phrase_client(campaign) or "").upper()
        queries = list(dict.fromkeys((campaign, client or re.split(r"[\s\-\u2010-\u2015]+", campaign)[0], "")))
        seen = set()
        all_read = False
        for query in queries:
            before = self._names("services", SEL_LEFT_GRID)
            self._type_into(box, query)
            box.press("Enter")
            typed_at = time.monotonic()
            scroll_candidates = self.page.locator(f"{SEL_LEFT_GRID} .x-grid3-scroller")
            scroller = scroll_candidates.first if scroll_candidates.count() else self.page.locator(SEL_LEFT_GRID)
            scroller.evaluate("e => { e.scrollTop = 0; e.dispatchEvent(new Event('scroll', {bubbles:true})); }")
            deadline = time.monotonic() + 12
            unchanged = 0
            previous = None
            while time.monotonic() < deadline:
                names = self._names("services", SEL_LEFT_GRID)
                seen.update(names)
                hits = [i for i, name in enumerate(names) if _campaign_key(name) == target]
                # The list must have changed since typing (or 2 s passed) and then
                # read the same twice in a row: filtering has actually finished.
                applied = names != before or time.monotonic() - typed_at >= 2
                if names == previous and applied:
                    if len(hits) > 1:
                        raise BPError(f'Multiple campaigns match "{campaign}"; use a unique full name.')
                    if hits:
                        return hits[0], names[hits[0]]
                    position = scroller.evaluate("e => ({top:e.scrollTop, height:e.scrollHeight, view:e.clientHeight})")
                    at_end = position["top"] + position["view"] >= position["height"] - 1
                    unchanged = unchanged + 1 if at_end else 0
                    if unchanged >= 8:   # 8 reads 0.15 s apart: the same settle time as before
                        all_read = query == "" or bool(client and query == client)
                        break
                    scroller.evaluate("e => { e.scrollTop += Math.max(100, e.clientHeight * .8); e.dispatchEvent(new Event('scroll', {bubbles:true})); }")
                previous = names
                time.sleep(.15)
            if all_read:
                break
        if all_read:
            resolved = _resolve_campaign_phrase(campaign, seen)
            if resolved:
                self._type_into(box, resolved)
                box.press("Enter")
                index = self._find_row("services", _norm(resolved), timeout=8)
                if index >= 0:
                    return index, _norm(resolved)
        return -1, _norm(campaign)

    def _move_row(self, kind, idx, target):
        """Select one Available row, press the right arrow, confirm it arrived. Retries twice."""
        for _ in range(3):
            row = self.page.locator(f"{SEL_LEFT_GRID} .x-grid3-row").nth(idx)
            row.locator(JS_CLICK_CELL[kind]).click()
            self._wait(lambda: "x-grid3-row-selected" in (row.get_attribute("class") or ""), 0.5, 0.05)
            names = self._names(kind, SEL_LEFT_GRID)
            if idx < len(names) and names[idx] == target and \
                    "x-grid3-row-selected" in (row.get_attribute("class") or ""):
                self.page.locator(SEL_TO_RIGHT).click()
                if self._wait(lambda: target in self._names(kind, SEL_RIGHT_GRID), 4):
                    return True
            idx = self._find_row(kind, target, timeout=3)
            if idx < 0:
                return False
        return False

    def _stable_names(self, kind, grid, before=None, timeout=6):
        """The grid's names once two reads agree (and, when `before` is given,
        once they differ from it or 2 s have passed -- a search that hasn't
        applied yet must not count as "no match")."""
        prev, started = None, time.monotonic()

        def settled():
            nonlocal prev
            names = self._names(kind, grid)
            done = names == prev and (before is None or names != before or time.monotonic() - started >= 2)
            prev = names
            return (names,) if done else None

        result = self._wait(settled, timeout, 0.4)
        return result[0] if result else self._names(kind, grid)

    def _search_and_move_person(self, name, seen):
        """Slow path for one supervisor: search the Available list by full name,
        name without its tag, surname, then first name, and move the one match.
        Returns the name as BrightPattern spells it, or None. Every name the
        searches showed is added to `seen` (for "did you mean" hints)."""
        box = self.page.locator(f"{SEL_LEFT_GRID} {SEL_FILTER_INPUT}")
        core = " ".join(re.sub(r"^(\([^)]*\)\s*)+", "", " ".join(name.split())).split())
        words = core.split()
        queries = [name, core] + ([words[-1], words[0]] if len(words) > 1 else [])
        for query in dict.fromkeys(q for q in queries if q):
            before = self._names("people", SEL_LEFT_GRID)
            self._type_into(box, query)
            box.press("Enter")
            names = self._stable_names("people", SEL_LEFT_GRID, before=before)
            seen.update(self._raw_names(SEL_LEFT_GRID))
            assigned = self._names("people", SEL_RIGHT_GRID)
            i = _match_person(name, assigned)
            if i >= 0:
                return assigned[i]
            i = _match_person(name, names)
            if i >= 0 and self._move_row("people", i, names[i]):
                return names[i]
        return None

    def _raw_names(self, grid):
        return [" ".join(n.split()) for n in self.page.evaluate(JS_NAMES["people"], grid)]

    def _assign_supervisors(self, supervisors):
        self.log(f"Opening supervisor picker ({len(supervisors)} to add)...")
        self._open_picker("Supervisors:")
        try:
            self._fill_supervisor_picker(supervisors)
        except BPError:
            self._click_dialog_button("Cancel")
            raise
        self._ok_picker()

    def _fill_supervisor_picker(self, supervisors):
        # Let the Available list finish loading: reading it half-filled sends
        # people who simply hadn't appeared yet down the slow path.
        self._wait(lambda: self._names("people", SEL_LEFT_GRID), 10)
        left = self._stable_names("people", SEL_LEFT_GRID)
        right = self._names("people", SEL_RIGHT_GRID)
        everyone = left + right
        seen = set(self._raw_names(SEL_LEFT_GRID) + self._raw_names(SEL_RIGHT_GRID))
        ambiguous = [s for s in supervisors if everyone.count(_norm(s)) > 1]
        if ambiguous:
            raise BPError("More than one supervisor matches: " + "; ".join(ambiguous)
                          + ". Nothing was saved; use unique supervisor names.")

        # Each wanted name -> the name as BrightPattern spells it.
        actual = {}
        for s in supervisors:
            i = _match_person(s, everyone)
            if i >= 0:
                actual[s] = everyone[i]
        self._log_respelled(actual)

        # Fast path: Ctrl-click every wanted row in the full list, one arrow click.
        try:
            index = {}
            for i, n in enumerate(left):
                index.setdefault(n, i)
            wanted = list(dict.fromkeys(a for a in actual.values() if a in index and a not in right))
            if wanted:
                if not self._select_rows_at_once("people", [index[a] for a in wanted], left):
                    rows = self.page.locator(f"{SEL_LEFT_GRID} .x-grid3-row")
                    for n, a in enumerate(wanted):
                        rows.nth(index[a]).locator(JS_CLICK_CELL["people"]).click(modifiers=["Control"] if n else None)
                self.page.locator(SEL_TO_RIGHT).click()
                self._wait(lambda: set(wanted) <= set(self._names("people", SEL_RIGHT_GRID)), 5)
        except PWTimeout:
            pass  # anything missed is picked up one at a time below

        assigned = set(self._names("people", SEL_RIGHT_GRID))
        leftover = [s for s in supervisors if actual.get(s) not in assigned]
        if leftover:
            self.log(f"Adding {len(leftover)} supervisor(s) one at a time...")
        missing = []
        for s in leftover:
            found = self._search_and_move_person(s, seen)
            if found:
                actual[s] = found
                self._log_respelled({s: found})
            else:
                missing.append(s)
        if missing:
            raise BPError("These supervisors were not found in BrightPattern, so nothing was saved: "
                          + "; ".join(self._with_hint(s, seen) for s in missing)
                          + ". Fix the name in the centers file (or remove the line if they've left).")

        assigned = set(self._names("people", SEL_RIGHT_GRID))
        not_in = [s for s in supervisors if actual.get(s) not in assigned]
        if not_in:
            raise BPError("Supervisor list did not match after adding: " + "; ".join(not_in))
        self.log(f"  {len(supervisors)} supervisor(s) assigned.")

    def _select_rows_at_once(self, kind, indexes, expected_names):
        """Select `indexes` in the Available list with one scripted round trip.
        True only when the list is unchanged and EXACTLY those rows show as
        selected; otherwise the caller clicks them one by one (the first plain
        click there resets any partial selection)."""
        try:
            if self._names(kind, SEL_LEFT_GRID) != expected_names:
                return False
            selected = self.page.evaluate(JS_SELECT_ROWS, [SEL_LEFT_GRID, indexes, JS_CLICK_CELL[kind]])
        except Exception:
            return False
        return [i for i, on in enumerate(selected) if on] == sorted(indexes)

    def _log_respelled(self, actual):
        for wanted, found in actual.items():
            if found != _norm(wanted):
                self.log(f'  "{wanted}" is spelled "{found}" in BrightPattern; using that '
                         "(update the centers file to match).")

    @staticmethod
    def _with_hint(name, seen):
        """`name`, plus BrightPattern's closest-looking people if there are any."""
        keys = {}
        for n in seen:
            keys.setdefault(_person_key(n), n)
        close = difflib.get_close_matches(_person_key(name), list(keys), n=3, cutoff=0.6)
        surname = _person_key(name).split()[-1:] or [""]
        close += [k for k in keys if surname[0] and surname[0] in k.split() and k not in close]
        return name + (f" (closest in BrightPattern: {', '.join(keys[k] for k in close[:3])})" if close else "")

    def _assign_campaign(self, campaign, skill_level):
        """Services tab > edit > move the campaign across > OK > answer the skill prompt.
        Returns False (after cancelling, back on Properties) when no such campaign exists."""
        self.log(f'Adding campaign: "{campaign}"')
        self._assigned_campaign = None
        self._open_form_tab("Services")
        self._open_picker("Service and")
        self._wait(lambda: self._names("services", SEL_LEFT_GRID), 10)

        target = _resolve_campaign_phrase(campaign, self._names("services", SEL_RIGHT_GRID))
        if target is None:
            idx, target = self._find_campaign(campaign)
            if idx < 0:
                self._click_dialog_button("Cancel")
                self.page.wait_for_selector(SEL_ASSIGN_WIN, state="hidden")
                self._open_form_tab("Properties")
                return False
            if not self._move_row("services", idx, target):
                self._click_dialog_button("Cancel")
                raise BPError(f'Could not move campaign "{campaign}" into Selected services.')
        self._ok_picker()

        # "Apply the following skill level to all team members ...?"  Yes / No
        def prompt():
            texts = self._dialogs()
            if any("skill level" in t.lower() for t in texts):
                return "skill"
            return texts[0] if texts else None

        result = self._wait(prompt, 6)
        if result == "skill":
            box = self.page.locator(".x-win:visible input.x-form-text, .x-window:visible input.x-form-text")
            if box.count():
                self._type_into(box.first, str(skill_level))
            self._click_dialog_button("Yes")
            self.log(f"  Skill level {skill_level} applied for team members.")
        elif result:
            self._click_dialog_button("OK")
            raise BPError(f"BrightPattern said: {result}")
        self._assigned_campaign = target
        self.log("  Campaign added.")
        return True

    def _ok_picker(self):
        self.page.locator(f"{SEL_ASSIGN_WIN} button", has_text=re.compile(r"^\s*OK\s*$")).click()
        self.page.wait_for_selector(SEL_ASSIGN_WIN, state="hidden")

    def _open_form_tab(self, label):
        self.page.locator(f"{SEL_TEAMS_VIEW} button", has_text=re.compile(rf"^\s*{label}\s*$")).first.click()
        if not self._wait(lambda: self._header().endswith(_norm(f">> {label}")), 10):
            raise BPError(f"The {label} tab did not open.")

    # ---------------------------------------------------------------- save
    def _click_apply(self):
        btn = self.page.locator(f"{SEL_NEW_APPLY}:visible").first
        btn.wait_for(state="visible", timeout=15000)
        buttons = btn.locator("button:visible")
        target = buttons.first if buttons.count() else btn
        if not self._wait(lambda: "x-item-disabled" not in (btn.get_attribute("class") or "")
                          and target.is_enabled(), 15):
            raise BPError("The Apply button stayed greyed out, so BrightPattern did not accept the changes.")
        self._save_rows_before = {_norm(n) for n in self.page.evaluate(JS_TEAM_ROW_NAMES)}
        target.click()

    def _wait_for_saved(self, name, timeout=45):
        want = _norm(f"Teams >> {name} >> ")
        target = _norm(name)
        before = getattr(self, "_save_rows_before", None)
        row_can_confirm = before is not None and target not in before
        previous_row = False
        started = last_progress = time.monotonic()

        def progress(status):
            nonlocal last_progress
            now = time.monotonic()
            if now - last_progress >= 5:
                self.log(f"Waiting for save confirmation ({now - started:.0f}s): {status}.")
                last_progress = now

        def outcome():
            nonlocal previous_row
            state = self.page.evaluate(JS_SAVE_STATE)
            if state["dialogs"]:
                text = re.sub(r"(\s*(OK|Cancel|Save|Discard|Yes|No))+\s*$", "", state["dialogs"][0]).strip()
                return ("error", text)
            if state["busy"]:
                previous_row = False
                progress("saving/loading indicator is visible")
                return None
            if _norm(state["header"]).startswith(want):
                return "saved"
            present = row_can_confirm and target in {_norm(n) for n in state["names"]}
            confirmed = present and previous_row
            previous_row = present
            if confirmed:
                return "listed"
            progress("saved header/list has not updated")
            return None

        result = self._wait(outcome, timeout, 0.25)
        if result in ("saved", "listed"):
            if result == "listed":
                self.log("Save confirmed in the Teams list; the header has not caught up yet.")
            return
        if result:
            self._click_dialog_button("OK")
            raise BPError(f"BrightPattern did not save the team: {result[1]}")
        # Both the header and list can stay stale after a successful save.
        # Fetch a fresh server snapshot before reporting a failure; do not
        # click Apply again, or interrupt a save that is visibly still busy.
        try:
            state = self.page.evaluate(JS_SAVE_STATE)
            if not state["busy"] and not state["dialogs"] and row_can_confirm:
                self._check_stop()
                self.log("Refreshing Teams to verify the save...")
                from admin_teams import disable_http_cache
                disable_http_cache(self.page)
                self.page.reload(wait_until="domcontentloaded")
                self.ensure_logged_in()
                self.open_teams()
                if self.team_exists(name):
                    self.log("Save confirmed after refreshing the Teams list.")
                    return
        except BPError:
            raise
        except Exception:
            pass
        raise BPError("Timed out waiting for BrightPattern to confirm the save.")

    def _recover(self):
        """Get back to a clean Teams page after a failure."""
        try:
            for _ in range(3):
                if not self._dialogs():
                    break
                (self._click_dialog_button("Cancel") or self._click_dialog_button("OK")
                 or self._click_dialog_button("Discard"))
            self.page.goto(self.settings["admin_url"].split("#")[0] + "#GROUPS")
            self.page.reload()
        except Exception:
            pass
