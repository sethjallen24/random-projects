"""
A simple desktop UI for brightpattern-team-sync: one button to run all
configured team pairs in sequence, one button per individual pair, and a
"Start Watching"/"Stop Watching" pair of buttons that keep running the
same sync in the background indefinitely -- checking for newly
logged-in agents and moving them every few seconds -- until stopped.
Every mode drives the exact same login -> read agents -> select -> move
flow as `python src/main.py` (via main.py's run_pairs()/watch_pairs()),
just triggered by a click instead of a terminal command, with the live
output shown in a scrolling log pane instead of a console window.

Usage:
    python src/gui.py

Uses Tkinter (part of the Python standard library -- no extra install
needed) rather than a web framework, since this only ever needs to run
locally on the same machine as the browser automation itself; there's no
reason to add a server, a browser-based UI, or any new dependency for
what's fundamentally a handful of buttons and a log box.

Credentials and tenant settings still come from .env, exactly as before
(see env.example.txt) -- this UI does not add a way to type or store
credentials anywhere; it only reads the same .env file main.py already
requires.

Threading model: Playwright (via main.py's run_pairs()/watch_pairs())
runs synchronously in a single background thread per run, so the
Tkinter event loop in the main thread never blocks. Only one run is
allowed at a time -- every one-shot button and "Start Watching" are all
disabled while anything is already running, whether a one-shot run or a
watch session -- since two runs sharing one BrightPattern login session
concurrently would race on the same pages. Progress is captured by
redirecting sys.stdout to a small queue-backed writer for the duration
of the run, rather than threading a logger callback through
agent_desktop.py/admin_teams.py/login.py, which would have meant
touching every print() call in already carefully-tuned, live-verified
modules.

The watch-mode background thread is deliberately created with
daemon=False (unlike the one-shot run thread): a one-shot run finishes
in well under a minute even in the worst case, but a watch session runs
indefinitely, so if the window is closed while it's active, the process
needs to actually wait for that thread to notice the stop signal and
close its browser cleanly, rather than the interpreter just killing it
mid-flight and potentially leaving an orphaned browser process behind.
_on_close() sets the stop event immediately so that wait is short (at
most about one pair's processing time), not a full watch interval.
"""

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from tkinter import ttk

ROOT_DIR = Path(__file__).resolve().parent.parent

import os  # noqa: E402

from main import load_team_pairs, require_env, load_env_file, run_pairs, watch_pairs, center_name_for_pair  # noqa: E402
import theme  # noqa: E402
from ui_runtime import POLL_BATCH
from staffing_moves_gui import StaffingMovesPanel  # noqa: E402
from logout_sweep_gui import LogoutSweepPanel  # noqa: E402


class _QueueWriter:
    """A minimal file-like object: every write() is pushed onto a queue instead of a real stream."""

    def __init__(self, line_queue):
        self.line_queue = line_queue

    def write(self, text):
        if text:
            self.line_queue.put(("log", text))

    def flush(self):
        pass


ICON_PATH = ROOT_DIR / "assets" / "app_icon.ico"


