"""Regressions for the faster Team Maker path. Local fixture only; no live accounts.

Run: python test/test_team_maker_fast.py
"""
import time
from unittest import mock

import test_team_maker as tm

bp = tm.bp
LAND = tm.LAND
CAMPAIGN_TEAM = f"{LAND} Squad"


@tm._with_temp_dirs
def test_center_read_replaces_the_separate_name_search(tmp):
    bot = tm._bot(tmp)
    try:
        with mock.patch.object(bot, "team_exists", side_effect=AssertionError("center read covers the name")):
            assert bot.create_team(CAMPAIGN_TEAM, ["Grant Kane"], campaign=LAND) == []
        assert bot.page.evaluate("window.saved") == {CAMPAIGN_TEAM: [LAND]}
    finally:
        bot.close()


@tm._with_temp_dirs
def test_existing_name_without_the_campaign_is_still_refused(tmp):
    bot = tm._bot(tmp)
    try:
        bot.ensure_logged_in()
        bot.open_teams()
        bot.page.evaluate("n => { teams.push(n); teams.sort(); renderTeams(); }", CAMPAIGN_TEAM)
        with mock.patch.object(bot, "team_exists", side_effect=AssertionError("center read covers the name")):
            try:
                bot.create_team(CAMPAIGN_TEAM, ["Grant Kane"], campaign=LAND)
                raise AssertionError("an existing team with this name must not be created again")
            except bp.BPError as error:
                assert "already exists but is not assigned" in str(error), error
        assert CAMPAIGN_TEAM not in (bot.page.evaluate("window.saved") or {})
    finally:
        bot.close()


@tm._with_temp_dirs
def test_name_search_still_runs_when_the_center_read_cannot_cover_it(tmp):
    bot = tm._bot(tmp)
    try:
        assert not bot._inventory_covers("(Squad Holding 9)", LAND)       # holding names aren't in the read
        assert not bot._inventory_covers("No Center Here", LAND)          # no center on the end
        assert not bot._inventory_covers(CAMPAIGN_TEAM, "")               # no campaign, no center read
        assert bot._inventory_covers(CAMPAIGN_TEAM, LAND)
        calls = []
        real = bot.team_exists
        with mock.patch.object(bot, "team_exists", side_effect=lambda n: calls.append(n) or real(n)):
            assert bot.create_team("Plain Team Squad", ["Grant Kane"]) == []
        assert calls == ["Plain Team Squad"], calls
    finally:
        bot.close()


@tm._with_temp_dirs
def test_draft_is_reverified_before_apply(tmp):
    bot = tm._bot(tmp)
    try:
        bot.ensure_logged_in()
        bot.open_teams()
        box = bot._open_new_team_form()
        bot._type_into(box, "Draft Squad")
        bot._verify_draft("Draft Squad")                       # clean draft passes
        bot.page.evaluate("() => loadTeam('existing team')")    # another team loads into the form late
        try:
            bot._verify_draft("Draft Squad")
            raise AssertionError("a form showing another team must never be saved")
        except bp.BPError as error:
            assert "Nothing was saved" in str(error), error
        assert not bot.page.evaluate("window.saved")
    finally:
        bot.close()


@tm._with_temp_dirs
def test_bulk_row_select_is_verified_or_falls_back(tmp):
    bot = tm._bot(tmp)
    try:
        bot.ensure_logged_in()
        bot.open_teams()
        bot._open_new_team_form()
        bot._open_picker("Supervisors:")
        left = bot._stable_names("people", bp.SEL_LEFT_GRID)
        assert bot._select_rows_at_once("people", [0, 2], left)
        selected = bot.page.evaluate("() => [...document.querySelectorAll('#gwt-debug-assignLeftGrid .x-grid3-row')]"
                                     ".map(r => r.classList.contains('x-grid3-row-selected'))")
        assert [i for i, on in enumerate(selected) if on] == [0, 2], selected
        # A list that changed since it was read is never bulk-selected.
        assert not bot._select_rows_at_once("people", [0], left[1:])
        # A selection that doesn't come out exactly right reports False (caller clicks instead).
        real = bot.page.evaluate
        wrong = lambda script, *args: [True, True, True] if script == bp.JS_SELECT_ROWS else real(script, *args)
        with mock.patch.object(bot.page, "evaluate", side_effect=wrong):
            assert not bot._select_rows_at_once("people", [0, 2], left)
        bot._click_dialog_button("Cancel")
    finally:
        bot.close()


@tm._with_temp_dirs
def test_kept_browser_reloads_an_old_page_before_working(tmp):
    lines = []
    bot = tm._bot(tmp, log=lines.append)
    try:
        bot.ensure_logged_in()
        bot.open_teams()
        bot._reload_if_stale()
        assert not any("idle" in line for line in lines), lines
        bot._fresh_at = time.monotonic() - bot.MAX_PAGE_AGE_S - 1
        bot._reload_if_stale()
        assert any("idle" in line for line in lines), lines
        assert time.monotonic() - bot._fresh_at < 5
        assert bot.create_team("After Reload Squad", ["Grant Kane"]) == []
    finally:
        bot.close()


def test_browser_kept_only_with_a_different_staffing_login():
    import team_maker_gui as tmg
    gui = tmg.TeamMakerGUI.__new__(tmg.TeamMakerGUI)
    gui.settings = {"username": "maker-login"}
    gui.coordinator = mock.Mock(session=mock.Mock(username="staffing-login"))
    assert gui._keeps_browser()
    gui.coordinator.session.username = " Maker-Login "
    assert not gui._keeps_browser(), "same account: keep closing the other session first"
    gui.coordinator.session = mock.Mock()                  # no readable username
    assert not gui._keeps_browser()
    gui.coordinator = None
    assert not gui._keeps_browser()


def main():
    checks = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for check in checks:
        check()
        print(f"PASS: {check.__name__}")
    print(f"Team Maker fast-path checks passed: {len(checks)}")


if __name__ == "__main__":
    main()
