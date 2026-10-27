"""Partners Toolbox desktop shell. Navigation changes views, never worker ownership.

All four tools are created once. Staffing and Team Maker retain their shared
job coordinator, and changing workspaces leaves active jobs running.
"""

import sys
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import messagebox, ttk

ROOT_DIR = Path(__file__).resolve().parent.parent

import theme  # noqa: E402
import gui  # noqa: E402
import sheet_slack_gui  # noqa: E402
import sheet_chat_gui  # noqa: E402
import team_maker_gui  # noqa: E402
from workspace_state import LayoutPrefs

APP_TITLE = "Partners Toolbox"
ICON_PATH = ROOT_DIR / "assets" / "app_icon.ico"


class PartnersToolboxApp:
    def __init__(self, root):
        self.root = root
        self.bp_gui = None
        self.sheet_gui = None
        self.chat_gui = None
        self.team_maker_gui = None
        self.layout = LayoutPrefs()
        self._layout_after_id = None
        self._closing = False

        self.root.title(APP_TITLE)
        width = min(1480, max(1100, self.root.winfo_screenwidth() - 80))
        height = min(920, max(640, self.root.winfo_screenheight() - 100))
        window = self.layout.get("window")
        if isinstance(window.get("width"), int) and isinstance(window.get("height"), int):
            width = min(self.root.winfo_screenwidth(), max(1100, window["width"]))
            height = min(self.root.winfo_screenheight(), max(640, window["height"]))
        self._normal_size = (width, height)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(1100, 640)
        theme.apply(self.root)

        theme.set_icon(self.root, ICON_PATH)  # icon is cosmetic only, never fatal

        self._build_shell()

        self.bp_gui = gui.BrightPatternSyncGUI(self.pages["staffing"], embedded=True)
        self.team_maker_gui = team_maker_gui.TeamMakerGUI(self.pages["teams"], embedded=True)
        self.bp_gui.staffing.team_maker = self.team_maker_gui
        self.team_maker_gui.coordinator = self.bp_gui.staffing
        self.sheet_gui = sheet_slack_gui.SheetToSlackGUI(self.pages["slack"], embedded=True)
        self.chat_gui = sheet_chat_gui.SheetToChatGUI(self.pages["chat"], embedded=True)
        self.show_workspace("staffing")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        theme.bind_quit(self.root, self._on_close)
        self.root.bind("<Configure>", self._schedule_window_save, add="+")
        if window.get("maximized") is True and sys.platform in ("win32", "darwin"):
            self.root.after_idle(lambda: self.root.state("zoomed"))

    WORKSPACES = (
        ("staffing", "Staffing & agents", "Staffing & agents", "Manage staffing moves, logged-in agents, and center cleanup."),
        ("teams", "Team Maker", "Team Maker", "Create project teams with the right center and supervisors."),
        ("slack", "Slack updates", "Slack updates", "Schedule production reports and share updates in Slack."),
        ("chat", "Chat updates", "Chat updates", "Send center reports and manage scheduled Google Chat updates."),
    )

    def _build_shell(self):
        body = ttk.Frame(self.root, style="Workspace.TFrame")
        body.pack(fill="both", expand=True)
        sidebar = ttk.Frame(body, style="Sidebar.TFrame", width=204, padding=(12, 20, 12, 16))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(8, 0, 8, 16))
        brand.pack(fill="x")
        ttk.Label(brand, text="PT", style="BrandMark.TLabel").pack(anchor="w", pady=(0, 12))
        ttk.Label(brand, text="Partners", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(brand, text="Toolbox", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(brand, text="Operations workspace", style="SidebarHint.TLabel").pack(anchor="w", pady=(8, 0))

        footer = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(8, 12, 0, 0))
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, text="KEYBOARD SHORTCUTS", style="SidebarHeading.TLabel").pack(anchor="w")
        ttk.Label(footer, text=f"{theme.SHORTCUT_LABEL} 1 / 2 / 3 / 4", style="SidebarHint.TLabel").pack(anchor="w", pady=(6, 0))

        self.nav_buttons = {}
        for index, (key, label, _title, _subtitle) in enumerate(self.WORKSPACES):
            if index in (0, 2):
                ttk.Label(sidebar, text="OPERATIONS" if index == 0 else "REPORTING",
                          style="SidebarHeading.TLabel").pack(anchor="w", padx=16, pady=(16, 8))
            button = ttk.Button(sidebar, text=label, style="Nav.TButton", command=lambda name=key: self.show_workspace(name))
            button.pack(fill="x", pady=3)
            self.nav_buttons[key] = button
            self.root.bind(f"<{theme.SHORTCUT_KEY}-Key-{index + 1}>", lambda event, name=key: self._navigate_shortcut(name), add="+")

        content = ttk.Frame(body, style="Workspace.TFrame", padding=(20, 16, 20, 16))
        content.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(content, style="Workspace.TFrame")
        header.pack(fill="x", pady=(0, 12))
        self.page_title = tk.StringVar(value="")
        self.page_subtitle = tk.StringVar(value="")
        ttk.Label(header, text="PARTNERS TOOLBOX", style="Eyebrow.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Label(header, textvariable=self.page_title, style="Title.TLabel").pack(anchor="w")
        subtitle = ttk.Label(header, textvariable=self.page_subtitle, style="Subtitle.TLabel", wraplength=760)
        subtitle.pack(fill="x", pady=(4, 0))
        theme.wrap_label(subtitle)

        stack = ttk.Frame(content, style="Panel.TFrame", padding=1)
        stack.pack(fill="both", expand=True)
        stack.rowconfigure(0, weight=1)
        stack.columnconfigure(0, weight=1)
        self.pages = {}
        for key, _label, _title, _subtitle in self.WORKSPACES:
            page = ttk.Frame(stack, padding=(4, 4, 4, 0))
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()
            self.pages[key] = page
        self.active_workspace = None

    def show_workspace(self, key):
        """Only change presentation; never stop, restart, or duplicate a tool."""
        for name, _label, title, subtitle in self.WORKSPACES:
            if name == key:
                self.page_title.set(title)
                self.page_subtitle.set(subtitle)
                for item, page in self.pages.items():
                    if item != key:
                        page.grid_remove()
                self.pages[key].grid()
                self.pages[key].tkraise()
                for item, button in self.nav_buttons.items():
                    button.configure(style="NavActive.TButton" if item == key else "Nav.TButton")
                self.active_workspace = key
                return

    def _navigate_shortcut(self, key):
        self.show_workspace(key)
        self.nav_buttons[key].focus_set()
        return "break"

    def _on_close(self):
        self._closing = True
        if self._layout_after_id is not None:
            self.root.after_cancel(self._layout_after_id)
            self._layout_after_id = None
        self._save_window()
        # Shut down all four tabs before tearing down the one shared
        # root. Each shutdown() is independent and safe to call even if
        # another tab never fully finished constructing.
        for gui_obj in (self.bp_gui, self.sheet_gui, self.chat_gui, self.team_maker_gui):
            if gui_obj is not None:
                try:
                    gui_obj.shutdown()
                except Exception:
                    pass
        self.root.destroy()

    def _schedule_window_save(self, event):
        if event.widget is not self.root or self._closing:
            return
        if self._layout_after_id is not None:
            self.root.after_cancel(self._layout_after_id)
        self._layout_after_id = self.root.after(250, self._save_window)

    def _save_window(self):
        self._layout_after_id = None
        try:
            state = self.root.state()
            width, height = self.root.winfo_width(), self.root.winfo_height()
            if state == "normal" and width >= 1100 and height >= 640:
                self._normal_size = (width, height)
            self.layout.save("window", {"width": self._normal_size[0], "height": self._normal_size[1],
                                        "maximized": state == "zoomed"})
        except Exception:
            pass  # presentation preferences never stop a move or shutdown


def _show_fatal_error_dialog(exc_type, exc_value, exc_tb):
    """Used as Tk's callback-exception handler so errors are visible even
    when launched via pythonw (no console window to print to)."""
    err_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        messagebox.showerror(f"{APP_TITLE} - unexpected error", err_text)
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
            f"{APP_TITLE} - failed to start",
            f"{error_text}\n\nThis was also saved to {log_path}.",
        )
    except Exception:
        pass


