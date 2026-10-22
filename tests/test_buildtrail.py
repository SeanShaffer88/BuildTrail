import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from buildtrail.core import (BuildStore, default_recipe, run_build, validate_recipe,
    output_match, table_changes, source_comparison)
from buildtrail.process import execute
from buildtrail.report import render_report, export_report
from buildtrail._apptrail.history import SnapshotHistory
from buildtrail._apptrail.checkpoints import capture_checkpoint


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='BuildTrail spaces Ω ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'project'
        self.project.mkdir()
        (self.project / 'source.py').write_text('value = 1\n')
        (self.project / 'requirements.txt').write_text('example==1.0\n')
        self.store = BuildStore(self.base / 'build-library')
        self.history = SnapshotHistory(self.base / 'app-library')

    def recipe(self, script):
        recipe = default_recipe(self.project)
        recipe.update(shell='argv', argv=[sys.executable, '-c', script], tools=[[sys.executable, '--version']],
                      output_patterns=['dist/**/*'], timeout_seconds=10)
        return recipe

    def run_build(self, script, **kwargs):
        return run_build(self.recipe(script), self.store, self.history, **kwargs)

    def test_success_then_failure_preserves_stale_outputs_and_source_changes(self):
        old = self.run_build("from pathlib import Path; Path('dist').mkdir(); Path('dist/demo.exe').write_bytes(b'working'); print('build succeeded')")
        self.assertEqual(old['status'], 'success')
        self.assertEqual(old['output_changes'][0]['state'], 'Added')
        self.assertEqual(old['dependencies'][0]['path'], 'requirements.txt')
        (self.project / 'source.py').write_text('value = broken\n')
        (self.project / 'requirements.txt').write_text('example==2.0\n')
        new = self.run_build("import sys; print('compiler error', file=sys.stderr); sys.exit(7)")
        self.assertEqual(new['status'], 'failed')
        self.assertEqual(new['exit_code'], 7)
        self.assertEqual(new['output_changes'][0]['state'], 'Unchanged')
        self.assertIn('compiler error', self.store.evidence(new, 'build.log').read_text())
        self.assertEqual(source_comparison(self.store, old, new)['counts']['Modified'], 2)
        report = render_report(self.store, old, new)
        for text in ('example==1.0', 'example==2.0', 'compiler error', 'AppTrail source changes', 'Unchanged'):
            self.assertIn(text, report)
        self.assertEqual(len(self.history.entries()), 2)
        self.assertEqual(len(self.store.entries()), 2)

    def test_linked_checkpoint_reuses_rules_and_does_not_change_marked_baseline(self):
        identity, _ = capture_checkpoint(self.history, self.project, 'Before feature', ['dist/', '*.tmp'])
        (self.project / 'ignored.tmp').write_text('ignore')
        (self.project / 'source.py').write_text('after\n')
        recipe = self.recipe('print("ok")')
        recipe['checkpoint_id'] = identity
        record = run_build(recipe, self.store, self.history)
        self.assertEqual(record['status'], 'success')
        self.assertEqual(record['recipe']['exclusions'], ['dist/', '*.tmp'])
        self.assertEqual(self.history.get(identity)['baseline'], 1)
        comparison = source_comparison(self.store, record)
        self.assertEqual(comparison['counts']['Modified'], 1)
        self.assertEqual(comparison['counts']['Added'], 0)
        self.history.trash([identity])
        self.history.purge([identity])
        self.assertEqual(source_comparison(self.store, record)['counts']['Modified'], 1)

    def test_missing_compiler_probe_is_evidence_not_build_failure(self):
        recipe = self.recipe('print("ok")')
        recipe['tools'] = [['buildtrail-no-such-tool-1234', '--version']]
        record = run_build(recipe, self.store, self.history)
        self.assertEqual(record['status'], 'success')
        self.assertIn('error', record['tools'][0])

    def test_post_build_dependency_changes_are_recorded(self):
        record = self.run_build("from pathlib import Path; Path('requirements.txt').write_text('changed==2')")
        rows = table_changes(record['dependencies'], record['dependencies_after'])
        self.assertEqual(rows[0]['state'], 'Modified')

    def test_launch_error_is_saved(self):
        recipe = self.recipe('')
        recipe['argv'] = ['buildtrail-missing-executable-1234']
        record = run_build(recipe, self.store, self.history)
        self.assertEqual(record['status'], 'error')
        self.assertTrue(record['warnings'])
        self.assertIsNotNone(record['completed_at'])
        self.assertEqual(self.store.load(record['id'])['status'], 'error')

    def test_precancelled_build_never_launches(self):
        with patch('buildtrail.core.execute', side_effect=AssertionError('Must not execute')):
            record = self.run_build('print("never")', cancelled=lambda: True)
        self.assertEqual(record['status'], 'cancelled')
        self.assertFalse(record['outputs_after_complete'])

    def test_source_and_log_tampering_detected(self):
        record = self.run_build('print("ok")')
        self.store.evidence(record, 'build.log').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'changed outside'):
            self.store.evidence(record, 'build.log')
        self.store.evidence(record, 'source.json').write_text('{}')
        with self.assertRaises(ValueError):
            source_comparison(self.store, record, record)

    def test_unavailable_capture_does_not_claim_removed_outputs(self):
        row = {'path': 'demo.exe', 'sha256': 'abc'}
        self.assertEqual(table_changes([row], [], after_complete=False)[0]['state'], 'Unknown')
        self.assertEqual(table_changes([], [row], before_complete=False)[0]['state'], 'Unknown')

    def test_report_escapes_commands_and_logs(self):
        record = self.run_build("print('<script>alert(1)</script>')")
        html = render_report(self.store, record)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn("default-src 'none'", html)
        with self.assertRaises(ValueError):
            export_report(self.store, record, None, self.store.run_path(record['id']) / 'run.json')

    def test_recipe_persistence_and_validation(self):
        recipe = self.recipe('print("ok")')
        self.store.save_recipe(recipe)
        recipe['timeout_seconds'] = 30
        self.store.save_recipe(recipe)
        self.assertEqual(len(BuildStore(self.store.directory).recipes()), 1)
        self.assertEqual(self.store.recipes()[0]['timeout_seconds'], 30)
        for patterns in (['../outside'], ['/absolute'], ['C:/absolute'], []):
            recipe['output_patterns'] = patterns
            with self.assertRaises(ValueError):
                validate_recipe(recipe)

    def test_output_globs_include_top_level_files(self):
        self.assertTrue(output_match('dist/demo.exe', ['dist/**/*']))
        self.assertTrue(output_match('dist/x/demo.EXE', ['dist/**/*.exe']))
        self.assertFalse(output_match('src/demo.py', ['dist/**/*']))

    def test_history_overlap_and_cross_project_checkpoint_rejected(self):
        recipe = self.recipe('print("never")')
        recipe['project'] = str(self.base)
        with self.assertRaises(ValueError):
            run_build(recipe, self.store, self.history)
        other = self.base / 'other'
        other.mkdir()
        identity, _ = capture_checkpoint(self.history, other, 'Other', [])
        recipe['project'] = str(self.project)
        recipe['checkpoint_id'] = identity
        with self.assertRaises(ValueError):
            run_build(recipe, self.store, self.history)

    def test_checkpoint_exclusions_apply_to_dependency_capture(self):
        recipe = self.recipe('print("ok")')
        recipe['exclusions'].append('requirements.txt')
        record = run_build(recipe, self.store, self.history)
        self.assertEqual(record['dependencies'], [])

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell semantics')
    def test_powershell_exit_codes_and_paths_with_spaces(self):
        recipe = self.recipe('')
        recipe['shell'] = 'powershell'
        recipe['command'] = f"& '{sys.executable.replace(chr(39), chr(39)*2)}' -c 'import sys; print(123); sys.exit(6)'"
        record = run_build(recipe, self.store, self.history)
        self.assertEqual(record['exit_code'], 6)
        self.assertEqual(record['status'], 'failed')
        recipe['command'] = 'throw "intentional build failure"'
        record = run_build(recipe, self.store, self.history)
        self.assertNotEqual(record['exit_code'], 0)

    def test_cli_run_and_report(self):
        recipe = self.base / 'recipe.json'
        recipe.write_text(json.dumps(self.recipe('print("cli ok")')))
        env = dict(os.environ, LOCALAPPDATA=str(self.base / 'local'), XDG_DATA_HOME=str(self.base / 'local'))
        result = subprocess.run([sys.executable, '-m', 'buildtrail', '--data-dir', str(self.store.directory), 'run', str(recipe)],
            capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        record = self.store.entries()[0]
        result = subprocess.run([sys.executable, '-m', 'buildtrail', '--data-dir', str(self.store.directory), 'report', record['id'], '-o', str(self.base / 'report.html')],
            capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cli ok', (self.base / 'report.html').read_text(encoding='utf-8'))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_log_limit_still_drains_and_preserves_exit_code(self):
        result = execute([sys.executable, '-c', "import sys; sys.stdout.write('x'*2000000); sys.exit(9)"],
            self.root, self.root / 'build.log', 10, max_log_bytes=1024)
        self.assertEqual(result['exit_code'], 9)
        self.assertTrue(result['log_truncated'])
        self.assertEqual((self.root / 'build.log').stat().st_size, 1024)

    def test_timeout_kills_owned_child_processes(self):
        child = "import time; from pathlib import Path; time.sleep(1.2); Path('escaped').write_text('bad')"
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); print('spawned',flush=True); time.sleep(30)"
        result = execute([sys.executable, '-c', parent], self.root, self.root / 'build.log', .35)
        self.assertEqual(result['state'], 'timed_out')
        time.sleep(1.3)
        self.assertFalse((self.root / 'escaped').exists())

    def test_normal_parent_exit_stops_background_descendants(self):
        child = "import time; from pathlib import Path; time.sleep(.8); Path('escaped').write_text('bad')"
        parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}]); print('done')"
        result = execute([sys.executable, '-c', parent], self.root, self.root / 'build.log', 5)
        self.assertEqual(result['exit_code'], 0)
        time.sleep(.9)
        self.assertFalse((self.root / 'escaped').exists())

    def test_cancellation_works_even_if_command_closes_stdout(self):
        event = threading.Event()
        timer = threading.Timer(.25, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        result = execute([sys.executable, '-c', 'import os,time; os.close(1); os.close(2); time.sleep(20)'],
            self.root, self.root / 'build.log', 10, event.is_set)
        self.assertEqual(result['state'], 'cancelled')

    def test_log_write_failure_stops_process(self):
        with self.assertRaises(OSError):
            execute([sys.executable, '-c', 'import time; time.sleep(20)'],
                self.root, self.root / 'missing/build.log', 10)
