"""
A simple desktop UI for sheet_slack.py: a start-time box, Start/Stop/Run
Now/Delete Last Message buttons, a status line, and a scrolling log --
refactored into an embeddable SheetToSlackGUI class so it can run on its
own (as it always could) OR as one tab inside a larger combined app,
exactly mirroring how gui.py's BrightPatternSyncGUI supports both modes.

Usage (standalone):
    python src/sheet_slack_gui.py

All of the actual engine logic (reading the Google Sheet, building the
message, posting to/deleting from Slack via its Web API, the
schedule-anchor math) lives in sheet_slack.py, not here -- this module is
UI only, same split as gui.py/main.py on the BrightPattern side.

One-time setup (Google Cloud OAuth credentials.json, a Slack bot token,
Python packages) is documented in README.md. This GUI doesn't add any way
to type or store those credentials -- it only reads credentials.json/
token.json/slack_token.txt next to the project root, exactly as
sheet_slack.py does.

Threading model: unchanged from the original app. A background
(daemon=True) thread runs the recurring schedule loop once "Start" is
clicked; each individual run (scheduled or "Run Now") happens on its own
short-lived daemon thread via sheet_slack.run_job(), which itself uses a
module-level lock so overlapping runs can't stack up. Being daemon
threads (unlike BrightPattern's watch-mode thread, which deliberately
isn't) is intentional: a Slack API call completes in well under a second,
so there's nothing worth blocking process exit to wait for.
"""

import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

ROOT_DIR = Path(__file__).resolve().parent.parent

import sheet_slack as ss  # noqa: E402
import theme  # noqa: E402
from ui_runtime import UiDispatcher

ICON_PATH = ROOT_DIR / "assets" / "sheet_slack_icon.ico"