def _set_windows_app_user_model_id():
    """
    Without this, the Desktop shortcut's icon (set directly on the .lnk
    file by create_desktop_shortcut.vbs) and the running app's taskbar
    icon can look different even though both are meant to be
    assets/app_icon.ico. Windows groups taskbar buttons/icons by a
    process's "Application User Model ID" (AppUserModelID); a plain
    python.exe/pythonw.exe process that never sets one explicitly gets
    grouped under Python's own launcher identity, so the taskbar shows
    Python's generic icon instead of the one iconbitmap() set on the
    window itself. Giving this process its own explicit ID (any unique
    string -- it doesn't need to mean anything to Windows beyond being
    distinct) tells Windows to treat it as its own application and use
    ITS window icon for the taskbar too, matching the Desktop shortcut.
    Must be called before the Tk window is created. Windows-only and
    best-effort: a harmless no-op everywhere else (including this
    project's own Linux test environment).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PartnersToolbox.App")
    except Exception:
        pass


def main():
    _set_windows_app_user_model_id()
    root = tk.Tk()
    try:
        PartnersToolboxApp(root)
    except Exception:
        # Mirrors gui.py's own _report_startup_crash -- launched via a
        # silent pythonw shortcut, a startup crash would otherwise be
        # completely invisible (no window, no console, nothing).
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
