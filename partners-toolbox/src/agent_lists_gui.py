"""Named lists per center and a reviewed, exact-name project assignment picker."""
import re
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import staffing_moves as sm
import theme


class AgentListDialog(tk.Toplevel):
    def __init__(self, panel, group="Top 15"):
        super().__init__(panel.parent)
        self.panel = panel
        self.groups = sm.load_agent_groups()
        self.groups.setdefault("Top 15", {center: [] for center in sm.CENTERS})
        self.groups.setdefault(group, {center: [] for center in sm.CENTERS})
        self.title("Agent Lists")
        self.transient(panel.parent.winfo_toplevel())
        self.geometry("800x650")
        self.minsize(700, 540)
        self._group = tk.StringVar(value=group)
        self._center = tk.StringVar(value="InfoConnect" if group == "Spanish" else "611")
        self._state = {"group": group, "center": self._center.get()}
        self._count = tk.StringVar()

        frame = ttk.Frame(self, padding=theme.PAD)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Agent Lists", style="Section.TLabel").pack(anchor="w")
        theme.hint(frame, "Keep separate Top 15, Top 25, Spanish, or custom lists for each center. "
                   "Use the names exactly as they appear in Contact Center.", pady=(4, theme.GAP))
        pickers = ttk.Frame(frame)
        pickers.pack(fill="x")
        pickers.columnconfigure(1, weight=1)
        ttk.Label(pickers, text="List:").grid(row=0, column=0, sticky="w")
        self._group_combo = ttk.Combobox(pickers, textvariable=self._group, state="readonly", width=18)
        self._group_combo.grid(row=0, column=1, sticky="ew", padx=theme.GAP)
        ttk.Button(pickers, text="New list...", command=self._new_group).grid(row=0, column=2)
        ttk.Label(pickers, text="Center:").grid(row=0, column=3, padx=(theme.PAD, 0))
        center_combo = ttk.Combobox(pickers, textvariable=self._center, values=sm.CENTERS,
                                    state="readonly", width=14)
        center_combo.grid(row=0, column=4, padx=(theme.GAP, 0))
        self._group_combo.bind("<<ComboboxSelected>>", self._switch)
        center_combo.bind("<<ComboboxSelected>>", self._switch)
        self._refresh_groups()

        # Reserve actions before the expanding editor.
        actions = ttk.Frame(frame)
        actions.pack(side="bottom", fill="x", pady=(theme.GAP, 0))
        ttk.Button(actions, text="Choose agents & project...", style="Primary.TButton",
                   command=self._assign).pack(side="left")
        ttk.Button(actions, text="Save lists", command=self._save).pack(side="right")
        ttk.Button(actions, text="Cancel", command=self.destroy).pack(side="right", padx=theme.GAP)
        theme.hint(frame, "Queueing a selection does not move anyone. Review it in Staffing queue, "
                   "then use Run Ticked Moves.", side="bottom", pady=(theme.GAP, 0))
        ttk.Label(frame, textvariable=self._count, style="Hint.TLabel").pack(side="bottom", anchor="w", pady=(4, 0))

        tools = ttk.Frame(frame)
        tools.pack(fill="x", pady=(theme.GAP, 0))
        ttk.Button(tools, text="Import text / CSV...", command=self._import_file).pack(side="left")
        ttk.Button(tools, text="Format pasted names", command=self._format_text).pack(side="left", padx=theme.GAP)
        theme.hint(frame, "Paste one name per line, or code + name columns (for example, DD followed by Joan Cariñosa). "
                   "Codes are formatted as (DD); list order is preserved.", pady=(theme.GAP, theme.GAP))
        editor = ttk.Frame(frame)
        editor.pack(fill="both", expand=True)
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(0, weight=1)
        self._text = theme.text_input(editor, wrap="none", width=50, height=12)
        vertical = ttk.Scrollbar(editor, orient="vertical", command=self._text.yview)
        horizontal = ttk.Scrollbar(editor, orient="horizontal", command=self._text.xview)
        self._text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self._text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self._text.bind("<<Modified>>", self._modified)
        self._show()

    def _refresh_groups(self):
        self._group_combo.configure(values=list(self.groups))

    def _names(self):
        return sm.import_agent_names(self._text.get("1.0", "end"))

    def _modified(self, _event):
        if self._text.edit_modified():
            self._text.edit_modified(False)
            self._update_count()

    def _update_count(self):
        try:
            count = len(self._names())
            self._count.set(f"{count} name(s) in {self._state['group']} for {self._state['center']}")
        except ValueError as error:
            self._count.set(str(error))

    def _keep(self):
        names = self._names()
        self.groups[self._state['group']][self._state['center']] = names
        return names

    def _show(self):
        self._text.delete("1.0", "end")
        self._text.insert("1.0", "\n".join(self.groups[self._group.get()].get(self._center.get(), [])))
        self._state = {"group": self._group.get(), "center": self._center.get()}
        self._update_count()

    def _switch(self, _event=None):
        try:
            self._keep()
        except ValueError as error:
            self._group.set(self._state["group"])
            self._center.set(self._state["center"])
            messagebox.showerror("Agent Lists", str(error), parent=self)
            return
        self._show()

    def _new_group(self):
        name = simpledialog.askstring("New agent list", "List name (for example, Top 25 or Training):", parent=self)
        if name is None:
            return
        name = " ".join(name.split())
        match = re.fullmatch(r"top\s*(\d+)", name, flags=re.I)
        if match:
            if int(match[1]) < 1:
                messagebox.showerror("Agent Lists", "A top list needs a positive number of agents.", parent=self)
                return
            name = f"Top {int(match[1])}"
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{0,39}", name):
            messagebox.showerror("Agent Lists", "Use a list name up to 40 characters, with letters, numbers, and spaces.", parent=self)
            return
        try:
            self._keep()
        except ValueError as error:
            messagebox.showerror("Agent Lists", str(error), parent=self)
            return
        existing = sm.group_for_word(name, self.groups)
        self.groups.setdefault(existing or name, {center: [] for center in sm.CENTERS})
        self._group.set(existing or name)
        self._refresh_groups()
        self._show()

    def _format_text(self):
        try:
            names = self._names()
        except ValueError as error:
            messagebox.showerror("Agent Lists", str(error), parent=self)
            return
        self._text.delete("1.0", "end")
        self._text.insert("1.0", "\n".join(names))
        self._update_count()

    def _import_file(self):
        path = filedialog.askopenfilename(title="Import agent names", parent=self,
                                          filetypes=[("Agent lists", "*.txt *.csv *.tsv"), ("All files", "*.*")])
        if not path:
            return
        try:
            names = sm.import_agent_names(Path(path).read_text(encoding="utf-8-sig"))
            if not names:
                raise ValueError("This file has no agent names.")
        except (OSError, UnicodeError, ValueError) as error:
            messagebox.showerror("Agent Lists", f"Could not import names: {error}", parent=self)
            return
        # Import into the editable preview; nothing is saved or queued yet.
        self._text.delete("1.0", "end")
        self._text.insert("1.0", "\n".join(names))
        self._update_count()

    def _save(self):
        try:
            self._keep()
            self.panel.apply_agent_lists(self.groups)
        except (OSError, ValueError) as error:
            messagebox.showerror("Agent Lists", str(error), parent=self)
            return
        self.panel.log(f"Agent Lists saved: {', '.join(self.groups)}.")
        self.destroy()

    def _assign(self):
        try:
            names = self._keep()
        except ValueError as error:
            messagebox.showerror("Agent Lists", str(error), parent=self)
            return
        if not names:
            messagebox.showinfo("Agent Lists", "Add names to this list first.", parent=self)
            return
        return AgentAssignmentDialog(self, self._state["group"], self._state["center"], names)


