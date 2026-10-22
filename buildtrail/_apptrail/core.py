import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from datetime import datetime, timezone

from .pe import inspect_pe, file_version
from .textdiff import (MAX_TEXT_BYTES, MAX_TOTAL_TEXT_BYTES, POLICY,
                       eligibility, decode_text, validate_text)

SCHEMA = 'apptrail.snapshot.v2'
LEGACY_SCHEMA = 'apptrail.snapshot.v1'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def atomic_write(path, content):
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, delete=False) as stream:
            temp = stream.name
            stream.write(content)
        os.replace(temp, path)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def save_snapshot(snapshot, output):
    output = Path(output).resolve()
    root = Path(snapshot['root']).resolve()
    if output == root or root in output.parents:
        raise ValueError('Save the snapshot outside the application folder to avoid scanning AppTrail outputs.')
    atomic_write(output, json.dumps(snapshot, ensure_ascii=False, indent=2))


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def snapshot_folder(root, progress=lambda message: None, cancelled=lambda: False, capture_text=True, exclusions=()):
    from .checkpoints import exclusion_rules, excluded
    exclusions = exclusion_rules(exclusions)
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Choose an application folder.')
    result = {'schema': SCHEMA, 'root': str(root), 'started_at': now(),
              'completed_at': None, 'files': [], 'warnings': [],
              'text_capture': {'enabled': bool(capture_text), 'policy': POLICY,
                               'captured_bytes': 0}}
    text_bytes = 0
    warnings = result['warnings']
    pending = [root]
    while pending:
        if cancelled():
            raise InterruptedError('Snapshot cancelled. No partial snapshot was saved.')
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda e: e.name.casefold())
        except OSError as exc:
            warnings.append({'path': directory.relative_to(root).as_posix(), 'message': f'Cannot enumerate directory: {exc}'})
            continue
        for child in children:
            if cancelled():
                raise InterruptedError('Snapshot cancelled. No partial snapshot was saved.')
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if excluded(relative, exclusions):
                continue
            try:
                # Windows DirEntry.stat can omit file identity (st_ino = 0).
                # Use the same stat API for both sides of the stability check.
                before = path.stat(follow_symlinks=False)
                if is_link(before):
                    warnings.append({'path': relative, 'message': 'Skipped symbolic link or reparse point.'})
                    continue
                if stat.S_ISDIR(before.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(before.st_mode):
                    warnings.append({'path': relative, 'message': 'Skipped nonregular file.'})
                    continue
                progress(f"Hashing {relative}")
                entry = {'path': relative, 'size': before.st_size, 'mtime_ns': before.st_mtime_ns,
                         'sha256': None, 'version': None, 'pe': None, 'error': None,
                         'text': None, 'text_status': 'disabled' if not capture_text else eligibility(relative)}
                if entry['text_status'] == 'eligible':
                    if before.st_size > MAX_TEXT_BYTES:
                        entry['text_status'] = 'too_large'
                    elif before.st_size > MAX_TOTAL_TEXT_BYTES - text_bytes:
                        entry['text_status'] = 'budget_exceeded'
                collect_text = entry['text_status'] == 'eligible'
                raw_text = bytearray()
                result['files'].append(entry)
                digest = hashlib.sha256()
                bytes_read = 0
                with path.open('rb') as stream:
                    while chunk := stream.read(1024 * 1024):
                        if cancelled():
                            raise InterruptedError('Snapshot cancelled. No partial snapshot was saved.')
                        digest.update(chunk)
                        bytes_read += len(chunk)
                        if collect_text:
                            if len(raw_text) + len(chunk) > MAX_TEXT_BYTES:
                                collect_text = False
                                raw_text.clear()
                                entry['text_status'] = 'too_large'
                            else:
                                raw_text.extend(chunk)
                entry['sha256'] = digest.hexdigest()
                if collect_text:
                    entry['text'], entry['text_status'] = decode_text(bytes(raw_text))
                try:
                    entry['pe'] = inspect_pe(path)
                    if entry['pe']:
                        entry['version'] = file_version(path)
                except (OSError, ValueError) as exc:
                    warnings.append({'path': relative, 'message': f'PE metadata unavailable: {exc}'})
                after = path.stat(follow_symlinks=False)
                if bytes_read != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    entry['sha256'] = None
                    entry['text'] = None
                    entry['text_status'] = 'unstable'
                    entry['error'] = 'File changed during capture; recapture while the application is idle.'
                elif entry['text'] is not None:
                    text_bytes += len(raw_text)
            except OSError as exc:
                if result['files'] and result['files'][-1]['path'] == relative:
                    result['files'][-1]['error'] = str(exc)
                    result['files'][-1]['sha256'] = None
                    result['files'][-1]['text'] = None
                    result['files'][-1]['text_status'] = 'read_error'
                warnings.append({'path': relative, 'message': f'Cannot read file: {exc}'})
    result['files'].sort(key=lambda e: e['path'].casefold())
    result['completed_at'] = now()
    result['text_capture']['captured_bytes'] = text_bytes
    return result


def load_snapshot(path):
    with open(path, encoding='utf-8') as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or data.get('schema') not in (SCHEMA, LEGACY_SCHEMA):
        raise ValueError('This is not an AppTrail v1 or v2 snapshot.')
    for field in ('root', 'started_at', 'completed_at'):
        if not isinstance(data.get(field), str):
            raise ValueError(f'Invalid snapshot field: {field}')
    if not isinstance(data.get('files'), list) or not isinstance(data.get('warnings'), list):
        raise ValueError('Invalid snapshot file or warning list.')
    checkpoint = data.get('checkpoint')
    if checkpoint is not None:
        from .checkpoints import exclusion_rules
        if (not isinstance(checkpoint, dict) or not isinstance(checkpoint.get('name'), str)
                or not checkpoint['name'].strip() or len(checkpoint['name']) > 120
                or any(ord(c) < 32 for c in checkpoint['name'])
                or not isinstance(checkpoint.get('exclusions'), list)
                or not isinstance(checkpoint.get('git'), dict)):
            raise ValueError('Invalid checkpoint metadata.')
        exclusion_rules(checkpoint['exclusions'])
        git = checkpoint['git']
        if git.get('status') not in ('available', 'unavailable'):
            raise ValueError('Invalid checkpoint Git status.')
        if git['status'] == 'available':
            if any(not isinstance(git.get(k), str) for k in ('repository', 'commit', 'branch', 'subject', 'author', 'committed_at')) or type(git.get('dirty')) is not bool:
                raise ValueError('Invalid checkpoint Git details.')
        elif not isinstance(git.get('message'), str):
            raise ValueError('Invalid checkpoint Git message.')
    seen = set()
    total_text = 0
    for entry in data['files']:
        if not isinstance(entry, dict):
            raise ValueError('Invalid file entry.')
        name = entry.get('path')
        if not isinstance(name, str) or not name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name or ':' in name:
            raise ValueError('Invalid relative path in snapshot.')
        if name.casefold() in seen:
            raise ValueError(f'Duplicate path in snapshot: {name}')
        seen.add(name.casefold())
        for field in ('size', 'mtime_ns'):
            if type(entry.get(field)) is not int or entry[field] < 0:
                raise ValueError(f'Invalid {field}: {name}')
        sha = entry.get('sha256')
        if sha is not None and (not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha)):
            raise ValueError(f'Invalid SHA-256: {name}')
        for field in ('version', 'error'):
            if entry.get(field) is not None and not isinstance(entry[field], str):
                raise ValueError(f'Invalid {field}: {name}')
        total_text += validate_text(entry)
        if total_text > MAX_TOTAL_TEXT_BYTES:
            raise ValueError('Snapshot exceeds the total text capture budget.')
        pe = entry.get('pe')
        if pe is not None:
            if not isinstance(pe, dict) or not isinstance(pe.get('architecture'), str):
                raise ValueError(f'Invalid PE metadata: {name}')
            for field in ('imports', 'delay_imports'):
                if not isinstance(pe.get(field), list) or not all(isinstance(v, str) for v in pe[field]):
                    raise ValueError(f'Invalid imports: {name}')
    for warning in data['warnings']:
        if not isinstance(warning, dict) or not all(isinstance(warning.get(k), str) for k in ('path', 'message')):
            raise ValueError('Invalid snapshot warning.')
    capture = data.get('text_capture')
    if data['schema'] == SCHEMA or capture is not None:
        if not isinstance(capture, dict) or type(capture.get('enabled')) is not bool or capture.get('policy') != POLICY:
            raise ValueError('Invalid text capture policy.')
        if type(capture.get('captured_bytes')) is not int or capture['captured_bytes'] != total_text or (not capture['enabled'] and any(f.get('text') is not None for f in data['files'])):
            raise ValueError('Text capture byte count does not match the saved evidence.')
    return data


