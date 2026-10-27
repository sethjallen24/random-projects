# Partners Toolbox on a Mac

The same folder now runs on macOS as well as Windows. The four tools work
the same way, and your `config` folder, `.env`, `credentials.json`,
`slack_token.txt` and `chat_token.json` carry over unchanged.

## Install (once)

1. **Python:** install the latest Python from
   [python.org](https://www.python.org/downloads/macos/). Its installer
   includes Tk, which the app's window needs. Homebrew also works
   (`brew install python python-tk`). Apple's built-in `python3` is not
   used, because its old Tk draws the window incorrectly.
2. Extract the zip anywhere, e.g. your Documents folder.
3. Double-click **`Install (Mac).command`**. It finds Python and creates a
   private environment in `.venv` inside the folder. It then installs the
   packages, downloads Chromium for the BrightPattern tools, builds
   **`Partners Toolbox.app`** in the folder, and starts the app.
4. Drag **Partners Toolbox.app** to your Dock, or to Applications, to keep it.

**If macOS says the file "can't be opened"** because it came from an
unidentified developer, use either of these:

- Open **System Settings → Privacy & Security**, scroll down, and click
  **Open Anyway** next to the message about the file. Then double-click it
  again.
- Or open **Terminal**, type `bash ` (with a space), drag
  `Install (Mac).command` into the Terminal window, and press Return.

The installer clears that block for everything else in the folder, so this
is only needed once.

Run the installer again any time: after an updated zip, or after you move
the folder (the app remembers where the folder is).

## Everyday use

- **Start it:** open **Partners Toolbox.app**.
- **See the log:** if something goes wrong before the window appears,
  open **`Launch (Mac, with Terminal).command`**. It runs the app with a
  Terminal window that shows any error output. The app's own launches also
  write to `logs/mac-launch.log`.
- **Keyboard:** shortcuts use **Command** where Windows uses **Ctrl**:
  - ⌘1, ⌘2, ⌘3 and ⌘4 switch workspaces.
  - ⌘Return in Team Maker's names box creates the teams.
  - ⌘-click adds to a selection in lists.
  - ⌘Q closes the app the same way as its close button, so running
    browsers and jobs shut down cleanly.
- **Editing files:** "Edit Centers…" and a new `.env` open in TextEdit.

## What changed for the Mac

Windows behavior is unchanged.

| Area | Change |
| --- | --- |
| Install and launch | `Install (Mac).command`, `Launch (Mac, with Terminal).command` and a generated `Partners Toolbox.app` replace `Install.bat`, the `.bat`/`.vbs` launchers, the Desktop shortcut and the optional `.exe`. |
| BrightPattern automation | The tools clear text boxes and multi-select rows with **Command** on a Mac. Before, they used Ctrl. On a Mac, Ctrl+A only moves to the start of the line, so names would have been typed after old text. (`team_maker_bp.py`, `admin_teams.py`; Playwright's `ControlOrMeta`.) |
| Window icon | Windows keeps using the `.ico` files. The Mac uses the new `assets/app_icon.png` and `sheet_slack_icon.png` (also the Dock icon) and `assets/app_icon.icns` (the app's Finder icon), all made from the same artwork. |
| Text size | Tk on a Mac sizes text at 72 dpi, so the app scales fonts to match the Windows layout. The log uses Menlo. |
| Keyboard shortcuts | Command instead of Ctrl, and ⌘Q runs the normal close. |
| Opening files | "Edit Centers…" uses the Mac's default text editor instead of a Linux-only command. |
| Window size | A zoomed window is restored as zoomed next time, as a maximized one is on Windows. |
| Background throttling | The installer turns off App Nap for Python (`defaults write org.python.python NSAppSleepDisabled -bool YES`). App Nap would otherwise slow the automation while the window is behind other windows. |
| HTTPS certificates | The launchers point Python at the `certifi` certificate bundle. python.org's Python doesn't use the Mac's own certificate store. |

## Notes

- The menu bar shows "Python" instead of "Partners Toolbox" while the app
  runs. That's how a Python app started from a script shows up; it doesn't
  affect anything.
- Team Maker opens its dedicated browser in Google Chrome or Microsoft Edge
  if either is installed in Applications, else in the downloaded Chromium,
  as on Windows.
- Staffing Moves and Team Maker select several rows at once with
  Command-click on a Mac. BrightPattern's grids treat Command like Ctrl, and
  both tools still check the selection row by row before moving anyone.

## Verified, and what wasn't

These checks ran on Linux, the only system available here:

- Every existing test suite gives the same results before and after the
  Mac changes (see "Validation" below).
- The installer was dry-run up to launching the app, with the Mac-only
  commands simulated. The Chromium download was skipped because the test
  machine's network blocks it.
- The app was started through the generated launcher with the Mac code
  paths forced on: ⌘ shortcuts, font scaling, and ⌘Q closing cleanly.

No real Mac was available. Before relying on it, run one Team Maker team
and one staffing move on the Mac and check that both work.

## Validation

These suites ran on the previous version and on this one, with identical
results:

| Suite | Previous | Mac version |
| --- | --- | --- |
| `run_offline.py` | 95 pass | 95 pass |
| `run_design_checks.py` | 285 pass | 285 pass |
| `test_reliability`, `test_ui_runtime`, `test_final_audit` | 21 / 18 / 31 pass | same |
| `test_team_maker`, `_speed`, `_fast` | 10 / 13 / 9 pass | same |
| `test_staffing_accuracy` | 55 pass | 55 pass |
| `test_agent_lists_ui`, `test_uteams_ui`, `test_project_matching_ui`, `test_layout_resize` | 7 / 4 / 3 / 4 pass | same |
| `test/run.py` | 89 pass, stops at the Slack "Delete Last" GUI check | same |
| `test_staffing_moves` | 38 pass, then a Staffing Moves panel GUI timeout | same |
| `test_workspace_ui` | 4 pass, 1 window-size check fails under a virtual display | same |

The three failures are the same on both versions and were already listed
in SPEED_OPTIMIZATIONS.md (or depend on a real display).
