import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from buildtrail.gui import BuildTrail
from buildtrail.core import BuildStore, default_recipe
from buildtrail._apptrail.history import SnapshotHistory
from buildtrail._apptrail.checkpoints import capture_checkpoint


@unittest.skipUnless(os.name == 'nt', 'Windows Tk integration')
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'project'
        self.project.mkdir()
        (self.project / 'source.txt').write_text('before')
        self.store = BuildStore(self.base / 'BuildTrail')
        self.history = SnapshotHistory(self.base / 'AppTrail')
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = BuildTrail(self.root, self.store, self.history)
        recipe = default_recipe(self.project)
        recipe.update(shell='argv', argv=[sys.executable, '-c', 'print("hello build")'], tools=[], output_patterns=['dist/**/*'])
        self.app.apply_recipe(recipe)

    def tearDown(self):
        if self.app.busy:
            self.app.stop.set()
            self.finish()
        self.app.close()
        self.temp.cleanup()

    def finish(self):
        deadline = time.monotonic() + 10
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.app.busy)

    def test_save_load_and_run_recipe_then_review(self):
        self.app.save_recipe()
        self.app.name.set('changed')
        self.app.recipe_choice.current(0)
        self.app.load_recipe()
        self.assertEqual(self.app.name.get(), 'Release build')
        self.app.run()
        self.finish()
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertEqual(self.app.review_runs[0]['status'], 'success')
        self.assertIn('hello build', self.app.review.get('1.0', 'end'))
        self.assertEqual(self.app.tabs.select(), str(self.app.review_tab))
        with patch('buildtrail.gui.webbrowser.open') as browser:
            self.app.open_review()
            browser.assert_called_once()
        self.assertTrue((self.store.directory / 'review.html').exists())

    def test_checkpoint_selection_and_source_comparison(self):
        identity, _ = capture_checkpoint(self.history, self.project, 'Before feature', ['dist/'])
        self.app.refresh_checkpoints(identity)
        self.assertEqual(self.app.get_recipe()['checkpoint_id'], identity)
        (self.project / 'source.txt').write_text('after')
        self.app.run()
        self.finish()
        with patch('buildtrail.gui.webbrowser.open') as browser:
            self.app.open_source()
            browser.assert_called_once()
        self.assertIn('Modified', (self.store.directory / 'source-review.html').read_text(encoding='utf-8'))

    def test_compare_success_and_failure(self):
        self.app.run()
        self.finish()
        self.app.command.delete('1.0', 'end')
        import json
        self.app.command.insert('1.0', json.dumps([sys.executable, '-c', 'import sys; sys.exit(4)']))
        self.app.run()
        self.finish()
        self.app.tree.selection_set(self.app.tree.get_children())
        self.app.compare_selected()
        self.assertEqual([r['status'] for r in self.app.review_runs], ['success', 'failed'])
        self.assertIn('Output differences between builds', self.app.review.get('1.0', 'end'))

    def test_stop_command_saves_cancelled_run(self):
        import json
        self.app.command.delete('1.0', 'end')
        self.app.command.insert('1.0', json.dumps([sys.executable, '-c', 'import time; print("started",flush=True); time.sleep(30)']))
        self.app.run()
        self.root.after(200, self.app.stop.set)
        self.finish()
        self.assertEqual(self.app.review_runs[0]['status'], 'cancelled')

    def test_bad_recipe_is_reported_without_launch(self):
        self.app.timeout.set('bad timeout')
        with patch('buildtrail.gui.messagebox.showerror') as error:
            self.app.run()
        error.assert_called_once()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.store.entries(), [])

    def test_live_log_view_is_bounded(self):
        self.app.append_review('x' * 300000)
        self.assertLessEqual(len(self.app.review.get('1.0', 'end-1c')), 200000)
