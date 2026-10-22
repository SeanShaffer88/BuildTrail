"""Bounded alignment, literal search, and heuristic diagnostics for saved logs."""
import difflib
import hashlib
import re

from ._apptrail.core import is_link

MAX_CAPTURE_BYTES = 10 * 1024 * 1024
MAX_VIEW_CHARS = 1000000
MAX_VIEW_LINES = 10000
MAX_LINE_CHARS = 20000
MAX_ALIGNMENT_PRODUCT = 2000000
MAX_MATCHES = 2000
ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]')
CONTROLS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
SUMMARY_ITEM = re.compile(r'\d+\s+(?:error|warning)(?:s|\(s\))?', re.I)
DIAGNOSTIC = re.compile(r'\b(fatal\s+error|error|warning)(?:\[[^\]]{1,40}\]|\s+[A-Za-z]{1,12}\d{2,8})?\s*:', re.I)
LOG_LEVEL = re.compile(r'^\s*(?:\[[^\]]{1,60}\]\s*)*(ERROR|WARN(?:ING)?|FATAL)\b(?:\s*[-:]|\s+)', re.I)
BRACKET_LEVEL = re.compile(r'\[(ERROR|WARN(?:ING)?|FATAL)\]', re.I)
PYTHON_ERROR = re.compile(r'^\s*(?:Traceback \(most recent call last\):|(?:[\w.]*Error|[\w.]*Exception):)')


def severity(line):
    if len(line) <= 256:
        without_counts = SUMMARY_ITEM.sub('', line)
        if without_counts != line and not re.sub(r'\band\b|[\s,;.]', '', without_counts, flags=re.I):
            return None
    match = DIAGNOSTIC.search(line) or LOG_LEVEL.search(line) or BRACKET_LEVEL.search(line)
    if match:
        return 'warning' if match.group(1).casefold().startswith('warn') else 'error'
    if PYTHON_ERROR.search(line):
        return 'error'
    return None


def visible_log(raw):
    notices = []
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('utf-8', errors='replace')
        notices.append('UTF-8 replacement decoding is in use for invalid bytes.')
    if len(text) > MAX_VIEW_CHARS:
        text = text[:MAX_VIEW_CHARS]
        notices.append('Showing the first 1,000,000 decoded characters.')
    # Strip display-only terminal controls without changing the stored bytes.
    lines = text.splitlines()
    if len(lines) > MAX_VIEW_LINES:
        lines = lines[:MAX_VIEW_LINES]
        notices.append('Showing the first 10,000 log lines.')
    cleaned = []
    shortened = False
    for line in lines:
        line = CONTROLS.sub('', ANSI.sub('', line))
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS]
            shortened = True
        cleaned.append(line)
    if shortened:
        notices.append('Long lines show their first 20,000 characters.')
    return {'lines': cleaned, 'notices': notices}


def load_log(store, record):
    record = store.load(record['id'])  # Reject Trash, missing runs, and stale metadata.
    expected = record['evidence_hashes'].get('build.log')
    if not expected:
        raise ValueError('This run has no completed, fingerprinted build log to compare.')
    path = store.run_path(record['id']) / 'build.log'
    if is_link(path.stat(follow_symlinks=False)):
        raise ValueError('Refusing to read a linked build log.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_CAPTURE_BYTES + 1)
    if len(raw) > MAX_CAPTURE_BYTES or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('The build log changed outside BuildTrail; saved evidence cannot be verified.')
    log = visible_log(raw)
    if record.get('log_truncated'):
        log['notices'].append('This run reached its 10 MiB capture limit; later output was not saved.')
    return record, log


def align_logs(before, after, cancelled=lambda: False):
    """Exact bounded middle alignment, with explicit positional fallback for large edits."""
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(before), len(after)) - prefix and before[-suffix - 1] == after[-suffix - 1]:
        suffix += 1
    left_end, right_end = len(before) - suffix, len(after) - suffix
    left, right = before[prefix:left_end], after[prefix:right_end]
    fallback = len(left) * len(right) > MAX_ALIGNMENT_PRODUCT
    if fallback:
        opcodes = [('replace', 0, len(left), 0, len(right))]
    else:
        opcodes = difflib.SequenceMatcher(None, left, right, autojunk=True).get_opcodes()
    rows = []
    def add(a, b):
        x = before[a] if a is not None else None
        y = after[b] if b is not None else None
        rows.append({'before': x, 'after': y, 'before_number': a + 1 if a is not None else None,
            'after_number': b + 1 if b is not None else None,
            'kind': 'equal' if x == y else 'add' if a is None else 'remove' if b is None else 'replace'})
    for i in range(prefix):
        add(i, i)
    for _, a, b, c, d in opcodes:
        if cancelled():
            raise InterruptedError('Log comparison cancelled.')
        for offset in range(max(b - a, d - c)):
            add(prefix + a + offset if a + offset < b else None,
                prefix + c + offset if c + offset < d else None)
    for offset in range(suffix):
        add(left_end + offset, right_end + offset)
    changes, diagnostics = [], []
    changing = False
    for index, row in enumerate(rows, 1):
        if row['kind'] != 'equal' and not changing:
            changes.append(index)
        changing = row['kind'] != 'equal'
        for side in ('before', 'after'):
            kind = severity(row[side]) if row[side] is not None else None
            if kind:
                diagnostics.append({'side': side, 'row': index, 'number': row[side + '_number'], 'severity': kind})
    return {'rows': rows, 'changes': changes, 'diagnostics': diagnostics, 'positional_fallback': fallback}


def search_logs(rows, query, match_case=False, limit=MAX_MATCHES):
    if not query:
        return [], False
    needle = query if match_case else query.casefold()
    matches = []
    for index, row in enumerate(rows, 1):
        for side in ('before', 'after'):
            text = row[side]
            if text is None:
                continue
            haystack = text if match_case else text.casefold()
            boundaries = None
            if len(haystack) != len(text):
                boundaries = {0: 0}
                offset = 0
                for original, char in enumerate(text, 1):
                    offset += len(char.casefold())
                    boundaries[offset] = original
            cursor = 0
            while (start := haystack.find(needle, cursor)) >= 0:
                end = start + len(needle)
                if boundaries is not None and (start not in boundaries or end not in boundaries):
                    cursor = start + 1
                    continue
                if len(matches) >= limit:
                    return matches, True
                matches.append({'side': side, 'row': index,
                    'start': boundaries[start] if boundaries is not None else start,
                    'end': boundaries[end] if boundaries is not None else end})
                cursor = end
    return matches, False
