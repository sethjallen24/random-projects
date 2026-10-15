"""Shared, accessible desktop styles and presentation helpers."""
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

PAD = 16
GAP = 8
LOG_FONT = ("Consolas", 9) if sys.platform == "win32" else ("TkFixedFont", 9)
COLORS = {
    "surface": "#edf2f3", "field": "#ffffff", "text": "#182f36",
    "muted": "#526b73", "border": "#d6e2e5", "accent": "#087f83",
    "accent_hover": "#05666a", "selection": "#e1f3f2", "danger": "#b42339",
    "success": "#18734c", "warning": "#94560c", "disabled": "#6c7f85",
    "sidebar": "#142b32", "sidebar_hover": "#203f47", "sidebar_selected": "#27535b",
    "sidebar_text": "#f4fafb", "sidebar_muted": "#b7cdd2", "heading": "#f4f8f9",
}
MAX_LOG_LINES = 5000


def apply(root):
    """Apply once per Tk root, including when a new window replaces an old one."""
    root = root.winfo_toplevel()
    if getattr(root, "_partners_theme_applied", False):
        return
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    base = tkfont.nametofont("TkDefaultFont", root=root)
    base.configure(size=10)
    if sys.platform == "win32":
        base.configure(family="Segoe UI")
    family, size = base.cget("family"), base.cget("size")
    for name in ("TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name, root=root).configure(family=family, size=size)
        except tk.TclError:
            pass
    c = COLORS
    root.configure(background=c["surface"])
    style.configure(".", background=c["field"], foreground=c["text"], font=(family, size))
    style.configure("TFrame", background=c["field"])
    style.configure("TLabel", background=c["field"], foreground=c["text"])
    style.configure("Workspace.TFrame", background=c["surface"])
    style.configure("Panel.TFrame", background=c["field"], bordercolor=c["border"], relief="solid", borderwidth=1)
    style.configure("TButton", padding=(12, 7), background=c["field"], bordercolor=c["border"],
                    foreground=c["text"], relief="flat", focusthickness=2, focuscolor=c["accent"])
    style.map("TButton", background=[("disabled", c["surface"]), ("pressed", c["border"]), ("active", c["selection"])],
              foreground=[("disabled", c["disabled"])])
    style.configure("Primary.TButton", background=c["accent"], foreground="white",
                    font=(family, size, "bold"), bordercolor=c["accent"])
    style.map("Primary.TButton", background=[("disabled", c["border"]), ("pressed", c["accent_hover"]),
                                              ("active", c["accent_hover"])],
              foreground=[("disabled", c["muted"]), ("!disabled", "white")])
    style.configure("Danger.TButton", foreground=c["danger"], bordercolor="#efd5db")
    style.map("Danger.TButton", foreground=[("disabled", c["disabled"]), ("!disabled", c["danger"])],
              background=[("disabled", c["surface"]), ("active", "#fff0f2")])
    style.configure("TEntry", padding=7, fieldbackground=c["field"], bordercolor=c["border"], relief="flat")
    style.map("TEntry", bordercolor=[("focus", c["accent"])])
    style.configure("TCombobox", padding=6, fieldbackground=c["field"], bordercolor=c["border"], arrowsize=14)
    style.map("TCombobox", fieldbackground=[("readonly", c["field"]), ("disabled", c["surface"])],
              foreground=[("disabled", c["disabled"])])
    style.configure("TCheckbutton", padding=(0, 4), background=c["field"])
    style.map("TCheckbutton", background=[("active", c["field"])], foreground=[("disabled", c["disabled"])])
    style.configure("TNotebook", borderwidth=0, background=c["field"], tabmargins=(0, 0, 0, 6))
    style.configure("TNotebook.Tab", padding=(16, 9), background=c["field"], foreground=c["muted"],
                    font=(family, size, "bold"), borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", c["selection"]), ("active", c["heading"])],
              foreground=[("selected", c["accent"])])
    style.configure("TLabelframe", padding=(PAD, GAP, PAD, PAD), background=c["field"],
                    bordercolor=c["border"], relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", font=(family, size, "bold"), foreground=c["text"])
    style.configure("Treeview", rowheight=30, background=c["field"], fieldbackground=c["field"],
                    foreground=c["text"], bordercolor=c["border"], borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", c["selection"])], foreground=[("selected", c["text"])])
    style.configure("Treeview.Heading", padding=(12, 9), background=c["heading"],
                    foreground=c["muted"], font=(family, size, "bold"), relief="flat")
    style.configure("Vertical.TScrollbar", background=c["border"], troughcolor=c["heading"],
                    borderwidth=0, arrowsize=12, gripcount=0)
    style.configure("Horizontal.TScrollbar", background=c["border"], troughcolor=c["heading"],
                    borderwidth=0, arrowsize=12, gripcount=0)
    style.configure("Hint.TLabel", foreground=c["muted"])
    style.configure("Warning.TLabel", foreground=c["danger"])
    style.configure("Target.TLabel", foreground=c["muted"])
    style.configure("Title.TLabel", font=(family, 21, "bold"), background=c["surface"])
    style.configure("Subtitle.TLabel", foreground=c["muted"], background=c["surface"])
    style.configure("Eyebrow.TLabel", font=(family, 8, "bold"), foreground=c["muted"], background=c["surface"])
    style.configure("Section.TLabel", font=(family, 11, "bold"), foreground=c["text"])
    style.configure("Compact.TButton", padding=(9, 4), foreground=c["muted"])
    style.configure("Status.TLabel", anchor="w", padding=(PAD, 9), background=c["heading"], foreground=c["muted"])
    style.configure("Sidebar.TFrame", background=c["sidebar"])
    style.configure("Brand.TLabel", background=c["sidebar"], foreground=c["sidebar_text"], font=(family, 15, "bold"))
    style.configure("SidebarHint.TLabel", background=c["sidebar"], foreground=c["sidebar_muted"], font=(family, 9))
    style.configure("SidebarHeading.TLabel", background=c["sidebar"], foreground=c["sidebar_muted"], font=(family, 8, "bold"))
    style.configure("BrandMark.TLabel", background=c["accent"], foreground="white", font=(family, 12, "bold"),
                    padding=(10, 8), anchor="center")
    for name, selected in (("Nav.TButton", False), ("NavActive.TButton", True)):
        style.configure(name, background=c["sidebar_selected"] if selected else c["sidebar"],
                        foreground=c["sidebar_text"] if selected else c["sidebar_muted"],
                        bordercolor=c["sidebar_selected"] if selected else c["sidebar"],
                        padding=(16, 12), anchor="w", relief="flat", font=(family, 10, "bold"),
                        focusthickness=2, focuscolor=c["accent"])
        style.map(name, background=[("active", c["sidebar_hover"]), ("pressed", c["sidebar_selected"])],
                  foreground=[("active", "white")])
    root._partners_theme_applied = True


class ScrollableControls(ttk.Frame):
    """Keep controls reachable when a user gives more height to the queue."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, width=1, height=1, background=COLORS["field"],
                                borderwidth=0, highlightthickness=0)
        self.content = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas.bind("<Configure>", self._resize)
        self.content.bind("<Configure>", self._update_region)

    def _resize(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _update_region(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def enable_mousewheel(self):
        # Local bindings leave tables, editors, comboboxes, and other tabs alone.
        def bind(widget):
            if widget.winfo_class() not in ("TCombobox", "TEntry", "Entry", "Text", "Treeview"):
                for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    widget.bind(event, self._wheel, add="+")
            for child in widget.winfo_children():
                bind(child)
        bind(self.canvas)

    def _wheel(self, event):
        if self.canvas.yview() == (0.0, 1.0):
            return
        direction = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
        self.canvas.yview_scroll(direction * 2, "units")
        return "break"


class QueueSplit(tk.PanedWindow):
    """An adjustable divider that reserves visible space for a staffing queue.

    Keep the user's split proportion across resizes and tab switches. Clamp it
    only when necessary to leave a usable table and reachable controls.
    """

    CONTROL_MIN = 80
    QUEUE_MIN = 180
    DIVIDER = 12
    DEFAULT_FRACTION = 0.50

    def __init__(self, parent):
        super().__init__(parent, orient="vertical", background=COLORS["border"],
                         borderwidth=0, relief="flat", sashwidth=self.DIVIDER,
                         sashpad=0, showhandle=True, handlesize=8, opaqueresize=True)
        self._fraction = self.DEFAULT_FRACTION
        self.on_change = None
        self._layout_id = None
        self.bind("<Configure>", self._schedule_layout)
        self.bind("<Map>", self._schedule_layout)
        self.bind("<ButtonRelease-1>", self._remember_split, add="+")
        self.bind("<Destroy>", self._destroy_layout, add="+")

    def add_sections(self, controls, queue, table):
        self._queue = queue
        self._table = table
        table.bind("<Configure>", self._schedule_layout, add="+")
        self.add(controls, minsize=self.CONTROL_MIN, stretch="always")
        self.add(queue, minsize=self.QUEUE_MIN, stretch="always")
        self._schedule_layout()

    def reset_size(self):
        self._fraction = self.DEFAULT_FRACTION
        self._schedule_layout()
        if self.on_change:
            self.on_change()

    def set_fraction(self, value):
        if isinstance(value, (int, float)) and 0.05 <= value <= 0.95:
            self._fraction = float(value)
            self._schedule_layout()

    def _schedule_layout(self, _event=None):
        if self._layout_id is None:
            self._layout_id = self.after_idle(self._layout)

    def _layout(self):
        self._layout_id = None
        usable = self.winfo_height() - self.DIVIDER
        if len(self.panes()) != 2:
            return
        # Include headings, help, scrollbars, and the summary at the current
        # font/DPI, plus at least two data rows. Larger text must not hide rows.
        row_height = int(ttk.Style(self).lookup("Treeview", "rowheight") or 30)
        table_rows = int(self._table.cget("height"))
        queue_min = max(self.QUEUE_MIN, self._queue.winfo_reqheight() - (table_rows - 2) * row_height)
        if int(self.panecget(self._queue, "minsize")) != queue_min:
            self.paneconfigure(self._queue, minsize=queue_min)
        if usable < self.CONTROL_MIN + queue_min:
            return
        position = max(self.CONTROL_MIN, min(round(usable * self._fraction), usable - queue_min))
        self.sash_place(0, 0, position)

    def _remember_split(self, _event):
        usable = self.winfo_height() - self.DIVIDER
        if len(self.panes()) == 2 and usable > 0:
            self._fraction = self.sash_coord(0)[1] / usable
            self._schedule_layout()
            if self.on_change:
                self.on_change()

    def _destroy_layout(self, event):
        if event.widget is self and self._layout_id is not None:
            self.after_cancel(self._layout_id)
            self._layout_id = None


def section(parent, title, **pack_kwargs):
    frame = ttk.LabelFrame(parent, text=title)
    kwargs = {"fill": "x", "padx": PAD, "pady": (GAP, 0)}
    kwargs.update(pack_kwargs)
    frame.pack(**kwargs)
    return frame


def status_bar(parent, textvariable):
    bar = ttk.Label(parent, textvariable=textvariable, style="Status.TLabel")
    bar.pack(side="bottom", fill="x")
    return bar


def wrap_label(label):
    """Follow the allocated width, so long help and account text cannot widen panes."""
    label.bind("<Configure>", lambda event: label.configure(wraplength=max(1, event.width - 4)), add="+")
    return label


def hint(parent, text, **pack_kwargs):
    label = ttk.Label(parent, text=text, style="Hint.TLabel", wraplength=420, justify="left")
    label.pack(**({"anchor": "w", "fill": "x"} | pack_kwargs))
    return wrap_label(label)


def warning(parent, text, **pack_kwargs):
    label = ttk.Label(parent, text=text, style="Warning.TLabel", wraplength=420, justify="left")
    label.pack(**({"anchor": "w", "fill": "x", "pady": (2, 2)} | pack_kwargs))
    return wrap_label(label)


def log_pane(parent):
    from tkinter import scrolledtext
    box = scrolledtext.ScrolledText(parent, state="disabled", wrap="word", font=LOG_FONT,
                                    relief="flat", borderwidth=0, padx=12, pady=10,
                                    background=COLORS["field"], foreground=COLORS["text"],
                                    highlightthickness=1, highlightbackground=COLORS["border"],
                                    insertbackground=COLORS["text"])
    box.pack(fill="both", expand=True)
    return box


def text_input(parent, **kwargs):
    """A normal Tk text editor, styled consistently with entries."""
    options = dict(wrap="word", undo=True, font=("Segoe UI", 10) if sys.platform == "win32" else ("TkDefaultFont", 10),
                   relief="flat", borderwidth=0, padx=12, pady=10, highlightthickness=1,
                   background=COLORS["field"], foreground=COLORS["text"],
                   highlightbackground=COLORS["border"], highlightcolor=COLORS["accent"],
                   selectbackground=COLORS["selection"], selectforeground=COLORS["text"],
                   insertbackground=COLORS["text"])
    options.update(kwargs)
    return tk.Text(parent, **options)


def append_log(box, text):
    """Bound the on-screen history; callers retain their existing full disk logs."""
    at_bottom = box.yview()[1] >= 0.99
    box.configure(state="normal")
    try:
        box.insert("end", text)
        # Tk's final empty line is not part of the displayed history.
        lines = int(box.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            box.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        if at_bottom:
            box.see("end")
    finally:
        box.configure(state="disabled")


def tree_tags(tree, **tags):
    for tag, color in tags.items():
        tree.tag_configure(tag, foreground=COLORS[color])


def short_path(path, root_dir):
    try:
        return str(path.relative_to(root_dir))
    except (ValueError, AttributeError):
        return str(path)
