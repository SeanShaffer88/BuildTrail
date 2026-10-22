"""Windows desktop build recipes, history, logs, and comparison reports."""
import json
from pathlib import Path
import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import webbrowser

from . import __version__
from .core import (BuildStore, default_recipe, validate_recipe, run_build, source_comparison,
                   table_changes)
from .report import render_report, export_report
from .organization import run_name
from .logview import LogDiffWindow
from ._apptrail.core import atomic_write
from ._apptrail.history import SnapshotHistory
from ._apptrail.checkpoints import capture_checkpoint
from ._apptrail.report import render_html
from ._apptrail.theme import Theme


class BuildTrail:
    def __init__(self, root, store=None, history=None):
        self.root = root
        self.store = store if store is not None else BuildStore()
        self.history = history if history is not None else SnapshotHistory()
        self.theme = Theme(root, self.store.directory / 'preferences.json')
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.review_runs = ()
        self.checkpoints = {}
        self.records = {}
        root.title(f'BuildTrail {__version__} · Build evidence')
        root.geometry('1240x900')
        root.minsize(1050, 780)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        title = ttk.Frame(frame)
        title.pack(fill='x')
        ttk.Label(title, text='BuildTrail', font=('Segoe UI', 26, 'bold')).pack(side='left')
        ttk.Label(title, text='Commands → evidence → comparison', font=('Segoe UI', 12)).pack(side='left', padx=18)
        ttk.Button(title, text='Toggle theme', command=self.theme.toggle).pack(side='right')
        self.tabs = ttk.Notebook(frame)
        self.tabs.pack(fill='both', expand=True, pady=14)
        self.recipe_tab = ttk.Frame(self.tabs, padding=14)
        self.history_tab = ttk.Frame(self.tabs, padding=14)
        self.review_tab = ttk.Frame(self.tabs, padding=14)
        for tab, label in ((self.recipe_tab, 'Build recipe'), (self.history_tab, 'Build history'), (self.review_tab, 'Review & logs')):
            self.tabs.add(tab, text=label)
        self.build_recipe()
        self.build_history()
        self.build_review()
        bottom = ttk.Frame(frame)
        bottom.pack(fill='x')
        self.status = tk.StringVar(value='Ready · Builds and AppTrail checkpoints stay on this computer')
        ttk.Label(bottom, textvariable=self.status, wraplength=920).pack(side='left', fill='x', expand=True)
        self.cancel = ttk.Button(bottom, text='Stop build', command=self.stop.set, state='disabled')
        self.cancel.pack(side='right')
        self.apply_recipe(default_recipe())
        self.refresh_recipes()
        self.refresh_history()
        self.theme.paint(root)
        self.poll_id = root.after(70, self.poll)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def build_recipe(self):
        frame = self.recipe_tab
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=(0, 12))
        ttk.Label(bar, text='Saved recipes').pack(side='left')
        self.recipe_choice = ttk.Combobox(bar, state='readonly', width=75)
        self.recipe_choice.pack(side='left', padx=8, fill='x', expand=True)
        self.recipe_choice.bind('<<ComboboxSelected>>', self.load_recipe)
        ttk.Button(bar, text='Save recipe', command=self.save_recipe).pack(side='left')
        self.name = tk.StringVar()
        self.project = tk.StringVar()
        for label, variable in (('Recipe name', self.name), ('Project folder', self.project)):
            row = ttk.Frame(frame)
            row.pack(fill='x', pady=4)
            ttk.Label(row, text=label, width=16).pack(side='left')
            ttk.Entry(row, textvariable=variable).pack(side='left', fill='x', expand=True)
            if label == 'Project folder':
                ttk.Button(row, text='Choose…', command=self.choose_project).pack(side='left', padx=6)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=6)
        ttk.Label(row, text='Execution', width=16).pack(side='left')
        self.shell = tk.StringVar(value='powershell')
        ttk.Combobox(row, textvariable=self.shell, values=('powershell', 'argv'), state='readonly', width=14).pack(side='left')
        self.timeout = tk.StringVar(value='1800')
        ttk.Label(row, text='Timeout (seconds)').pack(side='left', padx=(18, 6))
        ttk.Entry(row, textvariable=self.timeout, width=8).pack(side='left')
        self.capture_text = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text='Capture AppTrail source/config text', variable=self.capture_text).pack(side='left', padx=18)
        ttk.Label(frame, text='Build command · PowerShell script, or a JSON argv array when execution is argv').pack(anchor='w', pady=(6, 2))
        self.command = tk.Text(frame, height=3, wrap='word', font=('Consolas', 11), undo=True)
        self.command.pack(fill='x')
        columns = ttk.Frame(frame)
        columns.pack(fill='both', expand=True, pady=10)
        self.rules = self.text_column(columns, 'Source/dependency exclusions · one glob per line', 0)
        self.outputs = self.text_column(columns, 'Output paths · relative globs, one per line', 1)
        self.tools = self.text_column(columns, 'Compiler/tool probes · one JSON argv array per line', 2)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text='AppTrail baseline').pack(side='left')
        self.checkpoint = ttk.Combobox(row, state='readonly')
        self.checkpoint.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(row, text='Refresh', command=self.refresh_checkpoints).pack(side='left')
        ttk.Button(row, text='Save checkpoint…', command=self.save_checkpoint).pack(side='left', padx=6)
        ttk.Label(frame, text='Linked baselines reuse their saved exclusions and text setting. Each run also captures its current source into AppTrail history.',
                  wraplength=1100).pack(anchor='w', pady=6)
        self.run_button = ttk.Button(frame, text='Run build & capture evidence', command=self.run)
        self.run_button.pack(anchor='w', pady=8)
        ttk.Label(frame, text='Recipes run with your permissions and inherited environment. Logs, commands, manifests, and snapshots can contain private data; review reports before sharing.',
                  wraplength=1100, style='Subtitle.TLabel').pack(anchor='w')

    def text_column(self, frame, label, column):
        frame.columnconfigure(column, weight=1, uniform='config')
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text=label, wraplength=350).grid(row=0, column=column, sticky='w', padx=4, pady=4)
        text = tk.Text(frame, height=7, width=25, font=('Consolas', 10), wrap='none', undo=True)
        text.grid(row=1, column=column, sticky='nsew', padx=4)
        return text

    def build_history(self):
        frame = self.history_tab
        ttk.Label(frame, text='Select one run to inspect it, or two to compare inputs, tools, logs, outputs, and source.',
                  wraplength=1100).pack(anchor='w', pady=(0, 12))
        controls = ttk.Frame(frame)
        controls.pack(fill='x', pady=(0, 10))
        ttk.Label(controls, text='Filter names, notes, recipes, or projects:').pack(side='left')
        self.history_filter = tk.StringVar()
        ttk.Entry(controls, textvariable=self.history_filter, width=45).pack(side='left', padx=8)
        self.show_trash = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text='Show Trash', variable=self.show_trash, command=self.refresh_history).pack(side='right')
        self.history_filter.trace_add('write', lambda *_: self.refresh_history())
        self.tree = ttk.Treeview(frame, columns=('name', 'status', 'exit', 'time', 'duration', 'project'), show='headings', selectmode='extended')
        for key, title, width in [('name', 'Run name', 220), ('status', 'Result', 95), ('exit', 'Exit', 50),
                                  ('time', 'Started (UTC)', 180), ('duration', 'Seconds', 80), ('project', 'Project', 450)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=45)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Double-1>', lambda event: self.view_selected())
        self.tree.bind('<<TreeviewSelect>>', self.history_selection_changed)
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=12)
        for title, command in [('View run', self.view_selected), ('Compare selected (2)', self.compare_selected),
                               ('Compare logs (2)', self.compare_selected_logs),
                               ('Refresh history', self.refresh_history), ('Load run recipe', self.recipe_from_run)]:
            ttk.Button(bar, text=title, command=command).pack(side='left', padx=(0, 8))

        organize = ttk.Frame(frame)
        organize.pack(fill='x', pady=(0, 8))
        for title, command in [('Edit name & notes…', self.edit_run), ('Move to Trash…', self.trash_runs),
                               ('Restore selected', self.restore_runs), ('Delete permanently…', self.purge_runs)]:
            ttk.Button(organize, text=title, command=command).pack(side='left', padx=(0, 8))
        self.history_info = tk.StringVar(value='Select a run to see its notes.')
        ttk.Label(frame, textvariable=self.history_info, wraplength=1100, style='Subtitle.TLabel').pack(anchor='w')

    def build_review(self):
        bar = ttk.Frame(self.review_tab)
        bar.pack(fill='x', pady=(0, 10))
        ttk.Button(bar, text='Open full HTML review', command=self.open_review).pack(side='left')
        ttk.Button(bar, text='Export HTML…', command=self.export).pack(side='left', padx=8)
        ttk.Button(bar, text='Open AppTrail source comparison', command=self.open_source).pack(side='left')
        ttk.Button(bar, text='Side-by-side logs…', command=self.open_log_diff).pack(side='left', padx=8)
        self.review = tk.Text(self.review_tab, wrap='none', font=('Consolas', 10), state='disabled')
        scroll = ttk.Scrollbar(self.review_tab, orient='vertical', command=self.review.yview)
        scroll.pack(side='right', fill='y')
        self.review.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(self.review_tab, orient='horizontal', command=self.review.xview)
        horizontal.pack(side='bottom', fill='x')
        self.review.configure(xscrollcommand=horizontal.set)
        self.review.pack(fill='both', expand=True)

    def set_review(self, text):
        self.review.configure(state='normal')
        self.review.delete('1.0', 'end')
        self.review.insert('1.0', text)
        self.review.configure(state='disabled')

    def append_review(self, text):
        text = text[-200000:]
        self.review.configure(state='normal')
        self.review.insert('end', text)
        if self.review.count('1.0', 'end-1c', 'chars')[0] > 200000:
            self.review.delete('1.0', 'end-200000c')
        self.review.see('end')
        self.review.configure(state='disabled')

    def apply_recipe(self, recipe):
        self.name.set(recipe['name'])
        self.project.set(recipe['project'])
        self.shell.set(recipe.get('shell', 'powershell'))
        self.timeout.set(str(recipe['timeout_seconds']))
        self.capture_text.set(recipe['capture_text'])
        command = json.dumps(recipe['argv'], ensure_ascii=False) if recipe.get('shell') == 'argv' else recipe['command']
        for widget, content in ((self.command, command), (self.rules, '\n'.join(recipe['exclusions'])),
            (self.outputs, '\n'.join(recipe['output_patterns'])), (self.tools, '\n'.join(json.dumps(t, ensure_ascii=False) for t in recipe['tools']))):
            widget.delete('1.0', 'end')
            widget.insert('1.0', content)
        self.refresh_checkpoints(recipe.get('checkpoint_id', ''))

    def get_recipe(self):
        recipe = default_recipe(self.project.get().strip())
        recipe.update(name=self.name.get().strip(), shell=self.shell.get(), timeout_seconds=int(self.timeout.get()),
            capture_text=self.capture_text.get(), command=self.command.get('1.0', 'end-1c'),
            exclusions=self.rules.get('1.0', 'end-1c').splitlines(),
            output_patterns=[p.strip() for p in self.outputs.get('1.0', 'end-1c').splitlines() if p.strip()],
            tools=[json.loads(line) for line in self.tools.get('1.0', 'end-1c').splitlines() if line.strip()],
            checkpoint_id=self.checkpoints.get(self.checkpoint.get(), ''))
        if recipe['shell'] == 'argv':
            recipe['argv'] = json.loads(recipe['command'])
        return validate_recipe(recipe)

    def refresh_recipes(self):
        self.saved = self.store.recipes()
        self.recipe_choice['values'] = [r['name'] + ' · ' + r['project'] for r in self.saved]

    def load_recipe(self, event=None):
        index = self.recipe_choice.current()
        if index >= 0:
            self.apply_recipe(self.saved[index])

    def save_recipe(self):
        try:
            recipe = self.get_recipe()
            self.store.save_recipe(recipe)
            self.refresh_recipes()
            self.status.set('Recipe saved. Loading a recipe does not execute it.')
        except (OSError, ValueError) as exc:
            messagebox.showerror('Recipe settings', str(exc), parent=self.root)

    def choose_project(self):
        path = filedialog.askdirectory(title='Choose project folder', parent=self.root)
        if path:
            self.project.set(path)
            self.refresh_checkpoints()

    def refresh_checkpoints(self, selected=None):
        if selected is None:
            selected = self.checkpoints.get(self.checkpoint.get(), '')
        self.checkpoints = {'No linked baseline · capture current source only': ''}
        project = str(Path(self.project.get()).resolve()) if self.project.get() else ''
        try:
            for entry in self.history.entries():
                if self.history.root_key(entry['root']) != self.history.root_key(project):
                    continue
                snapshot = self.history.load(entry['id'])
                if snapshot.get('checkpoint'):
                    label = f"{entry['label']} · {entry['captured_at']} · {entry['id'][:8]}"
                    self.checkpoints[label] = entry['id']
        except (OSError, ValueError) as exc:
            messagebox.showerror('AppTrail checkpoints', str(exc), parent=self.root)
        self.checkpoint['values'] = list(self.checkpoints)
        self.checkpoint.set(next((label for label, identity in self.checkpoints.items() if identity == selected), next(iter(self.checkpoints))))

    def start_job(self, job):
        if self.busy:
            return
        self.busy = True
        self.stop.clear()
        self.run_button.configure(state='disabled')
        self.cancel.configure(state='normal')
        def worker():
            try:
                self.events.put(('done', job()))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def save_checkpoint(self):
        if self.busy:
            return
        try:
            recipe = self.get_recipe()
        except ValueError as exc:
            messagebox.showerror('Recipe settings', str(exc), parent=self.root)
            return
        name = simpledialog.askstring('AppTrail checkpoint', 'Name this source baseline:', parent=self.root)
        if not name:
            return
        self.status.set('Saving AppTrail checkpoint…')
        self.start_job(lambda: ('checkpoint', capture_checkpoint(self.history, recipe['project'], name,
            recipe['exclusions'], recipe['capture_text'], cancelled=self.stop.is_set)[0]))

    def run(self):
        if self.busy:
            return
        try:
            recipe = self.get_recipe()
        except ValueError as exc:
            messagebox.showerror('Recipe settings', str(exc), parent=self.root)
            return
        self.review_runs = ()
        self.set_review('Starting build evidence capture…\n')
        self.tabs.select(self.review_tab)
        self.status.set('Build running · Stop build cancels the owned process tree')
        def output(text):
            if self.events.qsize() < 200:
                self.events.put(('output', text))
        self.start_job(lambda: ('run', run_build(recipe, self.store, self.history, self.stop.is_set, output)))

    def refresh_history(self):
        selected = self.tree.selection()
        try:
            self.records = {r['id']: r for r in self.store.entries(self.history_filter.get(), deleted=self.show_trash.get())}
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.status.set('Cannot load run history: ' + str(exc))
            return
        self.tree.delete(*self.tree.get_children())
        for record in self.records.values():
            self.tree.insert('', 'end', iid=record['id'], values=(run_name(record), record['status'],
                record['exit_code'] if record['exit_code'] is not None else '—', record['started_at'],
                record.get('duration_seconds', '—'), record['recipe']['project']))
        self.tree.selection_set([identity for identity in selected if identity in self.records])
        self.history_selection_changed()

    def history_selection_changed(self, event=None):
        identities = self.tree.selection()
        if len(identities) == 1 and identities[0] in self.records:
            run = self.records[identities[0]]
            notes = run.get('organization', {}).get('notes') or 'No notes yet.'
            preview = ' '.join(notes.split())
            self.history_info.set(run_name(run) + ' · ' + preview[:400] + ('…' if len(preview) > 400 else ''))
        else:
            self.history_info.set(f'{len(identities)} runs selected' if identities else 'Select a run to see its notes.')

    def edit_run(self):
        if self.busy:
            return
        runs = self.selected(1)
        if runs:
            return RunEditor(self.root, self.store, runs[0], self.metadata_saved)

    def metadata_saved(self, identity):
        self.refresh_history()
        if identity in self.records:
            self.tree.selection_set(identity)
            self.tree.see(identity)
        if any(r['id'] == identity for r in self.review_runs):
            self.show_runs([self.store.load(r['id']) for r in self.review_runs])

    def organize_selection(self, trash=False):
        if self.busy:
            messagebox.showinfo('Build in progress', 'Wait until the current capture finishes before organizing runs.', parent=self.root)
            return []
        if self.show_trash.get() != trash:
            messagebox.showinfo('Choose a history view', 'Turn on Show Trash first.' if trash else 'Turn off Show Trash first.', parent=self.root)
            return []
        identities = list(self.tree.selection())
        if not identities:
            messagebox.showinfo('Select runs', 'Select at least one run.', parent=self.root)
        return identities

    def clear_removed_review(self, identities):
        if any(r['id'] in identities for r in self.review_runs):
            self.review_runs = ()
            self.set_review('A reviewed run was moved to Trash or deleted. Restore it or select other runs.')

    def trash_runs(self):
        identities = self.organize_selection()
        if not identities:
            return
        if not messagebox.askyesno('Move runs to Trash?', f'Move {len(identities)} build run(s) to Trash?\n\nYou can restore them later. Project files, recipes, and AppTrail history are untouched.', parent=self.root):
            return
        try:
            self.store.trash(identities)
            self.clear_removed_review(identities)
            self.refresh_history()
            self.status.set(f'Moved {len(identities)} run(s) to Trash. Turn on Show Trash to restore them.')
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror('Cannot move runs', str(exc), parent=self.root)

    def restore_runs(self):
        identities = self.organize_selection(trash=True)
        if not identities:
            return
        try:
            self.store.restore(identities)
            self.refresh_history()
            self.status.set(f'Restored {len(identities)} run(s). Turn off Show Trash to see them.')
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror('Cannot restore runs', str(exc), parent=self.root)

    def purge_runs(self):
        identities = self.organize_selection(trash=True)
        if not identities:
            return
        if not messagebox.askyesno('Permanently delete runs?', f'Permanently delete {len(identities)} trashed run(s), their notes, logs, and saved evidence?\n\nThis cannot be undone. Project outputs, recipes, and shared AppTrail checkpoints remain.', parent=self.root, icon='warning', default='no'):
            return
        try:
            self.store.purge(identities)
            self.clear_removed_review(identities)
            self.refresh_history()
            self.status.set(f'Permanently deleted {len(identities)} owned build run(s).')
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.refresh_history()
            messagebox.showerror('Deletion incomplete', str(exc), parent=self.root)

    def selected(self, count):
        selected = self.tree.selection()
        if len(selected) != count:
            messagebox.showinfo('Select builds', f'Select exactly {count} build run(s) in Build history.', parent=self.root)
            return []
        if self.show_trash.get():
            messagebox.showinfo('Restore first', 'Restore runs from Trash before reviewing, comparing, or loading their recipes.', parent=self.root)
            return []
        try:
            return [self.store.load(identity) for identity in selected]
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror('Run history', str(exc), parent=self.root)
            return []

    def view_selected(self):
        if runs := self.selected(1):
            self.show_runs(runs)

    def compare_selected(self):
        if runs := self.selected(2):
            self.show_runs(sorted(runs, key=lambda r: (r.get('started_ns', 0), r['id'])))

    def compare_selected_logs(self):
        if runs := self.selected(2):
            runs = sorted(runs, key=lambda r: (r.get('started_ns', 0), r['id']))
            return LogDiffWindow(self.root, self.store, *runs)

    def open_log_diff(self):
        try:
            before, after = self.chosen_report()
            if after is None:
                raise ValueError('Compare two build runs first, or select two history rows and click Compare logs (2).')
            return LogDiffWindow(self.root, self.store, before, after)
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror('Build log comparison', str(exc), parent=self.root)

    def recipe_from_run(self):
        if runs := self.selected(1):
            self.apply_recipe(runs[0]['recipe'])
            self.tabs.select(self.recipe_tab)

    def show_runs(self, runs):
        self.review_runs = tuple(runs)
        text = []
        for run in runs:
            text.append(f"{run_name(run)} · {run['status']} · exit {run['exit_code']}\n{run['started_at']} · {run['recipe']['project']}\n")
            text.append('Original recipe: ' + run['recipe']['name'])
            if run.get('organization', {}).get('notes'):
                text.append('Notes: ' + run['organization']['notes'])
            text.append('Command: ' + (json.dumps(run['recipe']['argv']) if run['recipe'].get('shell') == 'argv' else run['recipe']['command']))
            text.append('AppTrail source: ' + run.get('source_id', 'unavailable'))
            for row in run.get('output_changes', []):
                text.append(f"  {row['state']}: {row['path']}")
            text.extend('Note: ' + note for note in run['warnings'])
        if len(runs) == 2:
            text.append('\nOutput differences between builds:')
            text.extend(f"  {r['state']}: {r['path']}" for r in table_changes(runs[0]['outputs_after'], runs[1]['outputs_after'], runs[0]['outputs_after_complete'], runs[1]['outputs_after_complete']))
            try:
                comparison = source_comparison(self.store, *runs)
                text.append('\nSource changes: ' + json.dumps(comparison['counts']))
            except (OSError, ValueError) as exc:
                text.append(str(exc))
        else:
            try:
                log = self.store.evidence(runs[0], 'build.log').read_text(encoding='utf-8', errors='replace')
                text.append('\nCaptured build log (first 200,000 characters):\n' + log[:200000])
            except (OSError, ValueError) as exc:
                text.append(str(exc))
        self.set_review('\n'.join(text))
        self.tabs.select(self.review_tab)

    def chosen_report(self):
        if not self.review_runs:
            raise ValueError('View a saved run or compare two runs first.')
        runs = [self.store.load(r['id']) for r in self.review_runs]
        return runs[0], runs[1] if len(runs) == 2 else None

    def open_review(self):
        try:
            before, after = self.chosen_report()
            path = self.store.directory / 'review.html'
            atomic_write(path, render_report(self.store, before, after))
            webbrowser.open(path.as_uri())
        except (OSError, ValueError) as exc:
            messagebox.showerror('Build review', str(exc), parent=self.root)

    def export(self):
        try:
            before, after = self.chosen_report()
            path = filedialog.asksaveasfilename(title='Export build comparison', defaultextension='.html',
                initialfile='BuildTrail-report.html', filetypes=[('HTML report', '*.html')], parent=self.root)
            if path:
                target = Path(path).resolve()
                if target == self.history.directory or self.history.directory in target.parents:
                    raise ValueError('Export outside AppTrail history.')
                export_report(self.store, before, after, target)
                self.status.set('Exported report: ' + str(target))
        except (OSError, ValueError) as exc:
            messagebox.showerror('Export report', str(exc), parent=self.root)

    def open_source(self):
        try:
            before, after = self.chosen_report()
            comparison = source_comparison(self.store, before, after)
            path = self.store.directory / 'source-review.html'
            atomic_write(path, render_html(comparison))
            webbrowser.open(path.as_uri())
        except (OSError, ValueError) as exc:
            messagebox.showerror('AppTrail source comparison', str(exc), parent=self.root)

    def poll(self):
        for _ in range(250):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'output':
                self.append_review(payload)
                continue
            self.busy = False
            self.run_button.configure(state='normal')
            self.cancel.configure(state='disabled')
            if kind == 'error':
                self.status.set('Operation stopped')
                messagebox.showerror('BuildTrail', payload, parent=self.root)
            elif payload[0] == 'run':
                record = payload[1]
                self.show_trash.set(False)
                self.history_filter.set('')
                self.refresh_history()
                self.tree.selection_set(record['id'])
                self.tree.see(record['id'])
                self.show_runs([record])
                self.status.set(f"Build {record['status']} · exit {record['exit_code']} · evidence saved")
            else:
                self.refresh_checkpoints(payload[1])
                self.status.set('AppTrail checkpoint saved and linked to this recipe')
        self.poll_id = self.root.after(70, self.poll)

    def close(self):
        if self.busy:
            self.stop.set()
            self.status.set('Stopping capture/build before closing…')
            self.root.after(100, self.close)
            return
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


