"""
A simple desktop UI for sheet_chat.py: a start-time box, one checkbox per
configured call center, Start/Stop/Run Now/Delete Last Batch buttons, a
status line, and a scrolling log -- embeddable SheetToChatGUI class, same
pattern as sheet_slack_gui.py's SheetToSlackGUI (see that file's docstring
for the full rationale).

Usage (standalone):
    python src/sheet_chat_gui.py

All of the actual engine logic (reading the Google Sheet, building one
message per call center, posting to/deleting from Google Chat, the
schedule-anchor math) lives in sheet_chat.py, not here.

One-time setup (Google Cloud OAuth credentials.json with the Google Chat
API enabled and its two extra scopes) is documented in README.md. This GUI
doesn't add any way to type or store credentials -- it only reads
credentials.json/chat_token.json next to the project root, exactly as
sheet_chat.py does.

Call-center checkboxes: one per sheet_chat.SECTION_TO_SPACE entry, all
checked by default. Unchecking one means that call center's section is
skipped starting with the very next run (scheduled or Run Now) -- see
sheet_chat.run_job()'s enabled_sections parameter. This choice lives only
in this window's memory; it is never written to disk, so every checkbox
is checked again the next time the app starts.

Threading model: identical to sheet_slack_gui.py -- a background
(daemon=True) thread runs the recurring schedule loop once "Start" is
clicked; each individual run (scheduled or "Run Now") happens on its own
short-lived daemon thread via sheet_chat.run_job(), which uses a
module-level lock so overlapping runs can't stack up. Direct Tk Variable
reads (BooleanVar.get()) never happen from those background threads --
Run Now snapshots the checkboxes on the main thread before spawning its
worker, and the recurring scheduler reads a plain-list mirror
(_enabled_sections_snapshot) kept in sync by each checkbox's trace
instead of reading the BooleanVars directly -- see the comments on that
attribute and on _refresh_enabled_sections_snapshot() in the class below.
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

import sheet_chat as sc  # noqa: E402
import theme  # noqa: E402
from ui_runtime import UiDispatcher

ICON_PATH = ROOT_DIR / "assets" / "app_icon.ico"


def _center_label(header):
    """Turn a SECTION_TO_SPACE header like "InfoConnect Update:" into a
    short checkbox label ("InfoConnect"). Falls back to the header as-is
    for anything that doesn't follow the usual "<Name> Update:" pattern,
    so a differently-worded header still shows up as *something* rather
    than being silently hidden."""
    text = header.strip()
    if text.lower().endswith("update:"):
        text = text[: -len("update:")].strip()
    return text or header.strip()


class SheetToChatGUI:
    def __init__(self, root, embedded=False):
        """
        root: the Tk() root window when run standalone, or any container
        widget (e.g. a ttk.Frame) when embedded as a tab inside a larger
        combined app -- same contract as sheet_slack_gui.py's
        SheetToSlackGUI.

        embedded=False (the default) keeps standalone behavior: this class
        owns the window's title, size, icon, and close protocol.

        embedded=True skips all of that -- the embedding app owns the
        actual window chrome, and is responsible for calling this
        instance's shutdown() (not destroy()) when the shared window
        closes.
        """
        self.root = root
        self.embedded = embedded

        if not embedded:
            self.root.title(sc.APP_TITLE)
            self.root.geometry("700x420")
            try:
                if ICON_PATH.exists():
                    self.root.iconbitmap(default=str(ICON_PATH))
            except Exception:
                pass  # icon is cosmetic only, never fatal

        self.stop_event = threading.Event()
        self.scheduler_thread = None
        self.next_run_at = None

        # One checkbox per configured call center (SECTION_TO_SPACE's
        # keys, in the order they're defined there), all checked by
        # default -- see _build_widgets() for the actual widgets. Keyed by
        # the exact SECTION_HEADERS-style header text (e.g. "InfoConnect
        # Update:") so it can be passed straight to sc.run_job()'s
        # enabled_sections without translation. This is in-memory only --
        # see sheet_chat.py's module docstring for why it's never saved.
        self.center_vars = {header: tk.BooleanVar(value=True) for header in sc.SECTION_TO_SPACE}

        # A plain-list mirror of self.center_vars' checked state, kept in
        # sync (via each BooleanVar's trace, see _build_widgets) every time
        # a checkbox is clicked on the main thread. scheduler_loop() runs
        # on its own background thread and needs to read "what's checked
        # right now" on every recurring run -- reading a Tk Variable
        # directly from a non-main thread is exactly the kind of cross-
        # thread Tk call this codebase otherwise always avoids (see
        # _schedule_ui_update / self.log), so scheduler_loop reads this
        # plain list instead. Always replaced as a whole new list (never
        # mutated in place) so a read from the other thread can't observe
        # a half-updated snapshot.
        self._enabled_sections_snapshot = list(self.center_vars.keys())

        self._build_widgets()
        self._ui = UiDispatcher(self.root)

        if not embedded:
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.log(
            f"Ready. Optionally set a start time (e.g. 1:00 PM), then click Start for the "
            f"{sc.INTERVAL_MINUTES}-minute schedule, or Run Now for a one-off run."
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
                          f"{sc.INTERVAL_MINUTES} min. Leave blank to start right away.",
                   pady=(4, 0))

        centers_row = ttk.Frame(sched)
        centers_row.pack(fill="x", pady=(theme.GAP, 0))
        ttk.Label(centers_row, text="Call centers to include:").pack(anchor="w")
        center_choices = ttk.Frame(centers_row)
        center_choices.pack(fill="x")
        for index, header in enumerate(sc.SECTION_TO_SPACE):
            ttk.Checkbutton(
                center_choices, text=_center_label(header), variable=self.center_vars[header],
            ).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 12))
            # Keep _enabled_sections_snapshot in sync on every click -- see
            # its own comment in __init__ for why scheduler_loop needs this
            # plain-list mirror instead of reading the BooleanVars directly.
            self.center_vars[header].trace_add("write", lambda *_args: self._refresh_enabled_sections_snapshot())

        # -- Buttons ----------------------------------------------------------
        top = ttk.Frame(self.root, padding=(theme.PAD, theme.GAP))
        top.pack(fill="x")
        self.start_btn = ttk.Button(
            top, text=f"Start (every {sc.INTERVAL_MINUTES} min)", style="Primary.TButton", command=self.on_start
        )
        self.start_btn.pack(side="left", padx=(0, theme.GAP))
        self.stop_btn = ttk.Button(top, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, theme.GAP))
        self.run_now_btn = ttk.Button(top, text="Run Now", command=self.on_run_now)
        self.run_now_btn.pack(side="left", padx=(0, theme.GAP))
        delete_row = ttk.Frame(self.root, padding=(theme.PAD, 0, theme.PAD, theme.GAP))
        delete_row.pack(fill="x")
        self.delete_last_btn = ttk.Button(
            delete_row, text="Delete Last Batch", style="Danger.TButton", command=self.on_delete_last, state="disabled",
        )
        self.delete_last_btn.pack(side="right")

        # Inline warnings, same pattern as sheet_slack_gui.py's -- shown but
        # never fatal to starting the app, since these only matter once
        # Start/Run Now is clicked. Only credentials.json is checked here
        # (no separate token file needs to already exist -- chat_token.json
        # is created automatically on first successful login).
        if not sc.CREDENTIALS_FILE.exists():
            theme.warning(
                self.root,
                f"{sc.CREDENTIALS_FILE.name} not found in the toolbox folder. "
                "See README.md: download your "
                "Google OAuth client credentials and enable the Google Chat API before using Start or Run Now.",
                padx=theme.PAD,
            )

        # -- Log --------------------------------------------------------------
        log_frame = ttk.Frame(self.root, padding=(theme.PAD, theme.GAP, theme.PAD, theme.PAD))
        log_frame.pack(fill="both", expand=True)
        ttk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_box = theme.log_pane(log_frame)

    def _refresh_enabled_sections_snapshot(self):
        """Called on the main thread every time a call-center checkbox is
        toggled (wired up as each BooleanVar's trace in _build_widgets).
        Rebuilds _enabled_sections_snapshot as a whole new list -- see that
        attribute's comment in __init__ for why scheduler_loop reads this
        instead of the checkboxes' BooleanVars directly."""
        self._enabled_sections_snapshot = [
            header for header, var in self.center_vars.items() if var.get()
        ]

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
        # Read the checkboxes here, on the main thread (the button-click
        # handler), rather than inside _worker -- keeps every direct
        # BooleanVar.get() confined to the main thread, same reasoning as
        # _enabled_sections_snapshot for the recurring scheduler.
        enabled = [header for header, var in self.center_vars.items() if var.get()]

        def _worker():
            if sc.run_job(self.log, enabled_sections=enabled):
                self._schedule_ui_update(self._enable_delete_button)
        threading.Thread(target=_worker, daemon=True).start()

    def on_delete_last(self):
        if not sc.has_last_sent_message():
            self.log("Nothing to delete -- no messages have been posted yet this session.")
            self.delete_last_btn.configure(state="disabled")
            return

        if not messagebox.askyesno(
            sc.APP_TITLE,
            "Delete every message this app posted to Google Chat during its last run? "
            "This can't be undone.",
        ):
            return

        self.delete_last_btn.configure(state="disabled")

        def _worker():
            sc.delete_last_batch_from_chat(self.log)
            if sc.has_last_sent_message():
                # Delete failed for a reason that doesn't mean the messages
                # are gone (e.g. a network hiccup) -- let them try again.
                self._schedule_ui_update(self._enable_delete_button)
        threading.Thread(target=_worker, daemon=True).start()

    def _enable_delete_button(self):
        self.delete_last_btn.configure(state="normal")

    def on_start(self):
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            return

        anchor_text = sc.build_anchor_text(self.start_time_var.get(), self.ampm_var.get())
        try:
            first_run = sc.compute_next_run_time(anchor_text)
        except ValueError as exc:
            self.log(f"ERROR: {exc}")
            return

        if anchor_text:
            self.log(
                f"Scheduled: first run at {first_run.strftime('%a %I:%M:%S %p')}, "
                f"then every {sc.INTERVAL_MINUTES} min from that anchor."
            )
        else:
            self.log(f"Scheduled: every {sc.INTERVAL_MINUTES} min starting now.")

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

            while not self.stop_event.is_set() and datetime.now() < next_run:
                remaining = (next_run - datetime.now()).total_seconds()
                time.sleep(max(0.0, min(1.0, remaining)))

            if self.stop_event.is_set():
                return

            self.set_status("running now")
            # Reads the plain-list snapshot, not the checkboxes' BooleanVars
            # directly -- this runs on the background scheduler thread, see
            # _enabled_sections_snapshot's comment in __init__.
            if sc.run_job(self.log, enabled_sections=list(self._enabled_sections_snapshot)):
                self._schedule_ui_update(self._enable_delete_button)
            next_run = sc.advance_run_time(next_run)


def _show_fatal_error_dialog(exc_type, exc_value, exc_tb):
    """Used as Tk's callback-exception handler so errors are visible even
    when launched via pythonw (no console window to print to)."""
    err_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        messagebox.showerror(f"{sc.APP_TITLE} - unexpected error", err_text)
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
            f"{sc.APP_TITLE} - failed to start",
            f"{error_text}\n\nThis was also saved to {log_path}.",
        )
    except Exception:
        pass


def _set_windows_app_user_model_id():
    """Windows-only, best-effort -- see the identical helper in app.py for
    the full explanation."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PartnersToolbox.SheetToChat")
    except Exception:
        pass


def main():
    _set_windows_app_user_model_id()
    root = tk.Tk()
    try:
        SheetToChatGUI(root)
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
