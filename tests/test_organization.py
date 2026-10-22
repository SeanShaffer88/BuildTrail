import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from buildtrail.core import BuildStore, default_recipe, run_build
from buildtrail.organization import run_name
from buildtrail.report import render_report
from buildtrail._apptrail.history import SnapshotHistory


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'project'
        self.project.mkdir()
        (self.project / 'source.py').write_text('value = 1\n')
        self.store = BuildStore(self.base / 'BuildTrail')
        self.history = SnapshotHistory(self.base / 'AppTrail')
        recipe = default_recipe(self.project)
        recipe.update(shell='argv', argv=[sys.executable, '-c', 'print("original evidence")'], tools=[])
        self.record = run_build(recipe, self.store, self.history)
        self.identity = self.record['id']
        self.original = {p.name: p.read_bytes() for p in self.store.run_path(self.identity).iterdir()}

    def duplicate(self):
        identity = uuid.uuid4().hex
        folder = self.store.run_path(identity)
        folder.mkdir()
        for name, content in self.original.items():
            (folder / name).write_bytes(content)
        record = json.loads(self.original['run.json'])
        record['id'] = identity
        self.store.save(record)
        return identity

    def test_names_notes_survive_restart_without_changing_evidence(self):
        self.store.update_metadata(self.identity, '  Release candidate 1  ', 'Tests passed\nIssue #42')
        store = BuildStore(self.store.directory)
        record = store.load(self.identity)
        self.assertEqual(run_name(record), 'Release candidate 1')
        self.assertEqual(record['organization']['notes'], 'Tests passed\nIssue #42')
        self.assertEqual(record['recipe']['name'], 'Release build')
        for name, original in self.original.items():
            self.assertEqual((store.run_path(self.identity) / name).read_bytes(), original)

    def test_legacy_run_falls_back_to_recipe_name(self):
        record = self.store.load(self.identity)
        self.assertEqual(run_name(record), 'Release build')
        self.assertEqual(record['organization']['notes'], '')

    def test_search_names_notes_and_original_recipe(self):
        self.store.update_metadata(self.identity, 'Golden build', 'Fixes PR-1234')
        for query in ('golden', 'pr-1234', 'Release build', str(self.project)):
            self.assertEqual(len(self.store.entries(query)), 1)
        self.assertEqual(self.store.entries('does not match'), [])

    def test_names_notes_in_html_are_escaped(self):
        self.store.update_metadata(self.identity, 'RC <script>', '<img src=x>\nPassed tests')
        html = render_report(self.store, self.store.load(self.identity))
        self.assertIn('RC &lt;script&gt;', html)
        self.assertIn('&lt;img src=x&gt;', html)
        self.assertNotIn('<script>', html)

    def test_metadata_validation(self):
        for name, notes in [('', ''), ('x'*121, ''), ('two\nlines', ''), ('good', 'x'*10001), ('good', '\0')]:
            with self.assertRaises(ValueError):
                self.store.update_metadata(self.identity, name, notes)
        self.assertEqual(run_name(self.store.load(self.identity)), 'Release build')

    def test_trash_restore_preserve_files_and_notes(self):
        self.store.update_metadata(self.identity, 'Known good', 'Keep this evidence')
        self.store.trash([self.identity])
        self.assertEqual(self.store.entries(), [])
        self.assertEqual(len(self.store.entries(deleted=True)), 1)
        with self.assertRaisesRegex(ValueError, 'Trash'):
            self.store.load(self.identity)
        store = BuildStore(self.store.directory)
        store.restore([self.identity])
        record = store.load(self.identity)
        self.assertEqual(run_name(record), 'Known good')
        self.assertEqual(record['organization']['notes'], 'Keep this evidence')
        for name, original in self.original.items():
            self.assertEqual((store.run_path(self.identity) / name).read_bytes(), original)

    def test_permanent_delete_only_owned_run_and_metadata(self):
        self.store.save_recipe(self.record['recipe'])
        recipe_bytes = (self.store.directory / 'recipes.json').read_bytes()
        export = self.base / 'shared-report.html'
        export.write_text('Previously exported report')
        other = self.duplicate()
        self.store.update_metadata(self.identity, 'Old build', 'discard')
        self.store.trash([self.identity])
        self.store.purge([self.identity])
        self.assertFalse(self.store.run_path(self.identity).exists())
        self.assertEqual(self.store.organization(self.identity)['notes'], '')
        self.assertTrue(self.store.run_path(other).exists())
        self.assertEqual((self.project / 'source.py').read_text(), 'value = 1\n')
        self.assertEqual((self.store.directory / 'recipes.json').read_bytes(), recipe_bytes)
        self.assertEqual(export.read_text(), 'Previously exported report')
        self.assertEqual(self.history.load(self.record['source_id'])['root'], str(self.project))

    def test_active_run_cannot_be_purged(self):
        with self.assertRaisesRegex(ValueError, 'Trash'):
            self.store.purge([self.identity])
        self.assertTrue(self.store.run_path(self.identity).exists())

    def test_locked_run_cannot_be_organized_in_other_store(self):
        other = BuildStore(self.store.directory)
        with self.store.run_lock(self.identity):
            for action in (lambda: other.trash([self.identity]),
                           lambda: other.update_metadata(self.identity, 'new', 'notes')):
                with self.assertRaisesRegex(ValueError, 'still active'):
                    action()
        self.store.trash([self.identity])

    def test_running_capture_holds_lock(self):
        observed = []
        def progress(message):
            if 'Running build command' in message:
                active = next(r for r in self.store.entries() if r['status'] == 'unfinished')
                with self.assertRaisesRegex(ValueError, 'still active'):
                    BuildStore(self.store.directory).trash([active['id']])
                observed.append(active['id'])
        record = run_build(self.record['recipe'], self.store, self.history, output=progress)
        self.assertEqual(record['status'], 'success')
        self.assertEqual(observed, [record['id']])

    def test_separate_process_cannot_trash_locked_run(self):
        script = '''import sys
from buildtrail.core import BuildStore
from buildtrail.organization import RunBusyError
try:
    BuildStore(sys.argv[1]).trash([sys.argv[2]])
except RunBusyError:
    print('protected')
else:
    raise SystemExit('Run was not protected')
'''
        with self.store.run_lock(self.identity):
            result = subprocess.run([sys.executable, '-c', script, str(self.store.directory), self.identity],
                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'protected')
        self.assertEqual(len(self.store.entries()), 1)

    def test_catalog_commit_failure_restores_files_and_trash_rows(self):
        self.store.trash([self.identity])
        original = self.store.connect_organization
        @contextmanager
        def failing_commit():
            with original() as db:
                yield db
                if db.total_changes:
                    raise sqlite3.OperationalError('injected catalog commit failure')
        with patch.object(self.store, 'connect_organization', failing_commit):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.purge([self.identity])
        self.assertTrue(self.store.run_path(self.identity).exists())
        self.assertEqual(len(self.store.entries(deleted=True)), 1)
        self.assertEqual(list(self.store.pending_delete.iterdir()), [])

    def test_batch_trash_validates_every_run_before_changing_any(self):
        with self.assertRaises(FileNotFoundError):
            self.store.trash([self.identity, 'f'*32])
        self.assertEqual(len(self.store.entries()), 1)

    def test_batch_purge_rejects_an_active_selection(self):
        other = self.duplicate()
        self.store.trash([self.identity])
        with self.assertRaises(ValueError):
            self.store.purge([self.identity, other])
        self.assertTrue(self.store.run_path(self.identity).exists())
        self.assertTrue(self.store.run_path(other).exists())

    def test_rename_failure_rolls_back_batch(self):
        other = self.duplicate()
        self.store.trash([self.identity, other])
        original = Path.replace
        calls = []
        def replace(path, target):
            if path.parent == self.store.runs:
                calls.append(path)
                if len(calls) == 2:
                    raise OSError('injected move failure')
            return original(path, target)
        with patch.object(Path, 'replace', replace):
            with self.assertRaises(OSError):
                self.store.purge([self.identity, other])
        self.assertTrue(self.store.run_path(self.identity).exists())
        self.assertTrue(self.store.run_path(other).exists())
        self.assertEqual(len(self.store.entries(deleted=True)), 2)
        self.assertEqual(list(self.store.pending_delete.iterdir()), [])

    def test_cleanup_failure_retried_on_startup(self):
        self.store.trash([self.identity])
        with patch('buildtrail.organization.shutil.rmtree', side_effect=OSError('injected cleanup failure')):
            with self.assertRaises(OSError):
                self.store.purge([self.identity])
        self.assertEqual(len(list(self.store.pending_delete.iterdir())), 1)
        reopened = BuildStore(self.store.directory)
        self.assertEqual(list(reopened.pending_delete.iterdir()), [])
        self.assertEqual(reopened.entries(deleted=True), [])

    def test_interrupted_deletion_restores_uncommitted_move(self):
        self.store.trash([self.identity])
        staged = self.store.pending_delete / (self.identity + '.' + uuid.uuid4().hex)
        self.store.run_path(self.identity).replace(staged)
        reopened = BuildStore(self.store.directory)
        self.assertTrue(reopened.run_path(self.identity).exists())
        self.assertEqual(len(reopened.entries(deleted=True)), 1)
        self.assertFalse(staged.exists())

    def test_invalid_ids_never_leave_library(self):
        for identity in ('../other', '', 'g'*32, 'a'*31):
            with self.assertRaises(ValueError):
                self.store.purge([identity])

    def test_linked_run_directory_refused(self):
        external = self.base / 'external'
        external.mkdir()
        sentinel = external / 'keep.txt'
        sentinel.write_text('keep')
        identity = uuid.uuid4().hex
        target = self.store.runs / identity
        if os.name == 'nt':
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(target), str(external)], capture_output=True)
            if result.returncode:
                self.skipTest('Junction creation unavailable')
            self.addCleanup(lambda: os.rmdir(target) if target.exists() else None)
        else:
            target.symlink_to(external, target_is_directory=True)
            self.addCleanup(lambda: target.unlink())
        with self.assertRaises(ValueError):
            self.store.run_path(identity)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_junction_inside_run_cannot_delete_external_files(self):
        if os.name != 'nt':
            self.skipTest('Windows junction integration')
        external = self.base / 'outside'
        external.mkdir()
        (external / 'keep.txt').write_text('untouched')
        junction = self.store.run_path(self.identity) / 'linked'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(external)], capture_output=True)
        if result.returncode:
            self.skipTest('Junction creation unavailable')
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        self.store.trash([self.identity])
        with self.assertRaisesRegex(ValueError, 'links or reparse'):
            self.store.purge([self.identity])
        self.assertEqual((external / 'keep.txt').read_text(), 'untouched')