class SheetToSlackGUI:
    def __init__(self, root, embedded=False):
        """
        root: the Tk() root window when run standalone, or any container
        widget (e.g. a ttk.Frame) when embedded as a tab inside a larger
        combined app -- same contract as gui.py's BrightPatternSyncGUI.

        embedded=False (the default) keeps the original standalone
        behavior: this class owns the window's title, size, icon, and
        close protocol.

        embedded=True skips all of that -- the embedding app owns the
        actual window chrome, and is responsible for calling this
        instance's shutdown() (not destroy()) when the shared window
        closes.
        """
        self.root = root
        self.embedded = embedded

        if not embedded:
            self.root.title(ss.APP_TITLE)
            self.root.geometry("700x420")
            theme.set_icon(self.root, ICON_PATH)  # icon is cosmetic only, never fatal

        self.stop_event = threading.Event()
        self.scheduler_thread = None
        self.next_run_at = None

        self._build_widgets()
        self._ui = UiDispatcher(self.root)

        if not embedded:
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.log(
            f"Ready. Optionally set a start time (e.g. 1:00 PM), then click Start for the "
            f"{ss.INTERVAL_MINUTES}-minute schedule, or Run Now for a one-off run."
        )

    def _build_widgets(self):
        theme.apply(self.root.winfo_toplevel())
        self.status_var = tk.StringVar(value="Status: stopped")
        theme.status_bar(self.root, self.status_var)

        # -- Schedule ---------------------------------------------------------
        sched = theme.section(self.root, "Schedule", pady=(theme.PAD, 0))
        time_row = ttk.Frame(sched)
        time_row.pack(fill="x")
        ttk.Label(time_row, text="Start time:").pack(side="left")
        self.start_time_var = tk.StringVar(value="")
        self.start_time_entry = ttk.Entry(time_row, textvariable=self.start_time_var, width=8)
        self.start_time_entry.pack(side="left", padx=(theme.GAP, 4))

        default_ampm = "AM" if datetime.now().hour < 12 else "PM"
        self.ampm_var = tk.StringVar(value=default_ampm)
        self.ampm_combo = ttk.Combobox(
            time_row, textvariable=self.ampm_var, values=["AM", "PM"],
            state="readonly", width=4,
        )
        self.ampm_combo.pack(side="left")
        theme.hint(sched, f"Optional. E.g. 1:00 PM: first run at that time, then every "
                          f"{ss.INTERVAL_MINUTES} min. Leave blank to start right away.",
                   pady=(4, 0))

        # -- Buttons ----------------------------------------------------------
        top = ttk.Frame(self.root, padding=(theme.PAD, theme.GAP))
        top.pack(fill="x")
        self.start_btn = ttk.Button(
            top, text=f"Start (every {ss.INTERVAL_MINUTES} min)", style="Primary.TButton", command=self.on_start
        )
        self.start_btn.pack(side="left", padx=(0, theme.GAP))
        self.stop_btn = ttk.Button(top, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, theme.GAP))
        self.run_now_btn = ttk.Button(top, text="Run Now", command=self.on_run_now)
        self.run_now_btn.pack(side="left", padx=(0, theme.GAP))
        delete_row = ttk.Frame(self.root, padding=(theme.PAD, 0, theme.PAD, theme.GAP))
        delete_row.pack(fill="x")
        self.delete_last_btn = ttk.Button(
            delete_row, text="Delete Last Message", style="Danger.TButton", command=self.on_delete_last, state="disabled",
        )
        self.delete_last_btn.pack(side="right")

        # Inline warnings, mirroring gui.py's env_error pattern -- shown but
        # never fatal to starting the app, since these only actually matter
        # once Start/Run Now is clicked.
        if not ss.CREDENTIALS_FILE.exists():
            theme.warning(
                self.root,
                f"{ss.CREDENTIALS_FILE.name} not found in the toolbox folder. See README.md: "
                "download your Google OAuth client credentials before using Start or Run Now.",
                padx=theme.PAD,
            )
        if not ss.SLACK_TOKEN_FILE.exists():
            theme.warning(
                self.root,
                f"{ss.SLACK_TOKEN_FILE.name} not found in the toolbox folder. See README.md: "
                "create a Slack app/bot token before using Start or Run Now.",
                padx=theme.PAD,
            )

        # -- Log --------------------------------------------------------------
        log_frame = ttk.Frame(self.root, padding=(theme.PAD, theme.GAP, theme.PAD, theme.PAD))
        log_frame.pack(fill="both", expand=True)
        ttk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_box = theme.log_pane(log_frame)

    # -- logging --------------------------------------------------------
    def _schedule_ui_update(self, fn):
        self._ui.submit(fn)

    def log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        def _append():
            theme.append_log(self.log_box, f"[{timestamp}] {message}\n")
        self._schedule_ui_update(_append)

    def set_status(self, text):
        self._schedule_ui_update(lambda: self.status_var.set(f"Status: {text}"))

    # -- button handlers --------------------------------------------------
    def on_run_now(self):
        def _worker():
            if ss.run_job(self.log):
                self._schedule_ui_update(self._enable_delete_button)
        threading.Thread(target=_worker, daemon=True).start()

    def on_delete_last(self):
        if not ss.has_last_sent_message():
            self.log("Nothing to delete -- no message has been posted yet this session.")
            self.delete_last_btn.configure(state="disabled")
            return

        if not messagebox.askyesno(
            ss.APP_TITLE,
            f"Delete the last message this app posted to {ss.SLACK_CHANNEL}? "
            "This can't be undone.",
        ):
            return

        self.delete_last_btn.configure(state="disabled")

        def _worker():
            ss.delete_last_message_from_slack(self.log)
            if ss.has_last_sent_message():
                # Delete failed for a reason that doesn't mean the message
                # is gone (e.g. a network hiccup) -- let them try again.
                self._schedule_ui_update(self._enable_delete_button)
        threading.Thread(target=_worker, daemon=True).start()

    def _enable_delete_button(self):
        self.delete_last_btn.configure(state="normal")

    def on_start(self):
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            return

        anchor_text = ss.build_anchor_text(self.start_time_var.get(), self.ampm_var.get())
        try:
            first_run = ss.compute_next_run_time(anchor_text)
        except ValueError as exc:
            self.log(f"ERROR: {exc}")
            return

        if anchor_text:
            self.log(
                f"Scheduled: first run at {first_run.strftime('%a %I:%M:%S %p')}, "
                f"then every {ss.INTERVAL_MINUTES} min from that anchor."
            )
        else:
            self.log(f"Scheduled: every {ss.INTERVAL_MINUTES} min starting now.")

        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.start_time_entry.configure(state="disabled")
        self.ampm_combo.configure(state="disabled")
        self.scheduler_thread = threading.Thread(
            target=self.scheduler_loop, args=(first_run,), daemon=True
        )
        self.scheduler_thread.start()

    def on_stop(self):
        self.stop_event.set()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.start_time_entry.configure(state="normal")
        self.ampm_combo.configure(state="readonly")
        self.set_status("stopped")

    def _on_close(self):
        """Standalone-mode close handler (never used when embedded=True -- see shutdown())."""
        self.shutdown()
        self.root.destroy()

    def shutdown(self):
        """Stop scheduling and discard queued UI updates before widgets close."""
        self.stop_event.set()

        self._ui.close()

    # -- scheduler --------------------------------------------------------
    def scheduler_loop(self, next_run):
        while not self.stop_event.is_set():
            self.next_run_at = next_run
            self.set_status(f"waiting - next run at {next_run.strftime('%a %I:%M:%S %p')}")

            # Wake up every second to check the stop flag and re-check the
            # wall clock (rather than counting elapsed seconds), so this
            # can't drift and correctly handles waits longer than one
            # interval (e.g. a start time hours in the future).
            while not self.stop_event.is_set() and datetime.now() < next_run:
                remaining = (next_run - datetime.now()).total_seconds()
                time.sleep(max(0.0, min(1.0, remaining)))

            if self.stop_event.is_set():
                return

            self.set_status("running now")
            if ss.run_job(self.log):
                self._schedule_ui_update(self._enable_delete_button)
            next_run = ss.advance_run_time(next_run)


def _show_fatal_error_dialog(exc_type, exc_value, exc_tb):
    """Used as Tk's callback-exception handler so errors are visible even
    when launched via pythonw (no console window to print to)."""
    err_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        messagebox.showerror(f"{ss.APP_TITLE} - unexpected error", err_text)
    except Exception:
        pass


def _report_startup_crash():
    error_text = traceback.format_exc()
    log_path = ROOT_DIR / "gui_error.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{error_text}\n")
    except Exception:
        pass

    try:
        messagebox.showerror(
            f"{ss.APP_TITLE} - failed to start",
            f"{error_text}\n\nThis was also saved to {log_path}.",
        )
    except Exception:
        pass


def _set_windows_app_user_model_id():
    """
    Without this, a Desktop shortcut's icon and this running app's
    taskbar icon can look different even though both are meant to be the
    same .ico file -- see the identical helper (and full explanation) in
    app.py. Windows-only and best-effort.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PartnersToolbox.AutoUpdate")
    except Exception:
        pass


def main():
    _set_windows_app_user_model_id()
    root = tk.Tk()
    try:
        SheetToSlackGUI(root)
    except Exception:
        _report_startup_crash()
        try:
            root.destroy()
        except Exception:
            pass
        return
    root.report_callback_exception = _show_fatal_error_dialog
    root.mainloop()


if __name__ == "__main__":
    main()
