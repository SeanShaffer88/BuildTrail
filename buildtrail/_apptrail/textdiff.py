"""Bounded source/config text capture and diffs of saved evidence, never live files."""
import difflib
import hashlib
import re
from pathlib import PurePosixPath

MAX_TEXT_BYTES = 256 * 1024
MAX_TOTAL_TEXT_BYTES = 8 * 1024 * 1024
MAX_TEXT_LINES = 3000
POLICY = 'source-config-v1'
ENCODINGS = ('utf-8', 'utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be')
EXTENSIONS = set('''.py .pyw .pyi .js .jsx .mjs .cjs .ts .tsx .json .jsonc .yaml .yml .toml
.ini .cfg .conf .config .xml .cs .csproj .fs .fsproj .vb .vbproj .sln .slnx .props .targets .resx
.c .cpp .cc .cxx .h .hpp .hxx .rs .go .java .kt .kts .sh .ps1 .psm1 .psd1 .bat .cmd .sql
.html .htm .css .scss .less .md .rst .txt .vue .svelte .rb .php .swift .cmake .gradle .proto
.graphql .gql .gitignore .gitattributes .editorconfig .dockerignore'''.split())
FILENAMES = {'makefile', 'dockerfile', 'cmakelists.txt', '.gitignore', '.gitattributes', '.editorconfig', '.dockerignore'}
STATUS_MESSAGES = {
    'legacy': 'This older snapshot did not capture text. Take new snapshots with text capture enabled.',
    'disabled': 'Text capture was disabled for this snapshot.',
    'excluded': 'Text was excluded by the credential/private-directory policy.',
    'not_eligible': 'This file type is outside the source/config text capture policy.',
    'too_large': 'The file exceeded the 256 KiB text capture limit.',
    'line_limit': 'The file exceeded the 3,000-line text capture limit.',
    'budget_exceeded': 'The snapshot reached its 8 MiB total text capture budget.',
    'unsupported': 'The file was binary or not valid UTF-8 / BOM-marked UTF-16 / UTF-32 text.',
    'unstable': 'The file changed during capture; its text was discarded.',
    'read_error': 'The file could not be read reliably.',
    'captured': 'Text captured.',
}


def eligibility(path):
    path = PurePosixPath(path)
    name = path.name.casefold()
    if '.git' in {part.casefold() for part in path.parts} or name.startswith(('.env', 'secrets.', 'credentials.')) or name in {'.npmrc', '.pypirc', '.netrc'}:
        return 'excluded'
    if path.suffix.casefold() not in EXTENSIONS and name not in FILENAMES:
        return 'not_eligible'
    return 'eligible'


def decode_text(raw):
    if raw.startswith(b'\xff\xfe\0\0'):
        encoding = 'utf-32-le'
    elif raw.startswith(b'\0\0\xfe\xff'):
        encoding = 'utf-32-be'
    elif raw.startswith(b'\xff\xfe'):
        encoding = 'utf-16-le'
    elif raw.startswith(b'\xfe\xff'):
        encoding = 'utf-16-be'
    else:
        encoding = 'utf-8'
    try:
        content = raw.decode(encoding, errors='strict')
    except UnicodeError:
        return None, 'unsupported'
    if any(ord(char) < 32 and char not in '\t\r\n\f' for char in content):
        return None, 'unsupported'
    if len(content.splitlines()) > MAX_TEXT_LINES:
        return None, 'line_limit'
    return {'encoding': encoding, 'content': content}, 'captured'


def validate_text(entry):
    payload = entry.get('text')
    status = entry.get('text_status')
    if status is not None and status not in STATUS_MESSAGES:
        raise ValueError('Invalid text capture status.')
    if payload is None:
        if status == 'captured':
            raise ValueError('Captured text is missing.')
        return 0
    if not isinstance(payload, dict) or payload.get('encoding') not in ENCODINGS or not isinstance(payload.get('content'), str):
        raise ValueError('Invalid captured text payload.')
    content = payload['content']
    if len(content) > MAX_TEXT_BYTES:
        raise ValueError('Captured text exceeds the file limit.')
    try:
        raw = content.encode(payload['encoding'], errors='strict')
    except UnicodeError:
        raise ValueError('Invalid captured text encoding.') from None
    if len(raw) > MAX_TEXT_BYTES or len(content.splitlines()) > MAX_TEXT_LINES:
        raise ValueError('Captured text exceeds size or line limits.')
    if status != 'captured' or entry.get('error') or entry.get('size') != len(raw) or hashlib.sha256(raw).hexdigest() != entry.get('sha256'):
        raise ValueError('Captured text does not match the file evidence.')
    decoded, expected_status = decode_text(raw)
    if expected_status != 'captured' or decoded != payload:
        raise ValueError('Captured text is not supported text evidence.')
    return len(raw)