def compare(before, after):
    old = {e['path'].casefold(): e for e in before['files']}
    new = {e['path'].casefold(): e for e in after['files']}
    changes = []
    counts = dict.fromkeys(('Added', 'Removed', 'Modified', 'Metadata', 'Unchanged', 'Unknown'), 0)
    incomplete = bool(before['warnings'] or after['warnings'])
    for key in sorted(old.keys() | new.keys()):
        a, b = old.get(key), new.get(key)
        notes = []
        if a is None:
            status = 'Added'
            notes.append('Present only in the after snapshot.' + (' Capture warnings may explain the difference.' if incomplete else ''))
        elif b is None:
            status = 'Removed'
            notes.append('Present only in the before snapshot.' + (' Capture warnings may explain the difference.' if incomplete else ''))
        elif a.get('error') or b.get('error') or not a['sha256'] or not b['sha256']:
            status = 'Unknown'
            notes.append('Content comparison unavailable because a capture was incomplete or unstable.')
        else:
            status = 'Modified' if a['sha256'] != b['sha256'] else 'Unchanged'
            for field in ('size', 'version'):
                if a.get(field) != b.get(field):
                    notes.append(f"{field.title()}: {a.get(field)} → {b.get(field)}")
            ap, bp = a.get('pe') or {}, b.get('pe') or {}
            if ap.get('architecture') != bp.get('architecture'):
                notes.append(f"Architecture: {ap.get('architecture', 'unavailable')} → {bp.get('architecture', 'unavailable')}")
            for field in ('imports', 'delay_imports'):
                av, bv = set(ap.get(field, [])), set(bp.get(field, []))
                if bv - av:
                    notes.append(f"New {field.replace('_', ' ')}: {', '.join(sorted(bv - av))}")
                if av - bv:
                    notes.append(f"Removed {field.replace('_', ' ')}: {', '.join(sorted(av - bv))}")
            if a['mtime_ns'] != b['mtime_ns']:
                notes.append('Last-write timestamp changed.')
            if a['path'] != b['path']:
                notes.append(f"Path casing changed: {a['path']} → {b['path']}")
            if status == 'Unchanged' and notes:
                status = 'Metadata'
            if status == 'Modified':
                notes.insert(0, 'SHA-256 content hash changed.')
        counts[status] += 1
        changes.append({'path': (b or a)['path'], 'status': status, 'notes': notes, 'before': a, 'after': b})
    return {'before': before, 'after': after, 'created_at': now(), 'counts': counts, 'changes': changes}