class RunEditor(tk.Toplevel):
    def __init__(self, parent, store, record, on_saved):
        super().__init__(parent)
        self.store, self.identity, self.on_saved = store, record['id'], on_saved
        self.title('Edit build run name and notes')
        self.transient(parent)
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Run name').pack(anchor='w')
        self.name = tk.StringVar(value=run_name(record))
        entry = ttk.Entry(frame, textvariable=self.name, width=70)
        entry.pack(fill='x', pady=(4, 12))
        ttk.Label(frame, text='Notes · test results, issue numbers, or why this build matters').pack(anchor='w')
        self.notes = tk.Text(frame, width=70, height=10, wrap='word', font=('Segoe UI', 10))
        self.notes.insert('1.0', record.get('organization', {}).get('notes', ''))
        self.notes.pack(fill='both', expand=True, pady=(4, 12))
        ttk.Label(frame, text='Names and notes are stored separately from the original recipe and captured evidence.').pack(anchor='w')
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=(12, 0))
        ttk.Button(bar, text='Save', command=self.save).pack(side='right')
        ttk.Button(bar, text='Cancel', command=self.destroy).pack(side='right', padx=8)
        theme = getattr(parent, '_apptrail_theme', None)
        if theme:
            theme.paint(self)
        self.grab_set()
        entry.focus_set()
        entry.selection_range(0, 'end')

    def save(self):
        try:
            self.store.update_metadata(self.identity, self.name.get(), self.notes.get('1.0', 'end-1c'))
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror('Cannot save run details', str(exc), parent=self)
            return
        self.on_saved(self.identity)
        self.destroy()


def main():
    root = tk.Tk()
    try:
        BuildTrail(root)
    except Exception as exc:
        root.withdraw()
        messagebox.showerror('BuildTrail unavailable', str(exc), parent=root)
        root.destroy()
        return
    root.mainloop()