def text_summary(entry):
    if entry is None:
        return 'Absent from this capture'
    payload = entry.get('text')
    if payload is None:
        return STATUS_MESSAGES.get(entry.get('text_status', 'legacy'), STATUS_MESSAGES['legacy'])
    content = payload['content']
    crlf = content.count('\r\n')
    lf = content.count('\n') - crlf
    cr = content.count('\r') - crlf
    endings = ', '.join(f'{name}: {count}' for name, count in (('CRLF', crlf), ('LF', lf), ('CR', cr)) if count) or 'no line endings'
    final = 'final newline' if content.endswith(('\n', '\r')) else 'no final newline'
    bom = ' · BOM' if content.startswith('\ufeff') else ''
    return f'{payload["encoding"]}{bom} · {entry["size"]} bytes · {endings} · {final}'


def build_diff(change):
    before, after = change.get('before'), change.get('after')
    result = {'state': 'unavailable', 'message': '', 'lines': [], 'added': 0, 'removed': 0,
              'before_summary': text_summary(before), 'after_summary': text_summary(after)}
    if before is not None and after is not None and before.get('sha256') and before['sha256'] == after.get('sha256') and not before.get('error') and not after.get('error'):
        result.update(state='unchanged', message='The captured file bytes are identical. There is no content diff.')
        return result
    missing = []
    for side, entry in (('Before', before), ('After', after)):
        if entry is not None and (entry.get('text') is None or entry.get('error') or not entry.get('sha256')):
            reason = STATUS_MESSAGES.get(entry.get('text_status', 'legacy'), STATUS_MESSAGES['legacy'])
            missing.append(f'{side}: {reason}')
    if missing:
        result['message'] = '\n'.join(missing) + '\nDiffs use saved text only; current files are never read to fill missing evidence.'
        return result
    if before is None and after is None:
        result['message'] = 'Neither capture contains this file.'
        return result
    def saved_lines(entry):
        content = entry['text']['content'] if entry is not None else ''
        if content.startswith('\ufeff'):
            content = content[1:]
        return content.splitlines(keepends=True)
    lines = difflib.unified_diff(saved_lines(before), saved_lines(after),
        fromfile='before/' + change['path'] if before is not None else '/dev/null',
        tofile='after/' + change['path'] if after is not None else '/dev/null', n=3)
    for index, line in enumerate(lines):
        kind = ('header' if index < 2 else 'hunk' if line.startswith('@@')
                else 'add' if line.startswith('+') else 'remove' if line.startswith('-') else 'context')
        result['lines'].append({'kind': kind, 'text': line})
        if kind in ('add', 'remove'):
            result['added' if kind == 'add' else 'removed'] += 1
        if kind in ('add', 'remove', 'context') and not line.endswith(('\r', '\n')):
            result['lines'].append({'kind': 'meta', 'text': '\\ No newline at end of file\n'})
    result['state'] = 'available'
    if result['lines']:
        result['message'] = f'{result["added"]} added line(s), {result["removed"]} removed line(s). Unified diff with 3 lines of context.'
    elif before is None or after is None:
        result['message'] = f'An empty text file is present only in the {"After" if before is None else "Before"} capture.'
    else:
        result['message'] = 'Decoded text is identical. The byte change is in encoding or the byte-order mark; see capture details above.'
    if before is None or after is None:
        result['message'] += ' Absence from one capture is not proof of deletion if that capture had read warnings.'
    return result


def diff_text(model, show_endings=False):
    output = []
    for line in model['lines']:
        text = line['text']
        if line['kind'] in ('add', 'remove', 'context') and show_endings:
            ending = 'CRLF' if text.endswith('\r\n') else 'LF' if text.endswith('\n') else 'CR' if text.endswith('\r') else 'no final newline'
            output.append(text.rstrip('\r\n') + f'  ⟦{ending}⟧\n')
        else:
            output.append(text.rstrip('\r\n') + '\n')
    return ''.join(output)


