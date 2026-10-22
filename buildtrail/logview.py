"""Read-only side-by-side build log review, with bounded search and diagnostics."""
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sqlite3

from .logdiff import load_log, align_logs, search_logs
from .organization import run_name
from ._apptrail.theme import PALETTES


class LogDiffWindow(tk.Toplevel):
    def __init__(self, parent, store, before, after):
        super().__init__(parent)
        self.store = store
        self.records = (before, after)
        self.title('BuildTrail · Side-by-side build logs')
        self.geometry('1320x860')
        self.minsize(1000, 680)
        self.theme = getattr(parent, '_apptrail_theme', None)
        self.model = {'rows': [], 'changes': [], 'diagnostics': []}
        self.matches = []
        self.match_index = self.issue_index = self.change_index = -1
        self.ready = False
        self.failure = None
        self.closed = False
        self.syncing = False
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.search_timer = None
        self.theme_trace = None
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill='both', expand=True)
        header = ttk.Frame(frame)
        header.pack(fill='x')
        self.export_button = ttk.Button(header, text='Export HTML…', command=self.export_html, state='disabled')
        self.export_button.pack(side='right')
        ttk.Label(header, text='Build log comparison', font=('Segoe UI', 18, 'bold')).pack(side='left')
        self.summary = tk.StringVar(value='Verifying saved logs and aligning lines…')
        ttk.Label(frame, textvariable=self.summary, wraplength=1260).pack(anchor='w', pady=(4, 8))
        search = ttk.Frame(frame)
        search.pack(fill='x', pady=4)
        ttk.Label(search, text='Find in both logs').pack(side='left')
        self.query = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.query, width=38)
        self.search_entry.pack(side='left', padx=8)
        self.match_case = tk.BooleanVar(value=False)
        ttk.Checkbutton(search, text='Match case', variable=self.match_case, command=self.schedule_search).pack(side='left')
        ttk.Button(search, text='Previous match', command=lambda: self.jump_match(-1)).pack(side='left', padx=4)
        ttk.Button(search, text='Next match', command=lambda: self.jump_match(1)).pack(side='left')
        ttk.Button(search, text='Clear', command=lambda: self.query.set('')).pack(side='left', padx=4)
        self.search_status = tk.StringVar(value='Ctrl+F · F3 / Shift+F3 · Literal search')
        ttk.Label(frame, textvariable=self.search_status).pack(anchor='w', pady=4)
        issues = ttk.Frame(frame)
        issues.pack(fill='x', pady=4)
        self.issue_kind = tk.StringVar(value='Errors & warnings')
        self.issue_side = tk.StringVar(value='Both logs')
        for variable, values, width in ((self.issue_kind, ('Errors & warnings', 'Errors', 'Warnings'), 18),
                                         (self.issue_side, ('Both logs', 'Before', 'After'), 12)):
            box = ttk.Combobox(issues, textvariable=variable, values=values, state='readonly', width=width)
            box.pack(side='left', padx=(0, 6))
            box.bind('<<ComboboxSelected>>', self.filter_issues)
        ttk.Button(issues, text='Previous issue', command=lambda: self.jump_issue(-1)).pack(side='left')
        ttk.Button(issues, text='Next issue', command=lambda: self.jump_issue(1)).pack(side='left', padx=6)
        ttk.Button(issues, text='Previous change', command=lambda: self.jump_change(-1)).pack(side='right')
        ttk.Button(issues, text='Next change', command=lambda: self.jump_change(1)).pack(side='right', padx=6)
        self.issue_status = tk.StringVar(value='Errors/warnings are heuristic matches, not a compiler verdict.')
        ttk.Label(frame, textvariable=self.issue_status).pack(anchor='w', pady=4)
        panes = ttk.Frame(frame)
        panes.pack(fill='both', expand=True, pady=(6, 0))
        panes.columnconfigure(0, weight=1, uniform='logs')
        panes.columnconfigure(1, weight=1, uniform='logs')
        panes.rowconfigure(1, weight=1)
        self.headings = {}
        self.texts = {}
        for column, (side, record) in enumerate((('before', before), ('after', after))):
            self.headings[side] = ttk.Label(panes, text=self.heading(side, record), wraplength=580)
            self.headings[side].grid(row=0, column=column, sticky='w', padx=4, pady=4)
            text = tk.Text(panes, wrap='none', font=('Consolas', 10), state='disabled', undo=False,
                           exportselection=False, takefocus=True)
            self.texts[side] = text
            text.grid(row=1, column=column, sticky='nsew', padx=4)
            text.configure(yscrollcommand=lambda first, last, side=side: self.sync_scroll(side, first, last))
            horizontal = ttk.Scrollbar(panes, orient='horizontal', command=text.xview)
            horizontal.grid(row=2, column=column, sticky='ew', padx=4)
            text.configure(xscrollcommand=horizontal.set)
        self.vertical = ttk.Scrollbar(panes, orient='vertical', command=self.scroll)
        self.vertical.grid(row=1, column=2, sticky='ns')
        ttk.Label(frame, text='Original line numbers · Red/green rows show changes · Display strips ANSI/control codes; saved bytes are unchanged.',
                  wraplength=1260, style='Subtitle.TLabel').pack(anchor='w', pady=(8, 0))
        self.query.trace_add('write', lambda *_: self.schedule_search())
        self.bind('<Control-f>', self.focus_search)
        self.bind('<Control-F>', self.focus_search)
        self.bind('<F3>', lambda event: self.key_action(self.jump_match, 1))
        self.bind('<Shift-F3>', lambda event: self.key_action(self.jump_match, -1))
        self.bind('<Control-Down>', lambda event: self.key_action(self.jump_issue, 1))
        self.bind('<Control-Up>', lambda event: self.key_action(self.jump_issue, -1))
        self.protocol('WM_DELETE_WINDOW', self.close)
        if self.theme:
            self.theme_trace = self.theme.mode.trace_add('write', lambda *_: self.update_colors())
        self.update_colors()
        def worker():
            try:
                a, left = load_log(store, before)
                b, right = load_log(store, after)
                model = align_logs(left['lines'], right['lines'], self.stop.is_set)
                self.events.put(('ready', (a, b, left, right, model)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()
        self.poll_timer = self.after(50, self.poll)
        self.bind('<Destroy>', lambda event: self.cleanup() if event.widget is self else None)

    @staticmethod
    def heading(side, record):
        return f"{side.title()} · {run_name(record)} · {record['status']} · exit {record['exit_code']}\n{record['started_at']}"

    def update_colors(self):
        if self.closed:
            return
        p = self.theme.colors if self.theme else PALETTES['dark']
        if self.theme:
            self.theme.paint(self)
        self.configure(background=p['bg'])
        for side, text in self.texts.items():
            text.configure(background=p['field'], foreground=p['fg'], insertbackground=p['fg'])
            text.tag_configure('line_number', foreground=p['muted'])
            text.tag_configure('changed', background=p['remove'] if side == 'before' else p['add'])
            text.tag_configure('spacer', background=p['spacer'])
            dark = not self.theme or self.theme.mode.get() == 'dark'
            text.tag_configure('error', foreground='#ff8080' if dark else '#a80000')
            text.tag_configure('warning', foreground='#e5c07b' if dark else '#785200')
            text.tag_configure('current_issue', background=p['select'], underline=True)
            text.tag_configure('current_change', underline=True)
            text.tag_configure('search_match', background=p['search'])
            text.tag_configure('search_current', background=p['search_active'], foreground=p['search_text'])
            for tag in ('current_issue', 'search_match', 'search_current', 'sel'):
                text.tag_raise(tag)

    def poll(self):
        if self.closed:
            return
        try:
            kind, payload = self.events.get_nowait()
        except queue.Empty:
            self.poll_timer = self.after(50, self.poll)
            return
        if kind == 'error':
            self.failure = payload
            self.summary.set('Log comparison unavailable: ' + payload)
            return
        a, b, left, right, self.model = payload
        self.headings['before'].configure(text=self.heading('before', a))
        self.headings['after'].configure(text=self.heading('after', b))
        notices = [f'{side}: {note}' for side, log in (('Before', left), ('After', right)) for note in log['notices']]
        if self.model['positional_fallback']:
            notices.append('Large edited section is aligned by position; surrounding equal lines remain anchored.')
        self.summary.set(f"{len(left['lines'])} before / {len(right['lines'])} after visible lines · {len(self.model['changes'])} change section(s). " + ' '.join(notices))
        self.render()
        self.ready = True
        self.export_button.configure(state='normal')
        self.filter_issues()
        self.update_search()

    def export_html(self):
        if not self.ready:
            return
        path = filedialog.asksaveasfilename(parent=self, title='Export side-by-side build logs',
            defaultextension='.html', initialfile='BuildTrail-log-comparison.html',
            filetypes=[('HTML report', '*.html')])
        if not path:
            return
        try:
            from pathlib import Path
            from .logreport import export_log_report
            export_log_report(self.store, *self.records, Path(path), query=self.query.get(),
                match_case=self.match_case.get(), issue_kind=self.issue_kind.get(),
                issue_side=self.issue_side.get(), theme=self.theme.mode.get() if self.theme else 'dark')
            messagebox.showinfo('Export complete', 'Saved offline side-by-side log report:\n' + path, parent=self)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            messagebox.showerror('Export failed', str(exc), parent=self)

    def render(self):
        for side, text in self.texts.items():
            lines = []
            for row in self.model['rows']:
                number = row[side + '_number']
                lines.append(f'{number:>5} │ {row[side]}\n' if number is not None else '\n')
            text.configure(state='normal')
            text.insert('1.0', ''.join(lines))
            for index, row in enumerate(self.model['rows'], 1):
                start, end = f'{index}.0', f'{index}.end'
                if row[side] is None:
                    text.tag_add('spacer', start, end + '+1c')
                else:
                    text.tag_add('line_number', start, f'{index}.8')
                    if row['kind'] != 'equal':
                        text.tag_add('changed', start, end + '+1c')
            text.configure(state='disabled')
        for issue in self.model['diagnostics']:
            self.texts[issue['side']].tag_add(issue['severity'], f"{issue['row']}.8", f"{issue['row']}.end")
        self.update_colors()

    def sync_scroll(self, side, first, last):
        if self.syncing:
            return
        self.syncing = True
        try:
            self.vertical.set(first, last)
            other = self.texts['after' if side == 'before' else 'before']
            other.yview_moveto(first)
        finally:
            self.syncing = False

    def scroll(self, *args):
        for text in self.texts.values():
            text.yview(*args)

    def reveal(self, row, side, index=None):
        for text in self.texts.values():
            text.yview(f'{row}.0')
        self.texts[side].see(index or f'{row}.8')
        self.texts[side].focus_set()

    @staticmethod
    def key_action(action, direction):
        action(direction)
        return 'break'

    def focus_search(self, event=None):
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, 'end')
        return 'break'

    def schedule_search(self):
        if self.search_timer:
            self.after_cancel(self.search_timer)
        self.search_timer = self.after(200, self.update_search)

    def match_range(self, match):
        value = self.model['rows'][match['row'] - 1][match['side']]
        # Tk 8.6 on Windows counts supplementary Unicode as two characters.
        start = 8 + int(self.tk.call('string', 'length', value[:match['start']]))
        end = 8 + int(self.tk.call('string', 'length', value[:match['end']]))
        return f"{match['row']}.{start}", f"{match['row']}.{end}"

    def update_search(self):
        if self.closed:
            return
        if self.search_timer:
            self.after_cancel(self.search_timer)
            self.search_timer = None
        for text in self.texts.values():
            text.tag_remove('search_match', '1.0', 'end')
            text.tag_remove('search_current', '1.0', 'end')
        self.matches, capped = search_logs(self.model['rows'], self.query.get(), self.match_case.get())
        self.match_index = -1
        for match in self.matches:
            self.texts[match['side']].tag_add('search_match', *self.match_range(match))
        self.search_count = str(len(self.matches)) + ('+' if capped else '')
        self.search_status.set(f'{self.search_count} matches in visible log text' if self.query.get() else 'Ctrl+F · F3 / Shift+F3 · Literal search')

    def jump_match(self, direction):
        if self.search_timer:
            self.update_search()
        if not self.matches:
            return
        self.match_index = (self.match_index + direction) % len(self.matches) if self.match_index >= 0 else (0 if direction > 0 else len(self.matches) - 1)
        match = self.matches[self.match_index]
        for text in self.texts.values():
            text.tag_remove('search_current', '1.0', 'end')
        start, end = self.match_range(match)
        self.texts[match['side']].tag_add('search_current', start, end)
        self.reveal(match['row'], match['side'], start)
        self.search_status.set(f"Match {self.match_index + 1}/{self.search_count} · {match['side'].title()} line {self.model['rows'][match['row'] - 1][match['side'] + '_number']}")

    def filter_issues(self, event=None):
        self.issue_index = -1
        side = self.issue_side.get().casefold()
        severity = {'Errors': 'error', 'Warnings': 'warning'}.get(self.issue_kind.get())
        self.issues = [issue for issue in self.model['diagnostics'] if (side == 'both logs' or issue['side'] == side) and (severity is None or issue['severity'] == severity)]
        for text in self.texts.values():
            text.tag_remove('current_issue', '1.0', 'end')
        errors = sum(issue['severity'] == 'error' for issue in self.issues)
        self.issue_status.set(f'{errors} error / {len(self.issues) - errors} warning matches in visible text · Ctrl+↑ / Ctrl+↓ · Heuristic detection')

    def jump_issue(self, direction):
        if not self.ready or not self.issues:
            return
        self.issue_index = (self.issue_index + direction) % len(self.issues) if self.issue_index >= 0 else (0 if direction > 0 else len(self.issues) - 1)
        issue = self.issues[self.issue_index]
        for text in self.texts.values():
            text.tag_remove('current_issue', '1.0', 'end')
        text = self.texts[issue['side']]
        text.tag_add('current_issue', f"{issue['row']}.8", f"{issue['row']}.end")
        text.xview_moveto(0)
        self.reveal(issue['row'], issue['side'])
        self.issue_status.set(f"Issue {self.issue_index + 1}/{len(self.issues)} · {issue['side'].title()} line {issue['number']} · {issue['severity']}")

    def jump_change(self, direction):
        changes = self.model['changes']
        if not self.ready or not changes:
            return
        self.change_index = (self.change_index + direction) % len(changes) if self.change_index >= 0 else (0 if direction > 0 else len(changes) - 1)
        row = changes[self.change_index]
        for text in self.texts.values():
            text.tag_remove('current_change', '1.0', 'end')
            text.tag_add('current_change', f'{row}.0', f'{row}.end')
        side = 'after' if self.model['rows'][row - 1]['after'] is not None else 'before'
        self.reveal(row, side)

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        self.after_cancel(self.poll_timer)
        if self.search_timer:
            self.after_cancel(self.search_timer)
        if self.theme and self.theme_trace:
            self.theme.mode.trace_remove('write', self.theme_trace)

    def close(self):
        self.cleanup()
        self.destroy()
