"""
Team Maker tab: creates BrightPattern teams -- name, the chosen center's
supervisors, and the team's campaign (Services tab) -- by driving Contact
Center Administrator in its own dedicated, normally hidden browser.

Usage:
    python src/team_maker_gui.py      (standalone window)
    python src/app.py                 (as the "Team Maker" tab of Partners Toolbox)

For each team name typed (one per line):
  - team name   = what you typed + " " + the selected center (not doubled if
                  you already typed the center on the end)
  - supervisors = the ticked names from config/teamMakerCenters.txt for that center
  - campaign    = what you typed WITHOUT the center -- the pattern every
                  existing team follows ("...Sept 24 Land" for the team
                  "...Sept 24 Land Tagbilaran")
If the requested campaign cannot be resolved, creation stops before saving.

Credentials: this tab's own BrightPattern login lives in config/teamMaker.json
(team_maker_config.py) -- NOT Active Agents' .env -- so the two tabs can use
different accounts. The first time the tab is shown with no login saved, the
Login Settings window opens by itself, prefilled from .env when possible.

Threading: Playwright's sync API must stay on one thread, so every browser
action runs on a single long-lived worker thread that keeps the browser
signed in between runs. The Tk main thread talks to it only through queues
(same idea as gui.py's line_queue).
"""

import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

ROOT_DIR = Path(__file__).resolve().parent.parent

import team_maker_config as tmc  # noqa: E402
import theme  # noqa: E402
from ui_runtime import POLL_BATCH

TAB_TITLE = "Team Maker"
ICON_PATH = ROOT_DIR / "assets" / "app_icon.ico"


def _stamp(msg):
    return f"[{time.strftime('%H:%M:%S')}] {msg}\n"


class _Worker:
    """One background thread for all browser work (Playwright's sync API requires it)."""

    def __init__(self, log):
        self.log = log
        self.jobs = queue.Queue()
        self.bot = None
        self.closed = False
        self.active_stop = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True, name="TeamMakerWorker")
        self.thread.start()

    def submit(self, fn, on_done=None, stop_event=None):
        future = Future()
        with self.lock:
            if self.closed:
                raise RuntimeError("Team Maker is closed.")
            self.jobs.put((fn, on_done, future, stop_event or threading.Event()))
        return future

    def _run(self):
        while True:
            fn, on_done, future, stop = self.jobs.get()
            if fn is None:
                break
            self.active_stop = stop
            try:
                if self.closed or stop.is_set():
                    raise RuntimeError("Team creation cancelled before starting.")
                result, err = fn(self), None
            except Exception as e:  # every failure is reported back to the UI
                result, err = None, e
            self.active_stop = None
            if err is not None:
                future.set_exception(err)
            else:
                future.set_result(result)
            if on_done:
                try:
                    on_done(result, err)
                except Exception as callback_error:
                    self.log(f"Team Maker result callback failed: {callback_error}")
        self.reset_bot()

    def get_bot(self, settings):
        from team_maker_bp import BrightPatternBot  # lazy: Playwright loads only when needed
        if self.bot is not None and getattr(self, "bot_settings", None) != settings:
            self.reset_bot()
        if self.bot is None:
            self.bot = BrightPatternBot(settings, log=self.log)
            self.bot_settings = dict(settings)
        self.bot.stop_event = self.active_stop
        return self.bot

    def reset_bot(self):
        if self.bot is not None:
            try:
                self.bot.close()
            except Exception:
                pass
        self.bot = None

    def stop(self):
        with self.lock:
            self.closed = True
            if self.active_stop:
                self.active_stop.set()
            self.jobs.put((None, None, None, None))