def word_changes(before, after, budget=200000):
    """Find changed words, punctuation, and whitespace within a paired row.

    Keep words intact (including Unicode identifiers). Bound matcher work for
    repetitive/minified input, falling back to the differing middle region.
    Offsets are Python character offsets into the displayed saved line.
    """
    before, after = before.rstrip('\r\n'), after.rstrip('\r\n')
    left = list(re.finditer(r'\w+|\s+|[^\w\s]', before))
    right = list(re.finditer(r'\w+|\s+|[^\w\s]', after))
    cost = len(left) * len(right)
    if cost > budget:
        prefix = 0
        while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
            prefix += 1
        tail = 0
        while tail < min(len(before), len(after)) - prefix and before[-tail - 1] == after[-tail - 1]:
            tail += 1
        return ([(prefix, len(before) - tail)] if prefix < len(before) - tail else [],
                [(prefix, len(after) - tail)] if prefix < len(after) - tail else [], 0)
    removed, added = [], []
    for kind, a, b, c, d in difflib.SequenceMatcher(None, [m.group() for m in left], [m.group() for m in right], autojunk=False).get_opcodes():
        if kind != 'equal':
            if a < b:
                removed.append((left[a].start(), left[b - 1].end()))
            if c < d:
                added.append((right[c].start(), right[d - 1].end()))
    return removed, added, cost


def side_by_side(change, ignore_whitespace=False):
    """Align saved lines, preserving real line numbers and blank spacer rows."""
    if build_diff(change)['state'] != 'available':
        return {'rows': [], 'changes': []}
    def lines(entry):
        content = entry['text']['content'] if entry else ''
        return (content[1:] if content.startswith('\ufeff') else content).splitlines(keepends=True)
    before, after = lines(change.get('before')), lines(change.get('after'))
    ignore_whitespace = ignore_whitespace and change.get('before') is not None and change.get('after') is not None
    rows, changes = [], []
    word_budget = 1000000
    def key(line):
        return ''.join(line.split()) if ignore_whitespace else line
    # Blank-only rows stay visible but do not displace real lines during filtering.
    left_indices = [i for i, line in enumerate(before) if not ignore_whitespace or key(line)]
    right_indices = [i for i, line in enumerate(after) if not ignore_whitespace or key(line)]
    alignment = []
    left_cursor = right_cursor = 0
    matcher = difflib.SequenceMatcher(None, [key(before[i]) for i in left_indices],
                                     [key(after[i]) for i in right_indices], autojunk=False)
    for group, (kind, a, b, c, d) in enumerate(matcher.get_opcodes()):
        for offset in range(max(b - a, d - c)):
            left = left_indices[a + offset] if a + offset < b else None
            right = right_indices[c + offset] if c + offset < d else None
            left_blanks = list(range(left_cursor, left)) if left is not None else []
            right_blanks = list(range(right_cursor, right)) if right is not None else []
            for blank in range(max(len(left_blanks), len(right_blanks))):
                alignment.append(('equal', left_blanks[blank] if blank < len(left_blanks) else None,
                                  right_blanks[blank] if blank < len(right_blanks) else None, group))
            alignment.append((kind, left, right, group))
            if left is not None:
                left_cursor = left + 1
            if right is not None:
                right_cursor = right + 1
    for offset in range(max(len(before) - left_cursor, len(after) - right_cursor)):
        alignment.append(('equal', left_cursor + offset if left_cursor + offset < len(before) else None,
                          right_cursor + offset if right_cursor + offset < len(after) else None, -1))
    last_group = None
    in_change = False
    for kind, left, right, group in alignment:
        if group != last_group:
            in_change = False
        last_group = group
        row = {'before_number': left + 1 if left is not None else None,
               'after_number': right + 1 if right is not None else None,
               'before': before[left] if left is not None else None,
               'after': after[right] if right is not None else None,
               'kind': kind, 'before_words': [], 'after_words': [], 'ending_changed': False,
               'ignored_whitespace': False}
        if ignore_whitespace:
            row['ignored_whitespace'] = (row['before'] != row['after'] and
                key(row['before'] or '') == key(row['after'] or ''))
            if row['ignored_whitespace']:
                row['kind'] = 'equal'
        if row['kind'] != 'equal' and not in_change:
            changes.append(len(rows) + 1)
        in_change = row['kind'] != 'equal'
        if row['kind'] == 'replace' and row['before'] is not None and row['after'] is not None:
            removed, added, cost = word_changes(row['before'], row['after'], min(word_budget, 200000))
            word_budget -= cost
            row['before_words'], row['after_words'] = removed, added
            row['ending_changed'] = (row['before'][len(row['before'].rstrip('\r\n')):] !=
                                     row['after'][len(row['after'].rstrip('\r\n')):])
        rows.append(row)
    return {'rows': rows, 'changes': changes}


def side_line(number, content, show_endings=False):
    if content is None:
        return '\n'
    ending = 'CRLF' if content.endswith('\r\n') else 'LF' if content.endswith('\n') else 'CR' if content.endswith('\r') else 'no final newline'
    marker = f'  ⟦{ending}⟧' if show_endings else ''
    return f'{number:>4} │ {content.rstrip(chr(13) + chr(10))}{marker}\n'
