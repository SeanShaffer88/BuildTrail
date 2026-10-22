import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from buildtrail.core import BuildStore, default_recipe, run_build
from buildtrail.gui import BuildTrail
from buildtrail.logview import LogDiffWindow
from buildtrail._apptrail.history import SnapshotHistory


@unittest.skipUnless(os.name == 'nt', 'Windows Tk log review')
class LogViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        project = self.base / 'project'
        project.mkdir()
        (project / 'source.py').write_text('value=1')
        self.store = BuildStore(self.base / 'builds')
        self.history = SnapshotHistory(self.base / 'snapshots')
        self.before_text = 'start\nwarning: deprecated\n😀 a+b shared\ndone\n'
        self.after_text = 'start\nwarning: deprecated\n😀 a+b shared\nmain.cpp(7): error C2143: missing token\nfailed\n'
        context = ''.join(f'context line {i}\n' for i in range(200))
        self.before_text += context
        self.after_text += context
        recipe = default_recipe(project)
        recipe.update(shell='argv', tools=[])
        def build(text, exit_code):
            recipe['argv'] = [sys.executable, '-c', f'import sys; sys.stdout.buffer.write({text!r}.encode("utf-8")); sys.exit({exit_code})']
            return run_build(recipe, self.store, self.history)
        self.before = build(self.before_text, 0)
        self.after = build(self.after_text, 1)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = BuildTrail(self.root, self.store, self.history)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            if window.winfo_exists():
                window.close()
        self.app.close()
        self.temp.cleanup()

    def wait(self, window):
        deadline = time.monotonic() + 8
        while not window.ready and window.failure is None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(window.ready, window.failure or 'Comparison did not finish')

    def open(self):
        window = LogDiffWindow(self.root, self.store, self.before, self.after)
        self.windows.append(window)
        self.wait(window)
        return window

    def test_read_only_alignment_and_saved_evidence_unchanged(self):
        original = self.store.evidence(self.before, 'build.log').read_bytes()
        window = self.open()
        self.assertEqual(window.texts['before'].cget('state'), 'disabled')
        self.assertIn('warning: deprecated', window.texts['before'].get('1.0', 'end'))
        self.assertIn('error C2143', window.texts['after'].get('1.0', 'end'))
        self.assertEqual(len(window.model['diagnostics']), 3)
        self.assertEqual(self.store.evidence(self.before, 'build.log').read_bytes(), original)

    def test_search_both_logs_unicode_offsets_navigation_and_clear(self):
        window = self.open()
        window.query.set('a+b')
        window.update_search()
        self.assertEqual(len(window.matches), 2)
        window.jump_match(1)
        self.assertIn('Before line 3', window.search_status.get())
        match = window.matches[window.match_index]
        self.assertEqual(window.texts[match['side']].get(*window.match_range(match)), 'a+b')
        window.jump_match(1)
        self.assertIn('After line 3', window.search_status.get())
        window.jump_match(1)
        self.assertEqual(window.match_index, 0)
        window.jump_match(-1)
        self.assertEqual(window.match_index, 1)
        window.query.set('')
        window.update_search()
        self.assertEqual(window.matches, [])
        self.assertFalse(window.texts['after'].tag_ranges('search_current'))

    def test_error_warning_side_filters_and_wrap(self):
        window = self.open()
        window.issue_kind.set('Errors')
        window.filter_issues()
        self.assertEqual(len(window.issues), 1)
        window.jump_issue(1)
        self.assertIn('After line 4 · error', window.issue_status.get())
        window.jump_issue(1)
        self.assertEqual(window.issue_index, 0)
        window.issue_kind.set('Warnings')
        window.issue_side.set('Before')
        window.filter_issues()
        self.assertEqual(len(window.issues), 1)
        window.jump_issue(-1)
        self.assertIn('Before line 2 · warning', window.issue_status.get())
        window.issue_kind.set('Errors')
        window.filter_issues()
        self.assertEqual(window.issues, [])
        window.jump_issue(1)

    def test_from_history_and_review_compares_chronologically(self):
        self.app.tree.selection_set([self.after['id'], self.before['id']])
        window = self.app.compare_selected_logs()
        self.windows.append(window)
        self.wait(window)
        self.assertIn('success', window.headings['before'].cget('text'))
        self.assertIn('failed', window.headings['after'].cget('text'))
        self.app.compare_selected()
        other = self.app.open_log_diff()
        self.windows.append(other)
        self.wait(other)

    def test_unverified_log_fails_without_showing_changed_contents(self):
        self.store.evidence(self.after, 'build.log').write_text('tampered secret')
        window = LogDiffWindow(self.root, self.store, self.before, self.after)
        self.windows.append(window)
        deadline = time.monotonic() + 5
        while window.failure is None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertIn('changed outside', window.failure)
        self.assertEqual(window.texts['after'].get('1.0', 'end-1c'), '')

    def test_change_navigation_and_theme_toggle(self):
        window = self.open()
        window.jump_change(1)
        self.assertEqual(window.change_index, 0)
        self.assertTrue(window.texts['after'].tag_ranges('current_change'))
        self.app.theme.toggle()
        self.assertEqual(window.texts['before'].cget('background'), self.app.theme.colors['field'])

    def test_scroll_sync_keyboard_search_and_match_case(self):
        window = self.open()
        window.deiconify()
        window.focus_force()
        self.root.update()
        window.texts['before'].yview_moveto(.6)
        self.root.update()
        self.assertAlmostEqual(window.texts['before'].yview()[0], window.texts['after'].yview()[0], places=4)
        window.query.set('SHARED')
        window.match_case.set(True)
        window.update_search()
        self.assertEqual(window.matches, [])
        window.match_case.set(False)
        window.update_search()
        self.assertEqual(len(window.matches), 2)
        window.focus_search()
        self.root.update()
        window.search_entry.event_generate('<F3>')
        self.root.update()
        self.assertEqual(window.match_index, 0)
        window.search_entry.event_generate('<Shift-F3>')
        self.root.update()
        self.assertEqual(window.match_index, 1)

    def test_destroy_cancels_search_and_theme_callbacks(self):
        window = self.open()
        window.query.set('shared')
        window.destroy()
        self.assertTrue(window.closed)
        self.app.theme.toggle()
        self.root.update()

    def test_single_run_and_trash_selection_cannot_open_comparison(self):
        self.app.show_runs([self.before])
        with patch('buildtrail.gui.messagebox.showerror') as error:
            self.assertIsNone(self.app.open_log_diff())
        error.assert_called_once()
        self.store.trash([self.before['id'], self.after['id']])
        self.app.show_trash.set(True)
        self.app.refresh_history()
        self.app.tree.selection_set(self.app.tree.get_children())
        with patch('buildtrail.gui.messagebox.showinfo') as info:
            self.assertIsNone(self.app.compare_selected_logs())
        info.assert_called_once()

    def test_export_preserves_settings_and_revalidates_evidence(self):
        window = self.open()
        window.query.set('a+b')
        window.match_case.set(True)
        window.issue_kind.set('Errors')
        window.issue_side.set('After')
        self.app.theme.mode.set('light')
        target = self.base / 'logs.html'
        with patch('buildtrail.logview.filedialog.asksaveasfilename', return_value=str(target)), patch('buildtrail.logview.messagebox.showinfo') as info:
            window.export_html()
        info.assert_called_once()
        html = target.read_text(encoding='utf-8')
        self.assertIn('value="a+b"', html)
        self.assertIn('value="error" selected', html)
        self.assertIn('value="after" selected', html)
        self.assertIn('<body class="light">', html)
        self.assertIn('type="checkbox" checked', html)
        original = target.read_bytes()
        self.store.evidence(self.after, 'build.log').write_bytes(b'tampered')
        with patch('buildtrail.logview.filedialog.asksaveasfilename', return_value=str(target)), patch('buildtrail.logview.messagebox.showerror') as error:
            window.export_html()
        error.assert_called_once()
        self.assertEqual(target.read_bytes(), original)

    def test_cli_log_html_and_managed_paths(self):
        import subprocess
        from buildtrail.logreport import export_log_report
        from buildtrail._apptrail.history import history_directory
        target = self.base / 'cli-logs.html'
        result = subprocess.run([sys.executable, '-m', 'buildtrail', '--data-dir', str(self.store.directory), 'logs', self.before['id'], self.after['id'], '--collapse', '-o', str(target)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('error C2143', target.read_text(encoding='utf-8'))
        self.assertIn('id="collapse" type="checkbox" checked', target.read_text(encoding='utf-8'))
        for protected in (self.store.directory / 'report.html', history_directory() / 'report.html'):
            with self.assertRaisesRegex(ValueError, 'outside managed'):
                export_log_report(self.store, self.before, self.after, protected)
        self.store.trash([self.after['id']])
        with self.assertRaisesRegex(ValueError, 'Trash'):
            export_log_report(self.store, self.before, self.after, target)

    def test_fold_sections_search_reveal_and_preserve_coordinates(self):
        window = self.open()
        original = self.store.evidence(self.before, 'build.log').read_bytes()
        self.assertTrue(window.folds)
        window.set_all_folds(False)
        start, end = window.folds[-1]
        for text in window.texts.values():
            self.assertEqual(int(text.tag_cget(f'fold_{start}', 'elide')), 1)
        window.query.set('context line 100')
        window.update_search()
        self.assertEqual(len(window.matches), 2)
        window.jump_match(1)
        match = window.matches[window.match_index]
        self.assertEqual(window.texts[match['side']].get(*window.match_range(match)), 'context line 100')
        self.assertIn(start, window.expanded)
        for text in window.texts.values():
            self.assertEqual(int(text.tag_cget(f'fold_{start}', 'elide')), 0)
        target = self.base / 'expanded-section.html'
        with patch('buildtrail.logview.filedialog.asksaveasfilename', return_value=str(target)), patch('buildtrail.logview.messagebox.showinfo'):
            window.export_html()
        html = target.read_text(encoding='utf-8')
        self.assertIn(f'data-fold="{start}" data-start="{start}" data-end="{end}" data-expanded="true"', html)
        window.toggle_fold(start)
        self.assertNotIn(start, window.expanded)
        window.set_all_folds(True)
        self.assertEqual(window.expanded, {first for first, _ in window.folds})
        window.collapse.set(False)
        window.apply_folds()
        for text in window.texts.values():
            self.assertEqual(int(text.tag_cget(f'marker_{start}', 'elide')), 1)
        self.assertEqual(self.store.evidence(self.before, 'build.log').read_bytes(), original)

    def test_folded_unchanged_diagnostic_reveals_both_sides_and_exports(self):
        window = self.open()
        # The shared warning near the beginning can be folded in a long equal prefix.
        from buildtrail.logdiff import align_logs
        lines = ['header'] + ['warning: shared'] + [f'same {i}' for i in range(20)]
        window.model = align_logs(lines + ['old'], lines + ['new'])
        for text in window.texts.values():
            text.configure(state='normal'); text.delete('1.0', 'end'); text.configure(state='disabled')
        window.render()
        window.filter_issues()
        window.set_all_folds(False)
        start = window.folds[0][0]
        window.jump_issue(1)
        self.assertIn(start, window.expanded)
        self.assertIn('Before line 2', window.issue_status.get())
        self.assertTrue(window.texts['before'].tag_ranges('current_issue'))
        target = self.base / 'folded.html'
        with patch('buildtrail.logview.filedialog.asksaveasfilename', return_value=str(target)), patch('buildtrail.logview.messagebox.showinfo'):
            window.export_html()
        self.assertIn('id="collapse" type="checkbox" checked', target.read_text(encoding='utf-8'))

    def test_context_persists_rebuilds_and_preserves_other_preferences(self):
        import json
        pref = self.store.directory / 'preferences.json'
        pref.write_text(json.dumps({'theme': 'light', 'context_lines': 42, 'unrelated': True}))
        window = self.open()
        window.set_all_folds(False)
        old_folds = list(window.folds)
        window.context.set('8')
        self.assertTrue(window.change_context())
        self.assertNotEqual(window.folds, old_folds)
        saved = json.loads(pref.read_text())
        self.assertEqual(saved, {'theme': 'light', 'context_lines': 42, 'unrelated': True, 'log_context_lines': 8})
        window.query.set('context line 100'); window.update_search(); window.jump_match(1)
        match = window.matches[window.match_index]
        self.assertEqual(window.texts[match['side']].get(*window.match_range(match)), 'context line 100')
        # A new comparison reconstructs the preference from disk.
        other = self.open()
        self.assertEqual(other.context_value, 8)
        self.assertEqual(other.context.get(), '8')
        self.assertEqual(other.folds, window.folds)
        other.context.set('0'); self.assertTrue(other.change_context())
        self.assertEqual(other.context_value, 0)
        other.context.set('100'); self.assertTrue(other.change_context())
        self.assertEqual(other.context_value, 100)

    def test_context_invalid_input_and_save_failure(self):
        window = self.open()
        for value in ('-1', '101', '1.5', '', 'bad'):
            window.context.set(value)
            with patch('buildtrail.logview.messagebox.showerror') as error:
                self.assertFalse(window.change_context())
            error.assert_called_once()
            self.assertEqual(window.context_value, 3)
            self.assertEqual(window.context.get(), '3')
        window.context.set('9')
        with patch('buildtrail.logview.write_preference', side_effect=OSError('disk full')), patch('buildtrail.logview.messagebox.showwarning') as warning:
            self.assertTrue(window.change_context())
        warning.assert_called_once()
        self.assertEqual(window.context_value, 9)

    def test_context_export_and_cli_saved_preference_override(self):
        import subprocess
        window = self.open()
        window.context.set('0')
        window.set_all_folds(False)
        target = self.base / 'context.html'
        with patch('buildtrail.logview.filedialog.asksaveasfilename', return_value=str(target)), patch('buildtrail.logview.messagebox.showinfo'):
            window.export_html()
        self.assertEqual(window.context_value, 0)  # Export commits a typed value.
        self.assertIn('Collapse unchanged lines (0 context lines)', target.read_text(encoding='utf-8'))
        args = [sys.executable, '-m', 'buildtrail', '--data-dir', str(self.store.directory), 'logs', self.before['id'], self.after['id'], '--collapse', '-o', str(target)]
        result = subprocess.run(args, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('(0 context lines)', target.read_text(encoding='utf-8'))
        result = subprocess.run(args + ['--context-lines', '7'], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('(7 context lines)', target.read_text(encoding='utf-8'))
        from buildtrail.logsettings import read_context
        self.assertEqual(read_context(window.preferences_path), 0)  # CLI override is transient.