class LoginSettingsDialog(tk.Toplevel):
    """
    Edits config/teamMaker.json (the same file you can also edit in Notepad).
    Other tabs with their own BrightPattern login (Logged-Out Cleanup) reuse it
    with their own tab_title / heading / settings_path / load_fn / save_fn.
    """

    def __init__(self, parent, settings, first_run=False, tab_title=None, heading=None, settings_path=None,
                 load_fn=None, save_fn=None):
        super().__init__(parent)
        self._tab_title = tab_title or TAB_TITLE
        self._load_fn = load_fn or tmc.load_settings
        self._save_fn = save_fn or tmc.save_settings
        self.title(f"{self._tab_title} - BrightPattern login" + (" (first-time setup)" if first_run else ""))
        self.resizable(False, False)
        self.saved = False
        try:
            self.transient(parent.winfo_toplevel())
        except Exception:
            pass

        frm = ttk.Frame(self, padding=14)
        frm.grid(sticky="nsew")
        ttk.Label(frm, text=heading or "BrightPattern account for the Team Maker tab", font=("", 10, "bold")
                  ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text=f"Saved to {theme.short_path(settings_path or tmc.SETTINGS_PATH, ROOT_DIR)} "
                            "(plain text, editable).",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.url_var = tk.StringVar(value=settings.get("admin_url", ""))
        self.user_var = tk.StringVar(value=settings.get("username", ""))
        self.pw_var = tk.StringVar(value=settings.get("password", ""))
        self.reveal_var = tk.BooleanVar(value=False)

        rows = (("Admin site address:", self.url_var, 46, None),
                ("Username:", self.user_var, 30, None),
                ("Password:", self.pw_var, 30, "*"))
        for r, (label, var, width, show) in enumerate(rows, start=2):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="e", padx=(0, 8), pady=4)
            entry = ttk.Entry(frm, textvariable=var, width=width, show=show or "")
            entry.grid(row=r, column=1, sticky="w", pady=4)
            if show:
                self.pw_entry = entry
        ttk.Checkbutton(frm, text="Show password", variable=self.reveal_var,
                        command=lambda: self.pw_entry.configure(show="" if self.reveal_var.get() else "*")
                        ).grid(row=5, column=1, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Save", style="Primary.TButton", command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 6))

        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        self._center_over(parent)
        self.grab_set()
        self.focus_force()

    def _center_over(self, parent):
        try:
            self.update_idletasks()
            top = parent.winfo_toplevel()
            x = top.winfo_rootx() + (top.winfo_width() - self.winfo_reqwidth()) // 2
            y = top.winfo_rooty() + (top.winfo_height() - self.winfo_reqheight()) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass

    def _save(self):
        data = self._load_fn()
        data.update(admin_url=self.url_var.get().strip(), username=self.user_var.get().strip(),
                    password=self.pw_var.get())
        if not tmc.settings_complete(data):
            messagebox.showwarning(self._tab_title, "Please fill in the address, username and password.", parent=self)
            return
        self._save_fn(data)
        self.saved = True
        self.destroy()


class TeamMakerGUI:
    def __init__(self, root, embedded=False):
        """Same contract as gui.py's BrightPatternSyncGUI: `root` is the Tk() window when
        standalone, or a ttk.Frame when embedded; embedded=True leaves window chrome to the
        host, which calls shutdown() when its window closes."""
        self.root = root
        self.embedded = embedded
        if not embedded:
            self.root.title(TAB_TITLE)
            self.root.geometry("820x720")
            try:
                if ICON_PATH.exists():
                    self.root.iconbitmap(default=str(ICON_PATH))
            except Exception:
                pass

        self.line_queue = queue.Queue()
        self.is_running = False
        self.buttons = []
        self.centers = {}
        self.sup_vars = []
        self._first_shown = False
        self._poll_after_id = None
        self._closed = False

        self.settings = tmc.load_settings()
        self._log_file = self._open_persistent_log_file()
        self._build_widgets()
        self.worker = _Worker(lambda msg: self.line_queue.put(("log", _stamp(msg))))
        self.reload_centers()
        self._poll_queue()

        if not embedded:
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Sign in (or ask for a login) the first time this tab is actually shown,
        # so opening the toolbox for another tab never starts a hidden browser here.
        self.outer.bind("<Map>", self._on_first_shown, add="+")

    # ------------------------------------------------------------------ setup
    def _open_persistent_log_file(self):
        """logs/team_maker.log, appended; best-effort like gui.py's logs/sync.log."""
        try:
            tmc.LOG_DIR.mkdir(exist_ok=True)
            f = open(tmc.LOG_DIR / "team_maker.log", "a", encoding="utf-8")
            f.write(f"\n{'=' * 70}\nGUI session started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.flush()
            return f
        except Exception:
            return None

    def _build_widgets(self):
        theme.apply(self.root.winfo_toplevel())
        self.outer = ttk.Frame(self.root)
        self.outer.pack(fill="both", expand=True)

        self.headless_var = tk.BooleanVar(value=not self.settings.get("show_browser", False))
        self.status_var = tk.StringVar(value="Idle")
        theme.status_bar(self.outer, self.status_var)

        self.notebook = ttk.Notebook(self.outer)
        self.notebook.pack(fill="both", expand=True)
        controls = ttk.Frame(self.notebook)
        log_tab = ttk.Frame(self.notebook)
        self.notebook.add(controls, text="Controls")
        self.notebook.add(log_tab, text="Log")

        # -- target row -----------------------------------------------------------
        top = ttk.Frame(controls, padding=(theme.PAD, theme.PAD, theme.PAD, 0))
        top.pack(fill="x")
        self.target_var = tk.StringVar()
        target = ttk.Label(top, textvariable=self.target_var, style="Target.TLabel", wraplength=420)
        target.pack(fill="x", pady=(0, theme.GAP))
        theme.wrap_label(target)
        options_row = ttk.Frame(top)
        options_row.pack(fill="x")
        login_btn = ttk.Button(options_row, text="Login Settings...", command=self.open_login_settings)
        login_btn.pack(side="right")
        cb = ttk.Checkbutton(options_row, text="Show browser window", variable=self.headless_var,
                             onvalue=False, offvalue=True, command=self._on_show_browser_changed)
        cb.pack(side="right", padx=(0, 16))
        self.buttons += [login_btn, cb]
        self.warning_label = theme.warning(controls, "", padx=theme.PAD)
        self.warning_label.pack_forget()
        self._refresh_target()
        cache_row = ttk.Frame(top)
        cache_row.pack(fill="x", pady=(theme.GAP, 0))
        self.cache_var = tk.StringVar()
        cache_label = ttk.Label(cache_row, textvariable=self.cache_var, style="Hint.TLabel", wraplength=650)
        self.cache_button = ttk.Button(cache_row, text="Refresh assignments", command=self.refresh_assignments)
        self.cache_button.pack(side="right", padx=(theme.GAP, 0))
        cache_label.pack(side="left", fill="x", expand=True)
        theme.wrap_label(cache_label)
        self.buttons.append(self.cache_button)
        self._cache_next = 0

        # -- team names -------------------------------------------------------------
        form = ttk.Frame(controls)
        form.pack(fill="both", expand=True)
        form.columnconfigure(0, weight=1, uniform="team_form")
        form.columnconfigure(1, weight=1, uniform="team_form")
        form.rowconfigure(0, weight=1)
        names_column = ttk.Frame(form)
        names_column.grid(row=0, column=0, sticky="nsew")
        center_column = ttk.Frame(form)
        center_column.grid(row=0, column=1, sticky="nsew")
        names_box = theme.section(names_column, "Project names", fill="both", expand=True)
        theme.hint(names_box, "One team name per line. The selected center is added to the end of each name; "
                              "the name as typed (without the center) is the campaign it's added to.")
        self.preview_var = tk.StringVar(value="")
        preview = ttk.Label(names_box, textvariable=self.preview_var, style="Target.TLabel", justify="left",
                            wraplength=420)
        preview.pack(side="bottom", fill="x", pady=(theme.GAP, 0))
        theme.wrap_label(preview)
        editor = ttk.Frame(names_box)
        editor.pack(fill="both", expand=True, pady=(theme.GAP, 0))
        self.names_text = theme.text_input(editor, height=8, width=28)
        self.names_text.pack(side="left", fill="both", expand=True)
        names_scroll = ttk.Scrollbar(editor, orient="vertical", command=self.names_text.yview)
        names_scroll.pack(side="right", fill="y")
        self.names_text.configure(yscrollcommand=names_scroll.set)
        self.names_text.bind("<<Modified>>", self._on_names_modified)
        self.names_text.bind("<Control-Return>", lambda e: (self.on_create(), "break")[1])

        # -- center + supervisors -----------------------------------------------------
        center_box = theme.section(center_column, "Center & supervisors", fill="both", expand=True)
        center_row = ttk.Frame(center_box)
        center_row.pack(fill="x")
        ttk.Label(center_row, text="Center:").pack(side="left", padx=(0, theme.GAP))
        self.center_var = tk.StringVar()
        self.center_combo = ttk.Combobox(center_row, textvariable=self.center_var, state="readonly", width=22)
        self.center_combo.pack(side="left", fill="x", expand=True)
        self.center_combo.bind("<<ComboboxSelected>>", lambda e: (self._show_supervisors(), self._update_preview(), self._update_cache_status()))
        self.buttons.append(self.center_combo)
        center_actions = ttk.Frame(center_box)
        center_actions.pack(fill="x", pady=(theme.GAP, 0))
        for text, cmd in (("Reload Centers", self.reload_centers), ("Edit Centers...", self.edit_centers)):
            b = ttk.Button(center_actions, text=text, command=cmd)
            b.pack(side="right", padx=(theme.GAP, 0))
            self.buttons.append(b)

        sup_head = ttk.Frame(center_box)
        sup_head.pack(fill="x", pady=(theme.GAP, 2))
        self.sup_label_var = tk.StringVar(value="Supervisors:")
        ttk.Label(sup_head, textvariable=self.sup_label_var).pack(side="left")
        for text, value in (("Clear All", False), ("Select All", True)):
            b = ttk.Button(sup_head, text=text, command=lambda v=value: self._set_all(v))
            b.pack(side="right", padx=(theme.GAP, 0))
            self.buttons.append(b)

        list_box = ttk.Frame(center_box, relief="solid", borderwidth=1)
        list_box.pack(fill="both", expand=True)
        self.sup_canvas = tk.Canvas(list_box, highlightthickness=0, height=150, background=theme.COLORS["field"])
        sb = ttk.Scrollbar(list_box, orient="vertical", command=self.sup_canvas.yview)
        self.sup_frame = tk.Frame(self.sup_canvas, background=theme.COLORS["field"], padx=6, pady=4)
        self.sup_frame.bind("<Configure>",
                            lambda e: self.sup_canvas.configure(scrollregion=self.sup_canvas.bbox("all")))
        self.sup_canvas.create_window((0, 0), window=self.sup_frame, anchor="nw")
        self.sup_canvas.configure(yscrollcommand=sb.set)
        self.sup_canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for widget in (self.sup_canvas, self.sup_frame):
            self._bind_wheel(widget)

        # -- create ------------------------------------------------------------------
        create_row = ttk.Frame(controls, padding=(theme.PAD, theme.GAP, theme.PAD, theme.PAD))
        create_row.pack(side="bottom", fill="x", before=top)
        self.create_btn = ttk.Button(create_row, text="Create Team(s)", style="Primary.TButton",
                                     command=self.on_create)
        self.create_btn.pack(side="right")
        theme.hint(create_row, "Review the project names and center before creating teams.", side="left", expand=True)
        self.buttons.append(self.create_btn)

        # -- log ---------------------------------------------------------------------
        log_frame = ttk.Frame(log_tab, padding=theme.PAD)
        log_frame.pack(fill="both", expand=True)
        ttk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_widget = theme.log_pane(log_frame)

    def _refresh_target(self):
        if tmc.settings_complete(self.settings):
            self.target_var.set(f"Target: {self.settings['admin_url']}  (as {self.settings['username']})")
            self.warning_label.pack_forget()
        else:
            self.target_var.set("Target: (not configured)")
            self.warning_label.configure(text="No BrightPattern login saved for this tab yet -- click Login Settings...")
            self.warning_label.pack(anchor="w", padx=theme.PAD, pady=(theme.GAP, 0))

    def _bind_wheel(self, widget):
        widget.bind("<MouseWheel>", self._on_wheel, add="+")
        widget.bind("<Button-4>", self._on_wheel, add="+")
        widget.bind("<Button-5>", self._on_wheel, add="+")


    def _on_wheel(self, event):
        if getattr(event, "num", None) in (4, 5):
            steps = -1 if event.num == 4 else 1
        else:
            delta = event.delta
            steps = (-1 if delta > 0 else 1) * max(1, abs(delta) // 120) if delta else 0
        self.sup_canvas.yview_scroll(steps, "units")
        return "break"

    # -------------------------------------------------------------- lifecycle
    def _on_first_shown(self, _event=None):
        if self._first_shown or self._closed:
            return
        self._first_shown = True
        self.root.after_idle(self._first_show_actions)  # let the tab finish drawing first

    def _first_show_actions(self):
        if self._closed:
            return
        if tmc.settings_complete(self.settings):
            self._warm_up()
        else:
            self.open_login_settings(first_run=True)

    def _on_close(self):
        self.shutdown()
        self.root.destroy()

    def shutdown(self):
        """Safe to call more than once: stops the worker (closing its browser) and the log file."""
        if self._closed:
            return
        self._closed = True
        try:
            self.worker.stop()
            self.worker.thread.join(timeout=5)
        except Exception:
            pass
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

    # ---------------------------------------------------------------- logging
    def _append_log(self, text):
        from login import redact
        text = redact(text)                  # never show or save a typed password (Playwright call logs)
        theme.append_log(self.log_widget, text)
        if self._log_file is not None:
            try:
                self._log_file.write(text)
                self._log_file.flush()
            except Exception:
                pass

    def _note(self, msg):
        """Log a line from the UI thread."""
        self._append_log(_stamp(msg))

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
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "idle_status" and not self.is_running:
                    self.status_var.set(payload)
                elif kind == "batch_done":
                    self._finish_batch(*payload)
                elif kind == "cache_done":
                    count, error = payload
                    self._set_busy(False)
                    self._update_cache_status()
                    if error:
                        self.status_var.set("Assignment refresh failed — see Log")
                        self._note(f"Assignment refresh failed: {error}")
                    else:
                        self.status_var.set(f"Assignments refreshed: {count} project team(s).")
        except queue.Empty:
            pass
        finally:
            try:
                if logs and not self._closed:
                    self._append_log("".join(logs))
            finally:
                if not self._closed:
                    if time.monotonic() >= getattr(self, "_cache_next", float("inf")):
                        self._update_cache_status()
                        self._cache_next = time.monotonic() + 30
                    self._poll_after_id = self.root.after(100, self._poll_queue)

    def _set_busy(self, busy):
        self.is_running = busy
        state = ["disabled"] if busy else ["!disabled"]
        for w in self.buttons:
            try:
                w.state(state)
            except Exception:
                pass
        if not busy:
            self.center_combo.state(["readonly"])
        self.names_text.configure(state="disabled" if busy else "normal")

    # ---------------------------------------------------------------- centers
    def _update_cache_status(self):
        if not hasattr(self, "cache_var"):
            return
        from assignment_cache import AssignmentCache, age_text
        from team_maker_bp import _team_center
        center = _team_center("Refresh " + self.center_var.get())
        try:
            hours = float(self.settings.get("assignment_cache_hours", 24))
            summary = AssignmentCache(tmc.ASSIGNMENT_CACHE_PATH, self.settings.get("admin_url", "")).summary(center, hours)
            self.cache_var.set((f"{self.center_var.get()}: caching off — fresh reads on creation." if hours <= 0 else
                f"{self.center_var.get()}: {summary['count']} cached team(s) · oldest {age_text(summary['oldest'])} · "
                f"latest {age_text(summary['newest'])}" + (f" · {summary['expired']} expired" if summary['expired'] else "")))
        except Exception as error:
            self.cache_var.set(f"Assignment cache unavailable: {error}")

    def refresh_assignments(self):
        if self.is_running:
            return
        if not tmc.settings_complete(self.settings):
            self.open_login_settings(first_run=True)
            if not tmc.settings_complete(self.settings):
                return
        center = self.center_var.get()
        if not center:
            return
        self._set_busy(True)
        self.status_var.set(f"Refreshing {center} assignments…")
        settings = dict(self.settings, show_browser=not self.headless_var.get())
        def job(worker):
            try:
                return len(worker.get_bot(settings).refresh_assignments(center))
            finally:
                if getattr(self, "coordinator", None) is not None:
                    worker.reset_bot()
        done = lambda result, error: self.line_queue.put(("cache_done", (result or 0, error)))
        coordinator = getattr(self, "coordinator", None)
        try:
            if coordinator is None:
                self.worker.submit(job, done)
            else:
                def coordinated(session, stop, opts):
                    try:
                        session.close()
                        future = self.worker.submit(job, done, stop_event=stop)
                    except Exception as error:
                        done(None, error)
                        raise
                    future.result()
                coordinator.submit_call(coordinated, "Refresh assignments",
                    on_cancel=lambda: done(None, RuntimeError("Assignment refresh cancelled.")))
        except Exception as error:
            done(None, error)

    def reload_centers(self):
        try:
            self.centers = tmc.load_centers()
        except OSError as e:
            self.centers = {}
            messagebox.showerror(TAB_TITLE, f"Could not read {tmc.CENTERS_PATH.name}:\n{e}")
        names = list(self.centers)
        self.center_combo["values"] = names
        if self.center_var.get() not in names:
            self.center_var.set(names[0] if names else "")
        self._show_supervisors()
        self._update_preview()
        self._update_cache_status()
        msg = f"Loaded {len(names)} center(s) from {theme.short_path(tmc.CENTERS_PATH, ROOT_DIR)}."
        self._note(msg if names else msg + " None found -- click Edit Centers to add some.")

    def edit_centers(self):
        path = tmc.CENTERS_PATH
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Center: <name> on its own line, then one supervisor per line.\n", encoding="utf-8")
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # Notepad / default editor
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as e:
            messagebox.showerror(TAB_TITLE, f"Could not open {path}:\n{e}")
            return
        self._note(f"Opened {path.name} -- click Reload Centers after saving.")

    def _show_supervisors(self):
        for w in self.sup_frame.winfo_children():
            w.destroy()
        self.sup_vars = []
        for name in self.centers.get(self.center_var.get(), []):
            var = tk.BooleanVar(value=True)
            choice = tk.Checkbutton(self.sup_frame, text=name, variable=var, anchor="w", background=theme.COLORS["field"],
                                    foreground=theme.COLORS["text"], activebackground=theme.COLORS["selection"],
                                    activeforeground=theme.COLORS["highlight"], selectcolor=theme.COLORS["surface"],
                                    bd=0, highlightthickness=0, command=self._update_sup_count)
            choice.pack(fill="x")
            self._bind_wheel(choice)
            self.sup_vars.append((name, var))
        self.sup_canvas.yview_moveto(0)
        self._update_sup_count()

    def _set_all(self, value):
        for _, var in self.sup_vars:
            var.set(value)
        self._update_sup_count()

    def _update_sup_count(self):
        n = sum(1 for _, v in self.sup_vars if v.get())
        self.sup_label_var.set(f"Supervisors ({n} of {len(self.sup_vars)} selected):")

    # ------------------------------------------------------------ team names
    def _with_center(self, name):
        name = " ".join(name.split())
        center = self.center_var.get().strip()
        if name and center and not name.casefold().endswith(center.casefold()):
            name = f"{name} {center}"
        return name

    def _campaign_for(self, line):
        """Campaign = the typed name without the center on the end."""
        name = " ".join(line.split())
        center = self.center_var.get().strip()
        if center and name.casefold().endswith(" " + center.casefold()):
            name = name[: -len(center)].rstrip()
        return name

    def team_jobs(self):
        """[(team name incl. center, campaign)] for each non-blank, unique line."""
        seen, out = set(), []
        for line in self.names_text.get("1.0", "end").splitlines():
            full = self._with_center(line)
            if full and full.casefold() not in seen:
                seen.add(full.casefold())
                out.append((full, self._campaign_for(line)))
        return out

    def _on_names_modified(self, _event=None):
        self.names_text.edit_modified(False)
        self._update_preview()

    def _update_preview(self):
        jobs = self.team_jobs()
        if not jobs:
            text = ""
        elif len(jobs) == 1:
            text = f"Will create:  {jobs[0][0]}\nCampaign:  {jobs[0][1]}"
        else:
            shown = "\n".join(f"  - {full}" for full, _ in jobs[:3])
            more = f"\n  ...and {len(jobs) - 3} more" if len(jobs) > 3 else ""
            text = f"Will create {len(jobs)} teams (each added to its campaign):\n{shown}{more}"
        self.preview_var.set(text)

    # ---------------------------------------------------------------- actions
    def open_login_settings(self, first_run=False):
        if self.is_running:
            return
        start = tmc.load_settings()
        if first_run and not tmc.settings_complete(start):
            suggestion = tmc.suggested_settings_from_env()
            for key in ("admin_url", "username", "password"):
                if not start.get(key) or (key == "admin_url" and start[key] == tmc.DEFAULT_SETTINGS["admin_url"]):
                    start[key] = suggestion.get(key) or start.get(key, "")
        dlg = LoginSettingsDialog(self.root, start, first_run=first_run)
        self.root.wait_window(dlg)
        if dlg.saved:
            self.settings = tmc.load_settings()
            self.settings["show_browser"] = not self.headless_var.get()
            self._refresh_target()
            self._note("Login saved. Signing in with it...")
            self.worker.submit(lambda w: w.reset_bot())
            self._warm_up()

    def _on_show_browser_changed(self):
        show = not self.headless_var.get()
        self.settings["show_browser"] = show
        try:
            data = tmc.load_settings()
            data["show_browser"] = show
            tmc.save_settings(data)
        except OSError:
            pass
        # Hidden/visible is fixed when the browser launches, so relaunch it.
        self.worker.submit(lambda w: w.reset_bot())
        if self._first_shown and tmc.settings_complete(self.settings):
            self._warm_up()

    def _warm_up(self):
        """Start the browser and sign in in the background so Create starts immediately."""
        if getattr(self, "coordinator", None) is not None:
            self.line_queue.put(("idle_status", "Ready. Sign-in starts with the next team job."))
            return
        if not tmc.settings_complete(self.settings):
            return
        settings, q = dict(self.settings), self.line_queue
        q.put(("idle_status", "Signing in to BrightPattern..."))

        def done(_result, err):
            if err is None:
                q.put(("idle_status", "Ready -- signed in."))
            else:
                q.put(("log", _stamp(f"Sign-in didn't finish: {err}")))
                q.put(("idle_status", "Not signed in yet (will retry when you click Create Team(s))."))

        self.worker.submit(lambda w: w.get_bot(settings).warm_up(), done)

    def on_create(self):
        if self.is_running:
            return
        if not tmc.settings_complete(self.settings):
            self.open_login_settings(first_run=True)
            if not tmc.settings_complete(self.settings):
                return
        jobs = self.team_jobs()
        if not jobs:
            messagebox.showinfo(TAB_TITLE, "Type at least one team name first (one per line).")
            return
        sups = [name for name, var in self.sup_vars if var.get()]
        center = self.center_var.get() or "(none)"
        listing = "\n".join(f"  - {full}" for full, _ in jobs[:10]) + (
            f"\n  ...and {len(jobs) - 10} more" if len(jobs) > 10 else "")
        if not messagebox.askyesno(
                TAB_TITLE,
                f"Create {len(jobs)} team(s)?\n\n{listing}\n\nCenter: {center}\n"
                f"Supervisors per team: {len(sups)}\n"
                "Each team is also added to its campaign (the name without the center)."):
            return

        self._set_busy(True)
        self.status_var.set(f"Creating {len(jobs)} team(s)...")
        self._append_log(f"\n{'=' * 60}\nStarting: {len(jobs)} team(s) for {center} "
                         f"({len(sups)} supervisors, headless={self.headless_var.get()})\n")
        settings = dict(self.settings, show_browser=not self.headless_var.get())
        q, log = self.line_queue, self.worker.log

        def job(w):
            bot = w.get_bot(settings)
            bot._assignment_cache = {}
            results = []
            for i, (name, campaign) in enumerate(jobs, 1):
                if w.active_stop and w.active_stop.is_set():
                    results.extend((n, "Cancelled before starting", []) for n, _ in jobs[i - 1:])
                    break
                q.put(("status", f'Creating {i} of {len(jobs)}: "{name}"...'))
                log(f'--- [{i}/{len(jobs)}] "{name}" (campaign "{campaign}") ---')
                t0 = time.time()
                try:
                    warns = bot.create_team(name, sups, campaign=campaign) or []
                    log(f"Done in {time.time() - t0:.0f} s.")
                    results.append((name, None, warns))
                except Exception as e:
                    from team_maker_bp import TeamAlreadyExists
                    if isinstance(e, TeamAlreadyExists):
                        log(f'Reused existing team: "{e.team_name}". No team created.')
                        results.append((name, None, [f"reused existing team: {e.team_name}"]))
                        continue
                    log(f'ERROR on "{name}": {e}')
                    results.append((name, str(e), []))
            return results

        done = lambda result, err: q.put(("batch_done", (result or [], err)))
        coordinator = getattr(self, "coordinator", None)
        if coordinator is None:
            self.worker.submit(job, done)
        else:
            def coordinated(session, stop, opts):
                session.close()
                def exclusive(w):
                    try:
                        return job(w)
                    finally:
                        w.reset_bot()
                future = self.worker.submit(exclusive, stop_event=stop)
                try:
                    result = future.result()
                except Exception as err:
                    done(None, err)
                    raise
                else:
                    done(result, None)
            coordinator.submit_call(coordinated, "Team Maker", on_cancel=lambda: done(None, RuntimeError("Team job cancelled.")))

    def create_team_for_staffing(self, name, campaign, center, timeout_s=300, assignment_cache=None,
                                 stop_event=None, keep_browser=False):
        """
        Called by the Staffing Moves sub-tab (from ITS worker thread, never the
        Tk thread) when a move's destination team doesn't exist yet. Queues the
        creation on this tab's own browser worker -- the only thread allowed to
        drive Team Maker's browser -- with the center's preset supervisors from
        config/teamMakerCenters.txt and the given campaign, then waits for it.
        Returns an empty list after a confirmed save. A team that
        turns out to exist already counts as success. Raises on failure.
        """
        if not tmc.settings_complete(self.settings):
            raise RuntimeError("Team Maker has no BrightPattern login saved yet -- open the Team Maker tab and "
                               "click Login Settings.")
        centers = tmc.load_centers()
        sups = next((v for k, v in centers.items() if k.casefold() == (center or "").casefold()), None)
        if sups is None:
            raise RuntimeError(f"There's no \"Center: {center}\" in config/teamMakerCenters.txt, so Team Maker "
                               "doesn't know which supervisors to add.")
        settings = dict(self.settings)
        q, done, box = self.line_queue, threading.Event(), {}

        def job(w):
            w.log(f'--- Staffing Moves needs a new team: "{name}" (campaign "{campaign}", '
                  f'{len(sups)} {center} supervisors) ---')
            q.put(("status", f'Creating "{name}" for Staffing Moves...'))
            t0 = time.time()
            try:
                bot = w.get_bot(settings)
                bot._assignment_cache = assignment_cache if assignment_cache is not None else {}
                warns = bot.create_team(name, sups, campaign=campaign) or []
            except Exception as e:
                from team_maker_bp import TeamAlreadyExists, ExistingTeamResult
                if isinstance(e, TeamAlreadyExists):
                    w.log(f'Using existing team "{e.team_name}"; no duplicate created.')
                    return ExistingTeamResult(e.team_name)
                raise
            finally:
                if not keep_browser:
                    w.reset_bot()
            w.log(f"Done in {time.time() - t0:.0f} s.")
            return warns

        def on_done(result, err):
            box.update(result=result, err=err)
            q.put(("idle_status", "Ready."))
            done.set()

        stop = stop_event or threading.Event()
        self.worker.submit(job, on_done, stop_event=stop)
        if not done.wait(timeout_s):
            stop.set()
            # Keep the shared session reserved until the browser worker has closed.
            done.wait()
            if keep_browser:
                self.finish_staffing_batch()
            raise RuntimeError(f'Team Maker did not finish creating "{name}" within {timeout_s // 60} minutes.')
        if box["err"] is not None:
            raise box["err"]
        return box["result"]

    def finish_staffing_batch(self):
        """Close on the owner thread before staffing or Logged-In resumes."""
        self.worker.submit(lambda w: w.reset_bot()).result()

    def _finish_batch(self, results, err):
        self._set_busy(False)
        ok = [n for n, e, _ in results if e is None]
        reused = [n for n, e, w in results if e is None and any(item.startswith("reused existing team") for item in w)]
        created_count = len(ok) - len(reused)
        reuse_text = f", {len(reused)} reused" if reused else ""
        bad = [(n, e) for n, e, _ in results if e is not None]
        warned = [(n, "; ".join(w)) for n, e, w in results if e is None and w and n not in reused]
        if err is not None:
            self._append_log(f"FATAL ERROR: {err}\n")
            bad.append(("(Team Maker)", str(err)))

        # Leave only the names that still need doing, so they can be retried.
        created = {n.casefold() for n in ok}
        keep = [line for line in self.names_text.get("1.0", "end").splitlines()
                if line.strip() and self._with_center(line).casefold() not in created]
        self.names_text.delete("1.0", "end")
        self.names_text.insert("1.0", "\n".join(keep))
        self._update_preview()

        if bad:
            self.status_var.set(f"Done -- {created_count} created{reuse_text}, {len(bad)} FAILED (see Log)")
            lines = "\n\n".join(f"- {n}\n   {e}" for n, e in bad[:6])
            extra = "".join(f"\n\n- {n}\n   {w}" for n, w in warned[:3])
            head = f"{created_count} created{reuse_text}, {len(bad)} failed.\n\n" if ok else ""
            messagebox.showerror(TAB_TITLE, head + lines + extra +
                                 ("\n\nFailed names were left in the box so you can retry them." if ok else ""))
        elif warned:
            self.status_var.set(f"Done -- {created_count} created{reuse_text} ({len(warned)} without a campaign)")
            lines = "\n\n".join(f"- {n}\n   {w}" for n, w in warned[:6])
            messagebox.showwarning(TAB_TITLE, f"{created_count} team(s) created{reuse_text}, but:\n\n{lines}")
        else:
            self.status_var.set(f"Done -- {created_count} team(s) created{reuse_text}")
            if len(ok) > 1:
                messagebox.showinfo(TAB_TITLE, f"{created_count} teams created{reuse_text}.")


def _set_windows_app_user_model_id():
    """Same reason as app.py's helper: keeps the taskbar icon correct. Windows-only, best-effort."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PartnersToolbox.TeamMaker")
    except Exception:
        pass


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
        messagebox.showerror(f"{TAB_TITLE} -- failed to start", f"{error_text}\n\nThis was also saved to {log_path}.")
    except Exception:
        pass


def main():
    _set_windows_app_user_model_id()
    root = tk.Tk()
    try:
        TeamMakerGUI(root)
    except Exception:
        _report_startup_crash()
        try:
            root.destroy()
        except Exception:
            pass
        return
    root.mainloop()


if __name__ == "__main__":
    main()
