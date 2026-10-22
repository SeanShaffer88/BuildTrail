import hashlib
from pathlib import Path
import tempfile
import time
import unittest

from buildtrail.logdiff import (align_logs, severity, visible_log, search_logs, load_log,
    MAX_VIEW_LINES, MAX_LINE_CHARS, MAX_VIEW_CHARS)


class LogModelTests(unittest.TestCase):
    def test_insert_remove_replace_preserve_real_line_numbers(self):
        model = align_logs(['start', 'old', 'end'], ['start', 'new', 'extra', 'end'])
        self.assertEqual([r['before_number'] for r in model['rows']], [1, 2, None, 3])
        self.assertEqual([r['after_number'] for r in model['rows']], [1, 2, 3, 4])
        self.assertEqual(model['changes'], [2])
        self.assertEqual(model['rows'][1]['kind'], 'replace')
        self.assertEqual(model['rows'][2]['kind'], 'add')
        reverse = align_logs(['start', 'extra', 'end'], ['start', 'end'])
        self.assertEqual(reverse['rows'][1]['kind'], 'remove')
        self.assertIsNone(reverse['rows'][1]['after_number'])

    def test_empty_identical_and_no_final_newline_logs(self):
        self.assertEqual(align_logs([], [])['rows'], [])
        self.assertEqual(align_logs(['x'], ['x'])['changes'], [])
        self.assertEqual(visible_log(b'first\r\nlast')['lines'], ['first', 'last'])
        self.assertEqual(align_logs([], ['first'])['rows'][0]['before'], None)

    def test_compiler_and_runtime_diagnostic_formats(self):
        errors = ['foo.cpp(10): fatal error C1083: cannot open file', 'main.cs(2): error CS0103: unknown name',
                  'error[E0425]: missing value', 'main.c:4:2: error: expected expression',
                  '[12:30] [ERROR] compiler failed', 'ERROR Build failed',
                  'Traceback (most recent call last):', 'ValueError: invalid value']
        warnings = ['foo.cpp(8): warning C4996: deprecated', 'warning: unused variable',
                    '[WARNING] deprecated API', 'WARN build input was ignored']
        for line in errors:
            self.assertEqual(severity(line), 'error', line)
        for line in warnings:
            self.assertEqual(severity(line), 'warning', line)
        for line in ['0 Error(s)', '0 Warning(s)', '0 errors, 0 warnings', '1 warning and 2 errors',
                     'build succeeded', 'no errors were reported', 'error_count=0']:
            self.assertIsNone(severity(line), line)

    def test_issue_locations_include_both_sides_and_original_line_numbers(self):
        model = align_logs(['start', 'warning: old'], ['start', 'extra', 'error: new'])
        self.assertEqual([(d['side'], d['number'], d['severity']) for d in model['diagnostics']],
                         [('before', 2, 'warning'), ('after', 3, 'error')])

    def test_ansi_controls_stripped_without_changing_source_line_numbers(self):
        raw = b'begin\r\n\x1b[31merror: bad\x1b[0m\r\n\x1b]0;title\x07warning: next\x00\n'
        self.assertEqual(visible_log(raw)['lines'], ['begin', 'error: bad', 'warning: next'])
        self.assertEqual(align_logs([], visible_log(raw)['lines'])['diagnostics'][1]['number'], 3)

    def test_invalid_utf8_has_explicit_notice(self):
        log = visible_log(b'value=\xff\n')
        self.assertIn('\ufffd', log['lines'][0])
        self.assertTrue(any('replacement' in n for n in log['notices']))

    def test_character_line_and_long_line_limits_reported(self):
        log = visible_log(('x' * (MAX_VIEW_CHARS + 1)).encode())
        self.assertLessEqual(len(log['lines'][0]), MAX_LINE_CHARS)
        self.assertTrue(any('1,000,000' in n for n in log['notices']))
        self.assertTrue(any('20,000' in n for n in log['notices']))
        log = visible_log(('line\n' * (MAX_VIEW_LINES + 1)).encode())
        self.assertEqual(len(log['lines']), MAX_VIEW_LINES)
        self.assertTrue(any('10,000' in n for n in log['notices']))

    def test_large_middle_fallback_is_bounded_and_announced(self):
        start = time.monotonic()
        before = ['header'] + [f'old {i}' for i in range(3000)] + ['footer']
        after = ['header'] + [f'new {i}' for i in range(3000)] + ['footer']
        model = align_logs(before, after)
        self.assertTrue(model['positional_fallback'])
        self.assertEqual(model['rows'][0]['kind'], 'equal')
        self.assertEqual(model['rows'][-1]['kind'], 'equal')
        self.assertLess(time.monotonic() - start, 5)

    def test_search_literal_case_unicode_and_original_offsets(self):
        model = align_logs(['😀 a+b ERROR', 'unchanged'], ['😀 a+b error', 'unchanged'])
        matches, capped = search_logs(model['rows'], 'a+b')
        self.assertFalse(capped)
        self.assertEqual([m['side'] for m in matches], ['before', 'after'])
        for match in matches:
            line = model['rows'][match['row'] - 1][match['side']]
            self.assertEqual(line[match['start']:match['end']], 'a+b')
        self.assertEqual(len(search_logs(model['rows'], 'ERROR')[0]), 2)
        self.assertEqual(len(search_logs(model['rows'], 'ERROR', match_case=True)[0]), 1)
        self.assertEqual(search_logs(model['rows'], '│'), ([], False))
        self.assertEqual(search_logs(model['rows'], ''), ([], False))

    def test_match_cap_is_explicit(self):
        model = align_logs(['a a a'], ['a a a'])
        matches, capped = search_logs(model['rows'], 'a', limit=2)
        self.assertEqual(len(matches), 2)
        self.assertTrue(capped)
        self.assertFalse(search_logs(model['rows'], 'a', limit=6)[1])

    def test_casefold_expansion_preserves_character_boundaries(self):
        rows = align_logs(['Straße'], [])['rows']
        match = search_logs(rows, 'STRASSE')[0][0]
        self.assertEqual(rows[0]['before'][match['start']:match['end']], 'Straße')
        self.assertEqual(len(search_logs(rows, 's')[0]), 1)  # Initial S, not half of ß.

    def test_adversarial_repetitions_remain_bounded(self):
        start = time.monotonic()
        rows = align_logs(['a' * 20000] * 40, [])['rows']
        self.assertEqual(search_logs(rows, 'a' * 1000 + 'b'), ([], False))
        self.assertIsNone(severity('0 errors' + ' ' * 20000 + 'x'))
        self.assertLess(time.monotonic() - start, 3)

    def test_cancel_alignment(self):
        with self.assertRaises(InterruptedError):
            align_logs(['before'], ['after'], cancelled=lambda: True)

    def test_verifies_bytes_used_for_view_and_exposes_capture_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'build.log'
            raw = b'error: saved bytes\n'
            path.write_bytes(raw)
            record = {'id': 'a' * 32, 'evidence_hashes': {'build.log': hashlib.sha256(raw).hexdigest()}, 'log_truncated': True}
            class Store:
                def load(self, identity): return record
                def run_path(self, identity): return path.parent
            _, log = load_log(Store(), record)
            self.assertEqual(log['lines'], ['error: saved bytes'])
            self.assertTrue(any('10 MiB' in n for n in log['notices']))
            path.write_bytes(b'changed externally')
            with self.assertRaisesRegex(ValueError, 'changed outside'):
                load_log(Store(), record)
            record['evidence_hashes'] = {}
            with self.assertRaisesRegex(ValueError, 'no completed'):
                load_log(Store(), record)

    def test_unchanged_sections_keep_context_and_handle_edges(self):
        from buildtrail.logdiff import unchanged_sections
        def rows(kinds): return [{'kind': kind} for kind in kinds]
        self.assertEqual(unchanged_sections([]), [])
        self.assertEqual(unchanged_sections(rows(['equal'] * 6)), [])
        self.assertEqual(unchanged_sections(rows(['equal'] * 10)), [(4, 7)])
        self.assertEqual(unchanged_sections(rows(['equal'] * 10 + ['replace'] + ['equal'] * 10)), [(1, 7), (15, 21)])
        self.assertEqual(unchanged_sections(rows(['replace'] + ['equal'] * 10 + ['add'])), [(5, 8)])
        self.assertEqual(unchanged_sections(rows(['remove', 'add', 'replace'])), [])
