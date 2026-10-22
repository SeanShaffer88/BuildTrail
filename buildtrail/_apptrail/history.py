"""Persistent, local snapshot library with owned JSON copies and a SQLite catalog."""
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .core import atomic_write, load_snapshot


def history_directory():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'AppTrail' / 'history'
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'AppTrail' / 'history'


class SnapshotHistory:
    def __init__(self, directory=None):
        self.directory = Path(directory if directory is not None else history_directory()).resolve()
        self.snapshots = self.directory / 'snapshots'
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'history.sqlite3'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, root TEXT NOT NULL,
                captured_at TEXT NOT NULL, captured_ts REAL NOT NULL,
                file_count INTEGER NOT NULL, issues INTEGER NOT NULL)''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(snapshots)')}
            for name, definition in (('notes', "TEXT NOT NULL DEFAULT ''"),
                                     ('deleted_at', 'TEXT'), ('baseline', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    db.execute(f'ALTER TABLE snapshots ADD COLUMN {name} {definition}')
            # Finish only previously staged deletion operations after an interrupted exit.
            for staging in self.snapshots.glob('*.pending-delete'):
                parts = staging.name.split('.')
                if len(parts) != 3 or len(parts[1]) != 32 or any(c not in '0123456789abcdef' for c in parts[1]):
                    continue
                try:
                    target = self.path(parts[0])
                except ValueError:
                    continue
                exists = db.execute('SELECT id FROM snapshots WHERE id = ?', (parts[0],)).fetchone()
                if exists and not target.exists():
                    staging.replace(target)
                elif not exists:
                    staging.unlink()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def path(self, identity):
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('Invalid history snapshot ID.')
        return self.snapshots / f'{identity}.json'

    def add(self, snapshot, label=None):
        # A canonical copy gives identical imports the same ID, irrespective of formatting.
        content = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)
        identity = hashlib.sha256(content.encode('utf-8')).hexdigest()
        captured = datetime.fromisoformat(snapshot['completed_at'])
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        timestamp = captured.timestamp()
        target = self.path(identity)
        # Write before cataloging: a failed write must never create a usable-looking row.
        atomic_write(target, content)
        issues = len(snapshot['warnings']) + sum(bool(f.get('error')) for f in snapshot['files'])
        label = label or Path(snapshot['root']).name or snapshot['root']
        with self.connect() as db:
            db.execute('''INSERT INTO snapshots (id, label, root, captured_at, captured_ts, file_count, issues)
                       VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET deleted_at = NULL''',
                       (identity, label, snapshot['root'], snapshot['completed_at'], timestamp,
                        len(snapshot['files']), issues))
        return identity

    def import_file(self, path):
        snapshot = load_snapshot(path)
        return self.add(snapshot)

    def entries(self, query='', deleted=False):
        with self.connect() as db:
            state = 'IS NOT NULL' if deleted else 'IS NULL'
            entries = [dict(row) for row in db.execute(f'SELECT * FROM snapshots WHERE deleted_at {state} ORDER BY captured_ts DESC, rowid DESC')]
        query = query.strip().casefold()
        for entry in entries:
            entry['available'] = self.path(entry['id']).is_file()
        return [entry for entry in entries if query in (entry['label'] + ' ' + entry['notes'] + ' ' + entry['root'] + ' ' + entry['captured_at']).casefold()]

    def get(self, identity, include_deleted=False):
        self.path(identity)
        with self.connect() as db:
            row = db.execute('SELECT * FROM snapshots WHERE id = ?', (identity,)).fetchone()
        if row is None or (row['deleted_at'] is not None and not include_deleted):
            raise ValueError('This snapshot is not in active history. Refresh the list or restore it from Trash.')
        return dict(row)

    def update_metadata(self, identity, label, notes):
        label, notes = label.strip(), notes.strip()
        if not label or len(label) > 120 or any(ord(c) < 32 for c in label):
            raise ValueError('Choose a single-line snapshot name of 1–120 characters.')
        if len(notes) > 10000 or '\0' in notes:
            raise ValueError('Notes must be at most 10,000 characters and contain no null characters.')
        with self.connect() as db:
            changed = db.execute('UPDATE snapshots SET label = ?, notes = ? WHERE id = ? AND deleted_at IS NULL',
                                 (label, notes, identity)).rowcount
            if not changed:
                raise ValueError('This snapshot is no longer in active history.')

    @staticmethod
    def root_key(root):
        return root.replace('/', '\\').rstrip('\\').casefold()

    def set_baseline(self, identity):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM snapshots WHERE id = ? AND deleted_at IS NULL', (identity,)).fetchone()
            if row is None:
                raise ValueError('Select an active snapshot to mark as baseline.')
            matching = [r['id'] for r in db.execute('SELECT id, root FROM snapshots')
                        if self.root_key(r['root']) == self.root_key(row['root'])]
            db.executemany('UPDATE snapshots SET baseline = 0 WHERE id = ?', [(key,) for key in matching])
            db.execute('UPDATE snapshots SET baseline = 1 WHERE id = ?', (identity,))

    def baseline_pair(self, identity):
        selected = self.get(identity)
        baseline = next((e for e in self.entries() if e['baseline'] and self.root_key(e['root']) == self.root_key(selected['root'])), None)
        if baseline is None:
            raise ValueError('Mark a known-good snapshot from this application folder as baseline first.')
        if baseline['id'] == identity:
            raise ValueError('Select another snapshot to compare with this baseline.')
        return [(key, self.load(key)) for key in (baseline['id'], identity)]

    def trash(self, identities):
        identities = list(dict.fromkeys(identities))
        if not identities:
            raise ValueError('Select snapshots to move to Trash.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for identity in identities:
                row = db.execute('SELECT id FROM snapshots WHERE id = ? AND deleted_at IS NULL', (identity,)).fetchone()
                if row is None:
                    raise ValueError('A selected snapshot is no longer in active history. Refresh the list.')
            deleted_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            db.executemany('UPDATE snapshots SET deleted_at = ?, baseline = 0 WHERE id = ?',
                           [(deleted_at, identity) for identity in identities])

    def restore(self, identities):
        identities = list(dict.fromkeys(identities))
        if not identities:
            raise ValueError('Select snapshots to restore.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for identity in identities:
                if db.execute('SELECT id FROM snapshots WHERE id = ? AND deleted_at IS NOT NULL', (identity,)).fetchone() is None:
                    raise ValueError('A selected snapshot is no longer in Trash. Refresh the list.')
            db.executemany('UPDATE snapshots SET deleted_at = NULL WHERE id = ?', [(identity,) for identity in identities])

    def purge(self, identities):
        """Delete only explicitly selected trashed copies; stage files for rollback."""
        identities = list(dict.fromkeys(identities))
        if not identities:
            raise ValueError('Select snapshots from Trash to permanently delete.')
        staged = []
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for identity in identities:
                    target = self.path(identity)
                    if db.execute('SELECT id FROM snapshots WHERE id = ? AND deleted_at IS NOT NULL', (identity,)).fetchone() is None:
                        raise ValueError('Only snapshots already in Trash can be permanently deleted.')
                    if target.exists():
                        staging = target.with_suffix(f'.{uuid.uuid4().hex}.pending-delete')
                        target.replace(staging)
                        staged.append((target, staging))
                db.executemany('DELETE FROM snapshots WHERE id = ?', [(identity,) for identity in identities])
        except Exception:
            for target, staging in reversed(staged):
                staging.replace(target)
            raise
        for _, staging in staged:
            try:
                staging.unlink()
            except OSError as exc:
                raise OSError(f'History entries removed, but a staged copy could not be cleaned up: {staging}. {exc}') from exc

    def comparison_context(self, paths):
        context = {}
        for side, path in zip(('before', 'after'), paths):
            path = Path(path).resolve()
            if path.parent == self.snapshots:
                entry = self.get(path.stem)
                context[side] = {'label': entry['label'], 'notes': entry['notes'], 'baseline': bool(entry['baseline'])}
        return context

    def load(self, identity):
        path = self.path(identity)
        self.get(identity)
        try:
            content = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            raise ValueError('This history snapshot is missing. Import its JSON again to restore it.') from None
        if hashlib.sha256(content.encode('utf-8')).hexdigest() != identity:
            raise ValueError('This history snapshot changed outside AppTrail. Import the changed JSON as a new snapshot.')
        return load_snapshot(path)

    def load_path(self, path):
        path = Path(path).resolve()
        if path.parent == self.snapshots:
            return self.load(path.stem)
        return load_snapshot(path)

    def compare_pair(self, identities):
        if len(identities) != 2 or len(set(identities)) != 2:
            raise ValueError('Select exactly two different snapshots to compare.')
        entries = {e['id']: e for e in self.entries()}
        if any(identity not in entries for identity in identities):
            raise ValueError('The selected snapshot is no longer in history. Refresh the list.')
        # Older capture is Before; newer is After. Preserve selection order on ties.
        ordered = sorted(identities, key=lambda identity: entries[identity]['captured_ts'])
        return [(identity, self.load(identity)) for identity in ordered]