class AgentAssignmentDialog(tk.Toplevel):
    def __init__(self, editor, group, center, names):
        super().__init__(editor)
        self.editor, self.panel = editor, editor.panel
        self.group, self.center, self.names = group, center, list(names)
        self.title("Assign listed agents")
        self.transient(editor)
        self.geometry("820x600")
        self.minsize(700, 500)
        frame = ttk.Frame(self, padding=theme.PAD)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"{center} · {group}", style="Section.TLabel").pack(anchor="w")
        theme.hint(frame, "Select the exact people to move. All names are selected initially; "
                   f"{'Command' if theme.MAC else 'Ctrl'}-click or Shift-click to change the selection.",
                   pady=(4, theme.GAP))
        self._destination = tk.StringVar()
        self._selection_count = tk.StringVar()

        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(theme.GAP, 0))
        self._queue_button = ttk.Button(buttons, text="Queue selected agents", style="Primary.TButton", command=self._queue)
        self._queue_button.pack(side="right")
        if getattr(self.panel.gui, "env_error", None):
            self._queue_button.state(["disabled"])
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=theme.GAP)
        theme.hint(frame, "Only the selected names are queued. Missing names are reported, and moved names "
                   "are verified in the project. Run Ticked Moves approves the move.", side="bottom", pady=(theme.GAP, 0))

        project_row = ttk.Frame(frame)
        project_row.pack(fill="x", pady=(0, theme.GAP))
        project_row.columnconfigure(1, weight=1)
        ttk.Label(project_row, text="Team/project:").grid(row=0, column=0, sticky="w")
        projects = self.panel.teams
        if not projects:
            projects, _ = sm.load_team_cache(max_age_s=365 * 24 * 3600, base_url=self.panel.gui.base_url)
        project_names = sorted({project.name for project in projects or [] if project.center == center})
        self._project_combo = ttk.Combobox(project_row, textvariable=self._destination, values=project_names, width=44)
        self._project_combo.grid(row=0, column=1, sticky="ew", padx=(theme.GAP, 0))
        theme.hint(frame, "Choose a project or U team, or paste its name/code. The destination is checked before the move.",
                   pady=(0, theme.GAP))

        selection_tools = ttk.Frame(frame)
        selection_tools.pack(fill="x", pady=(0, theme.GAP))
        ttk.Button(selection_tools, text="Select all", command=self._select_all).pack(side="left")
        ttk.Button(selection_tools, text="Clear selection", command=lambda: self._tree.selection_remove(self._tree.selection())).pack(side="left", padx=theme.GAP)
        ttk.Label(selection_tools, textvariable=self._selection_count, style="Hint.TLabel").pack(side="right")
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self._tree = ttk.Treeview(table, columns=("name",), show="tree headings", selectmode="extended", height=8)
        self._tree.heading("#0", text="No.")
        self._tree.column("#0", width=55, stretch=False)
        self._tree.heading("name", text="Exact agent name")
        self._tree.column("name", width=500, minwidth=250)
        bar = ttk.Scrollbar(table, orient="vertical", command=self._tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=bar.set, xscrollcommand=horizontal.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        for index, name in enumerate(self.names):
            self._tree.insert("", "end", iid=str(index), text=str(index + 1), values=(name,))
        self._tree.bind("<<TreeviewSelect>>", self._selected)
        self._select_all()
        self.grab_set()

    def _select_all(self):
        self._tree.selection_set(self._tree.get_children())
        self._selected()

    def _selected(self, _event=None):
        self._selection_count.set(f"{len(self._tree.selection())} of {len(self.names)} selected")

    def _queue(self):
        selected = set(self._tree.selection())
        names = [name for index, name in enumerate(self.names) if str(index) in selected]
        destination = self._destination.get().strip()
        if not names or not destination:
            messagebox.showinfo("Agent Lists", "Select at least one agent and choose a project.", parent=self)
            return
        destination_center = sm.team_center(destination)
        if destination_center and destination_center != self.center:
            messagebox.showerror("Agent Lists", f"Choose a {self.center} project; this project belongs to {destination_center}.", parent=self)
            return
        try:
            self.editor._keep()
            self.panel.apply_agent_lists(self.editor.groups)
            self.panel.queue_named_agents(self.group, self.center, names, destination)
        except (OSError, ValueError) as error:
            messagebox.showerror("Agent Lists", str(error), parent=self)
            return
        self.destroy()
