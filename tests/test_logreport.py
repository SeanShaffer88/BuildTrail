import base64
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

from buildtrail.logreport import render_log_report, export_log_report, SCRIPT


class LogReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.records = {}
        for identity, text in [('a', 'start\nwarning: old\n</script><script>window.injected=true</script>\n'), ('b', 'start\nerror: new\n😀 a+b\n')]:
            folder = self.base / identity
            folder.mkdir()
            raw = text.encode()
            (folder / 'build.log').write_bytes(raw)
            self.records[identity] = {'id': identity, 'status': 'success' if identity == 'a' else 'failed', 'exit_code': 0 if identity == 'a' else 1,
                'recipe': {'name': '<script>name</script>'}, 'started_at': '2026-10-21T00:00:00Z', 'evidence_hashes': {'build.log': hashlib.sha256(raw).hexdigest()}}
        outer = self
        class Store:
            directory = outer.base
            def load(self, identity): return outer.records[identity]
            def run_path(self, identity): return outer.base / identity
        self.store = Store()

    def tearDown(self):
        self.temp.cleanup()

    def test_escaped_content_csp_and_diagnostic_locations(self):
        report = render_log_report(self.store, self.records['a'], self.records['b'], query='"><img src=x onerror=alert(1)>')
        self.assertIn('&lt;/script&gt;&lt;script&gt;', report)
        self.assertIn('data-side="before" data-number="2" data-severity="warning"', report)
        self.assertIn('data-side="after" data-number="2" data-severity="error"', report)
        self.assertIn('value="&quot;&gt;&lt;img', report)
        digest = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
        self.assertIn("script-src 'sha256-" + digest + "'", report)
        class Scripts(HTMLParser):
            scripts = 0
            def handle_starttag(self, tag, attrs):
                if tag == 'script': self.scripts += 1
                self_outer.assertNotIn(tag, ('img', 'iframe', 'object'))
        self_outer = self
        parser = Scripts(); parser.feed(report)
        self.assertEqual(parser.scripts, 1)

    def test_limits_and_empty_logs(self):
        for identity in self.records:
            raw = b'' if identity == 'a' else b'a' * 20001
            (self.base / identity / 'build.log').write_bytes(raw)
            self.records[identity]['evidence_hashes']['build.log'] = hashlib.sha256(raw).hexdigest()
        self.records['b']['log_truncated'] = True
        report = render_log_report(self.store, *self.records.values())
        self.assertIn('0 before / 1 after', report)
        self.assertIn('first 20,000', report)
        self.assertIn('capture limit', report)

    def test_failed_verification_preserves_existing_export(self):
        target = self.base.parent / (self.base.name + '.html')
        try:
            target.write_text('previous report')
            (self.base / 'b/build.log').write_bytes(b'modified')
            with self.assertRaisesRegex(ValueError, 'changed outside'):
                export_log_report(self.store, *self.records.values(), target)
            self.assertEqual(target.read_text(), 'previous report')
        finally:
            target.unlink(missing_ok=True)

    def test_context_validation_corrupt_preferences_and_report_ranges(self):
        import json
        from buildtrail.logsettings import read_context
        from buildtrail.logdiff import align_logs, unchanged_sections
        pref = self.base / 'preferences.json'
        for raw in ('broken', '[]', 'null', '{"log_context_lines": true}', '{"log_context_lines": 101}', '{"log_context_lines": "5"}'):
            pref.write_text(raw)
            self.assertEqual(read_context(pref), 3)
        pref.write_text(json.dumps({'log_context_lines': 0}))
        for identity in self.records:
            raw = ('\n'.join(['same'] * 20 + [identity])).encode()
            (self.base / identity / 'build.log').write_bytes(raw)
            self.records[identity]['evidence_hashes']['build.log'] = hashlib.sha256(raw).hexdigest()
        report = render_log_report(self.store, *self.records.values(), collapse=True)
        self.assertIn('(0 context lines)', report)
        self.assertIn('data-start="1" data-end="20"', report)
        report = render_log_report(self.store, *self.records.values(), context_lines=5)
        self.assertIn('data-start="1" data-end="15"', report)
        report = render_log_report(self.store, *self.records.values(), context_lines=100)
        self.assertNotIn('class="fold-marker"', report)
        for invalid in (-1, 101, True, 1.5, '3'):
            with self.assertRaises(ValueError):
                render_log_report(self.store, *self.records.values(), context_lines=invalid)
        rows = align_logs(['a'] * 5 + ['old'], ['a'] * 5 + ['new'])['rows']
        self.assertEqual(unchanged_sections(rows, 0), [(1, 5)])
        self.assertEqual(unchanged_sections(rows, 100), [])

    def test_log_preference_validation_and_independent_defaults(self):
        import json
        from buildtrail.logsettings import read_options, save_options, LOG_DEFAULTS
        path = self.base / 'preferences.json'
        for value in (None, [], True, 'bad'):
            path.write_text(json.dumps({'log_diff_options': value}))
            self.assertEqual(read_options(path), LOG_DEFAULTS)
        path.write_text(json.dumps({'theme': 'light', 'log_context_lines': 9, 'log_diff_options': {'collapse': 'false', 'match_case': True, 'issue_kind': ['Errors'], 'issue_side': 'After'}}))
        self.assertEqual(read_options(path), {'collapse': False, 'match_case': True, 'ignore_whitespace': False, 'issue_kind': 'Errors & warnings', 'issue_side': 'After'})
        save_options(path, collapse=True)
        saved = json.loads(path.read_text())
        self.assertEqual(saved['theme'], 'light'); self.assertEqual(saved['log_context_lines'], 9)
        for kwargs in ({'ignore_whitespace': 'true'}, {'collapse': 1}, {'match_case': 'true'}, {'issue_kind': 'bad'}, {'issue_side': False}):
            with self.assertRaises(ValueError):
                save_options(path, **kwargs)
        report = render_log_report(self.store, *self.records.values())
        self.assertIn('id="collapse" type="checkbox" checked', report)
        self.assertIn('id="case" type="checkbox" checked', report)
        self.assertIn('value="after" selected', report)
