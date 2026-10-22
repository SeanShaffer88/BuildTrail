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