class BrightPatternSyncGUI:
    def __init__(self, root, embedded=False):
        """
        root: the Tk() root window when run standalone, or any container
        widget (e.g. a ttk.Frame) when embedded as a tab inside a larger
        combined app -- Tkinter widgets only need a parent that supports
        .after()/geometry management, which both provide equally.

        embedded=False (the default, and the only mode this class used to
        support) keeps 100% of the original standalone behavior: this
        class owns the window's title, size, icon, and close protocol.

        embedded=True skips all of that -- a combined app embedding this
        as one tab among several owns the actual window chrome itself,
        and is responsible for calling shutdown() (not destroy()) on this
        instance when the shared window closes. This is what lets this
        app run side-by-side with something else entirely (e.g. inside a
        Notebook tab) without either one fighting over the window's
        title/icon/close behavior.
        """
        self.root = root
        self.embedded = embedded

        if not embedded:
            self.root.title("Active Agents")
            self.root.geometry("820x660")

            # A Tk window doesn't pick up assets/app_icon.ico just by existing
            # -- it needs to be set explicitly, and doing so is also what
            # makes Windows show this icon on the taskbar button while the
            # app is running (separate from the *Desktop shortcut's* icon,
            # which create_desktop_shortcut.vbs sets on the .lnk file itself).
            # iconbitmap(..., default=...) with a .ico path is Windows-specific
            # (X11/Tk elsewhere expects XBM/PNG) -- wrapped defensively so this
            # never blocks the app from starting on a platform/Tk build where
            # it isn't supported.
            try:
                if ICON_PATH.exists():
                    self.root.iconbitmap(default=str(ICON_PATH))
            except Exception:
                pass

        self.line_queue = queue.Queue()
        self.is_running = False
        self.is_watching = False
        self._stop_event = None
        self.buttons = []

        self._poll_after_id = None
        self._closed = False
        if not embedded:
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._log_file = self._open_persistent_log_file()

        self._load_env_and_config()
        self._build_widgets()
        self._poll_queue()

    def _open_persistent_log_file(self):
        """
        Opens logs/sync.log (creating the folder if needed) in append mode
        so every line ever shown in the Log tab is also saved to disk,
        surviving the window being closed or the log pane being scrolled
        past. Added after a real incident (2026-08-15) where an agent was
        moved into the wrong team -- but by the time it was noticed, the
        GUI window (and with it, the only copy of that run's log) was
        already closed, so there was no way to see whether the app's own
        WARNING/CRITICAL checks had actually fired for that run. Best-effort
        and never allowed to block the app from starting: if it can't be
        opened (e.g. a read-only install location), logging just falls
        back to the in-memory Log tab only, same as before this existed.
        """
        try:
            log_dir = ROOT_DIR / "logs"
            log_dir.mkdir(exist_ok=True)
            log_file = open(log_dir / "sync.log", "a", encoding="utf-8")
            log_file.write(f"\n{'=' * 70}\nGUI session started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_file.flush()
            return log_file
        except Exception as exc:
            print(f"(could not open logs/sync.log -- this run's log will only be visible in the Log tab: {exc})")
            return None

    def _on_close(self):
        """
        Standalone-mode close handler (never used when embedded=True --
        an embedding app calls shutdown() itself, then destroys the one
        shared root on its own schedule). Just shutdown() + destroy().
        """
        self.shutdown()
        self.root.destroy()

    def shutdown(self):
        """
        Everything that needs to happen before this tab/window goes away,
        whether that's this class's own _on_close() (standalone mode) or
        a combined app's close handler (embedded mode) tearing down every
        tab before destroying the one shared root. Safe to call more than
        once (e.g. a test's own cleanup calling it again after a watch
        session already stopped itself) -- every step guards against its
        own already-done state.

        - If watch mode is active, signals it to stop right away. The
          watch thread is non-daemon (see the module docstring), so the
          process actually waits for it to notice and close its browser
          before fully exiting -- setting the stop event here (instead of
          leaving it to whatever's left of the current watch interval) is
          what keeps that wait short.
        - Cancels the pending self.root.after(...) poll. Without this, Tk
          still fires that scheduled callback after the window (and its
          Tcl command) is gone, printing a harmless but alarming-looking
          'invalid command name "...poll_queue"' error to the console on
          every normal close.
        - Closes the persistent log file handle.
        """
        if self._closed:
            return
        self._closed = True
        if self.is_watching and self._stop_event is not None:
            self._append_log("\n--- Closing: stopping watch mode... ---\n")
            self._stop_event.set()

        if getattr(self, "staffing", None) is not None:
            self.staffing.shutdown()
        if getattr(self, "cleanup", None) is not None:
            self.cleanup.shutdown()

        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None

        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def _load_env_and_config(self):
        self.env_error = None
        try:
            # load_env_file (not a plain load_dotenv call) so a first-time
            # .env gets created and opened automatically, and a near-miss
            # filename (see its docstring in main.py -- most commonly a
            # leftover extension from renaming env.example.txt through
            # Windows Explorer) is used directly with no rename needed,
            # right here in the GUI too, not just the CLI.
            load_env_file()
            self.base_url = require_env("BP_BASE_URL").rstrip("/")
            self.username = require_env("BP_USERNAME")
            self.password = require_env("BP_PASSWORD")
        except Exception as e:
            self.env_error = str(e)
            self.base_url = self.username = self.password = None

        def positive_setting(key, fallback):
            try:
                value = int(os.environ.get(key, str(fallback)))
                return value if value > 0 else fallback
            except (ValueError, TypeError):
                return fallback
        self.timeout_ms = positive_setting("TEAM_SELECT_TIMEOUT_MS", 20000)
        self.chromium_path = os.environ.get("TEST_CHROMIUM_PATH")
        self.default_dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
        self.default_headless = os.environ.get("HEADLESS", "true").strip().lower() != "false"
        self.default_watch_interval = positive_setting("WATCH_INTERVAL_SECONDS", 30)

        self.config_error = None
        try:
            self.team_pairs = load_team_pairs()
        except Exception as e:
            self.config_error = str(e)
            self.team_pairs = []

        # Each pair's short, recognizable name -- "611", "InfoConnect",
        # "Squad", "Tagbilaran" -- used to label both its one-shot button
        # and its watch-mode toggle checkbox, instead of the full
        # "(611 Agents All) -> (611 Agents Logged In)" team names.
        self.pair_centers = [center_name_for_pair(pair) for pair in self.team_pairs]

    def _build_widgets(self):
        theme.apply(self.root.winfo_toplevel())

        # Keep the working version’s normal default; dry run is available through the checkbox.
        self.dry_run_var = tk.BooleanVar(value=False)
        self.headless_var = tk.BooleanVar(value=self.default_headless)

        # Status bar first, packed to the bottom, so it reserves its slot
        # there regardless of what else gets packed afterward -- it stays
        # visible no matter which sub-tab is selected.
        self.status_var = tk.StringVar(value="Idle")
        theme.status_bar(self.root, self.status_var)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        # Staffing Moves first (and shown when the app opens), then this tab's own
        # "Logged-In" controls (Active Agents), the Logged-Out Cleanup, and the shared Log.
        staffing_tab = ttk.Frame(self.notebook)
        controls_tab = ttk.Frame(self.notebook)
        cleanup_tab = ttk.Frame(self.notebook)
        log_tab = ttk.Frame(self.notebook)
        self.notebook.add(staffing_tab, text="Staffing Moves")
        self.notebook.add(controls_tab, text="Logged-In")
        self.notebook.add(cleanup_tab, text="Logged-Out Cleanup")
        self.notebook.add(log_tab, text="Log")
        self.notebook.select(staffing_tab)

        top = ttk.Frame(controls_tab, padding=(theme.PAD, theme.PAD, theme.PAD, 0))
        top.pack(fill="x")
        target_text = f"Target: {self.base_url}" if self.base_url else "Target: (not configured -- see .env)"
        target = ttk.Label(top, text=target_text, style="Target.TLabel", wraplength=420)
        target.pack(fill="x")
        theme.wrap_label(target)
        options = ttk.Frame(top)
        options.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(options, text="Dry run (log only, move nobody)", variable=self.dry_run_var
                        ).pack(side="left")
        ttk.Checkbutton(options, text="Show browser window", variable=self.headless_var,
                        onvalue=False, offvalue=True).pack(side="left", padx=(16, 0))

        # -- Start / Stop ----------------------------------------------------------
        # One toggle. While started, each center's "(Agents All)" team is checked
        # in Agent Desktop every N seconds; Contact Center is only touched -- and
        # agents only moved to "(Agents Logged In)" -- when someone there has
        # logged in (main.process_pair returns early when nobody has).
        watch_box = theme.section(controls_tab, "Move agents as they log in")
        theme.hint(watch_box, "Start checks each center's \"(Agents All)\" team at the configured interval and moves anyone who "
                              "logs in to that center's \"(Agents Logged In)\" team. Nothing is moved until someone "
                              "logs in. Stop ends it.")

        centers_row = ttk.Frame(watch_box)
        centers_row.pack(fill="x", pady=(theme.GAP, 0))
        ttk.Label(centers_row, text="Centers:").pack(side="left", padx=(0, theme.GAP))
        # One checkbox per configured pair, checked by default; unchecking a
        # center leaves its pair out of the next start.
        self.center_vars = []
        for center in self.pair_centers:
            var = tk.BooleanVar(value=True)
            self.center_vars.append(var)
            cb = ttk.Checkbutton(centers_row, text=center, variable=var)
            cb.pack(side="left", padx=(0, 12))
            self.buttons.append(cb)

        interval_row = ttk.Frame(watch_box)
        interval_row.pack(fill="x", pady=(theme.GAP, 0))
        ttk.Label(interval_row, text="Check every").pack(side="left")
        self.watch_interval_var = tk.StringVar(value=str(self.default_watch_interval))
        watch_interval_entry = ttk.Entry(interval_row, width=6, textvariable=self.watch_interval_var)
        watch_interval_entry.pack(side="left", padx=theme.GAP)
        ttk.Label(interval_row, text="seconds").pack(side="left")
        self.buttons.append(watch_interval_entry)

        # Not in self.buttons: it stays clickable (as Stop) while running.
        self.watch_toggle = ttk.Button(watch_box, text="Start", style="Primary.TButton", command=self._on_toggle_watch)
        self.watch_toggle.pack(fill="x", pady=(theme.GAP, 0))
        if self.config_error:
            theme.warning(watch_box, f"Could not load config/teamPairs.json: {self.config_error}")

        if self.env_error:
            theme.warning(controls_tab, f"{self.env_error}", padx=theme.PAD, pady=(theme.GAP, 0))
            for btn in self.buttons:
                btn.state(["disabled"])
            self.watch_toggle.state(["disabled"])

        log_frame = ttk.Frame(log_tab, padding=theme.PAD)
        log_frame.pack(fill="both", expand=True)
        ttk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_widget = theme.log_pane(log_frame)

        # Built last: it reads env_error and the Dry run / Show browser
        # checkboxes created above. It shares this tab's run lock through
        # is_running / _set_buttons_enabled, and pauses + resumes watch mode
        # (via _on_stop_watch / _on_start_watch) whenever it has moves to run.
        self.staffing = StaffingMovesPanel(staffing_tab, self)
        # Shares Staffing Moves' BrightPattern worker, login and job queue.
        self.cleanup = LogoutSweepPanel(cleanup_tab, self, self.staffing)

    def _append_log(self, text):
        from login import redact
        text = redact(text)                  # never show or save a typed password (Playwright call logs)
        theme.append_log(self.log_widget, text)

        # Every line shown in the Log tab -- one-shot runs, watch mode
        # cycles, WARNING/CRITICAL checks, everything -- passes through
        # here, so mirroring it to logs/sync.log (see
        # _open_persistent_log_file) is enough to capture the full
        # picture on disk too, without touching every print() call
        # elsewhere in the app.
        if self._log_file is not None:
            try:
                self._log_file.write(text)
                self._log_file.flush()
            except Exception:
                pass

    def _set_buttons_enabled(self, enabled):
        state = ["!disabled"] if enabled else ["disabled"]
        for btn in self.buttons:
            btn.state(state)
        self._paint_watch_toggle()

    def _paint_watch_toggle(self):
        """Start (idle) / Stop (watching) / Stopping... -- greyed while something else uses BrightPattern."""
        toggle = getattr(self, "watch_toggle", None)
        if toggle is None:
            return
        stopping = self.is_watching and self._stop_event is not None and self._stop_event.is_set()
        if self.is_watching:
            toggle.configure(text="Stopping..." if stopping else "Stop")
            toggle.state(["disabled"] if stopping else ["!disabled"])
        else:
            toggle.configure(text="Start")
            toggle.state(["disabled"] if (self.is_running or self.env_error) else ["!disabled"])

    def _on_toggle_watch(self):
        if self.is_watching:
            self._on_stop_watch()
        else:
            self._on_start_watch()

    def _on_run_all(self):
        self._start_run(self.team_pairs, "all 4 pairs")

    def _on_run_one(self, pair):
        self._start_run([pair], f"{pair['sourceTeam']} -> {pair['destinationTeam']}")

    def _start_run(self, pairs_to_run, description):
        if self.is_running or self.is_watching:
            return
        if not pairs_to_run:
            messagebox.showinfo("Nothing to run", "No team pairs are configured.")
            return

        dry_run = self.dry_run_var.get()
        headless = self.headless_var.get()

        self.is_running = True
        self._set_buttons_enabled(False)
        self.status_var.set(f"Running: {description}...")
        self._append_log(f"\n{'=' * 60}\nStarting: {description} (dry_run={dry_run}, headless={headless})\n")

        thread = threading.Thread(
            target=self._run_worker, args=(pairs_to_run, dry_run, headless), daemon=True
        )
        thread.start()

    def _run_worker(self, pairs_to_run, dry_run, headless):
        real_stdout = sys.stdout
        sys.stdout = _QueueWriter(self.line_queue)
        try:
            succeeded = run_pairs(
                pairs_to_run,
                self.base_url,
                self.username,
                self.password,
                dry_run,
                headless,
                self.timeout_ms,
                self.chromium_path,
            )
        except Exception as e:
            self.line_queue.put(("log", f"\nFATAL ERROR (not caught inside run_pairs): {e}\n"))
            succeeded = False
        finally:
            sys.stdout = real_stdout
        self.line_queue.put(("done", succeeded))

    def _read_watch_interval(self):
        """Returns a positive int from the "Check every ___ seconds" field, or None (after showing an error) if it isn't one."""
        raw = self.watch_interval_var.get().strip()
        try:
            value = int(raw)
            if value < 1:
                raise ValueError
            return value
        except ValueError:
            messagebox.showerror(
                "Invalid interval",
                f"'{raw}' is not a valid number of seconds. Enter a whole number of 1 or more.",
            )
            return None

    def _selected_pairs_and_centers(self):
        """Returns (pairs, center_names) for just the pairs whose center checkbox is currently checked."""
        pairs, centers = [], []
        for pair, center, var in zip(self.team_pairs, self.pair_centers, self.center_vars):
            if var.get():
                pairs.append(pair)
                centers.append(center)
        return pairs, centers

    def _on_start_watch(self):
        if self.is_running or self.is_watching:
            return
        if not self.team_pairs:
            messagebox.showinfo("Nothing to watch", "No team pairs are configured.")
            return

        pairs_to_watch, centers_to_watch = self._selected_pairs_and_centers()
        if not pairs_to_watch:
            messagebox.showinfo("Nothing to watch", "No centers are selected. Check at least one center's box before starting.")
            return

        interval = self._read_watch_interval()
        if interval is None:
            return

        dry_run = self.dry_run_var.get()
        headless = self.headless_var.get()

        self._stop_event = threading.Event()
        self.is_running = True
        self.is_watching = True
        self._set_buttons_enabled(False)
        self.status_var.set(
            f"Watching {', '.join(centers_to_watch)} -- checking every {interval}s for newly logged-in agents..."
        )
        self._append_log(
            f"\n{'=' * 60}\nStarting watch mode for: {', '.join(centers_to_watch)} "
            f"(checking every {interval}s, dry_run={dry_run}, headless={headless}). "
            "Click 'Stop Watching' any time to end it.\n"
        )

        # Deliberately NOT a daemon thread -- see the module docstring for
        # why a watch session (unlike the one-shot run above) needs the
        # process to actually wait for it to notice a stop request and
        # close its browser, rather than being killed mid-flight if the
        # window closes first.
        thread = threading.Thread(
            target=self._watch_worker, args=(pairs_to_watch, interval, dry_run, headless), daemon=False
        )
        thread.start()

    def _on_stop_watch(self):
        owner = getattr(self, "_staffing_watch_owner", None)
        if owner is not None:
            owner.stop_logged_in()
            self._paint_watch_toggle()
            return
        if not self.is_watching or self._stop_event is None:
            return
        self._stop_event.set()
        self._paint_watch_toggle()
        self.status_var.set("Stopping watch mode...")
        self._append_log("\n--- Stop requested: finishing the pair currently in progress, then stopping... ---\n")

    def _watch_worker(self, pairs_to_watch, interval, dry_run, headless):
        real_stdout = sys.stdout
        sys.stdout = _QueueWriter(self.line_queue)
        try:
            watch_pairs(
                pairs_to_watch,
                self.base_url,
                self.username,
                self.password,
                dry_run,
                headless,
                self.timeout_ms,
                interval,
                self._stop_event,
                self.chromium_path,
            )
        except Exception as e:
            self.line_queue.put(("log", f"\nFATAL ERROR (not caught inside watch_pairs): {e}\n"))
        finally:
            sys.stdout = real_stdout
        self.line_queue.put(("watch_done", None))

    def _poll_queue(self):
        if self._closed:
            return
        logs = []
        try:
            for _ in range(POLL_BATCH):
                kind, payload = self.line_queue.get_nowait()
                if kind == "log":
                    logs.append(payload)
                    continue
                if logs:
                    self._append_log("".join(logs))
                    logs.clear()
                if kind == "done":
                    self.is_running = False
                    self._set_buttons_enabled(True)
                    self.status_var.set("Done -- succeeded" if payload else "Done -- FAILED, see log above")
                elif kind == "watch_done":
                    self.is_running = False
                    self.is_watching = False
                    self._stop_event = None
                    self._set_buttons_enabled(True)
                    self.status_var.set("Watch mode stopped.")
        except queue.Empty:
            pass
        finally:
            try:
                if logs and not self._closed:
                    self._append_log("".join(logs))
            finally:
                if not self._closed:
                    self._poll_after_id = self.root.after(100, self._poll_queue)


def _set_windows_app_user_model_id():
    """
    Without this, a Desktop shortcut's icon and this running app's
    taskbar icon can look different even though both are meant to be
    assets/app_icon.ico -- see the identical helper (and full
    explanation) in app.py. Windows-only and best-effort.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PartnersToolbox.ActiveAgents")
    except Exception:
        pass


def main():
    _set_windows_app_user_model_id()
    root = tk.Tk()
    try:
        BrightPatternSyncGUI(root)
    except Exception:
        # The Desktop-icon launch path (run_gui_hidden.vbs -> pythonw)
        # runs with no console at all, unlike the old start_gui.bat path
        # -- so a startup crash here would otherwise be completely
        # invisible (no window, no console, nothing). Log it to a file
        # and show it in a message box (which works fine under pythonw;
        # only the console is gone) so it's still diagnosable.
        _report_startup_crash()
        try:
            root.destroy()
        except Exception:
            pass
        return
    root.mainloop()


def _report_startup_crash():
    import traceback

    error_text = traceback.format_exc()
    log_path = ROOT_DIR / "gui_error.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{error_text}\n")
    except Exception:
        pass

    try:
        messagebox.showerror(
            "Active Agents -- failed to start",
            f"{error_text}\n\nThis was also saved to {log_path}.",
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
