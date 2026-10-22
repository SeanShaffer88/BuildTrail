"""Separate run labels/notes, recoverable Trash, and locked owned-file deletion."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from ._apptrail.core import now, is_link


def run_name(record):
    return record.get('organization', {}).get('name') or record['recipe']['name']


class RunBusyError(ValueError):
    pass


class RunOrganization:
    def initialize_organization(self):
        self.locks = self.directory / 'locks'
        self.pending_delete = self.directory / 'pending-delete'
        for path in (self.locks, self.pending_delete):
            path.mkdir(exist_ok=True)
            if path.resolve() != path or is_link(path.stat(follow_symlinks=False)):
                raise ValueError('BuildTrail storage directories must not be links or reparse points.')
        self.organization_db = self.directory / 'organization.sqlite3'
        with self.connect_organization() as db:
            db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT \'\', notes TEXT NOT NULL DEFAULT \'\', deleted_at TEXT)')
        self.recover_deletions()

    @contextmanager
    def connect_organization(self):
        db = sqlite3.connect(self.organization_db, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def run_lock(self, identity):
        self.run_path(identity)
        if self.locks.resolve() != self.locks:
            raise ValueError('BuildTrail lock directory changed outside the app.')
        path = self.locks / (identity + '.lock')
        if path.is_symlink() or (path.exists() and is_link(path.stat(follow_symlinks=False))):
            raise ValueError('BuildTrail lock files must not be links.')
        with path.open('a+b') as stream:
            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RunBusyError('This run is still active in another operation or BuildTrail window. Try again after it finishes.') from None
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def organization(self, identity):
        self.run_path(identity)
        with self.connect_organization() as db:
            row = db.execute('SELECT name, notes, deleted_at FROM runs WHERE id = ?', (identity,)).fetchone()
        return dict(row) if row else {'name': '', 'notes': '', 'deleted_at': None}

    def update_metadata(self, identity, name, notes):
        if not isinstance(name, str) or not isinstance(notes, str):
            raise ValueError('Run name and notes must be text.')
        name, notes = name.strip(), notes.strip()
        if not name or len(name) > 120 or any(ord(c) < 32 for c in name):
            raise ValueError('Choose a single-line run name of 1–120 characters.')
        if len(notes) > 10000 or '\0' in notes:
            raise ValueError('Notes must be at most 10,000 characters and contain no null characters.')
        with self.run_lock(identity):
            self.load(identity)
            with self.connect_organization() as db:
                db.execute('INSERT INTO runs (id, name, notes) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, notes = excluded.notes', (identity, name, notes))

    def _identities(self, identities):
        identities = sorted(set(identities))
        if not identities:
            raise ValueError('Select at least one build run.')
        for identity in identities:
            self.run_path(identity)
        return identities

    def trash(self, identities):
        identities = self._identities(identities)
        with ExitStack() as locks:
            for identity in identities:
                locks.enter_context(self.run_lock(identity))
                self.load(identity)
            with self.connect_organization() as db:
                db.execute('BEGIN IMMEDIATE')
                for identity in identities:
                    db.execute('INSERT INTO runs (id, deleted_at) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET deleted_at = excluded.deleted_at', (identity, now()))

    def restore(self, identities):
        identities = self._identities(identities)
        with ExitStack() as locks:
            for identity in identities:
                locks.enter_context(self.run_lock(identity))
                record = self.load(identity, include_deleted=True)
                if record['organization']['deleted_at'] is None:
                    raise ValueError('Only runs in Trash can be restored.')
            with self.connect_organization() as db:
                db.executemany('UPDATE runs SET deleted_at = NULL WHERE id = ?', [(i,) for i in identities])

    def validate_owned_tree(self, path, parent):
        if parent.resolve() != parent or path.resolve().parent != parent or not path.is_dir():
            raise ValueError('Deletion target must be an owned BuildTrail run directory.')
        if is_link(path.stat(follow_symlinks=False)):
            raise ValueError('Refusing to delete a linked run directory.')
        for folder, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                child = Path(folder) / name
                if is_link(child.stat(follow_symlinks=False)):
                    raise ValueError('Refusing to delete a run containing symbolic links or reparse points.')

    def purge(self, identities):
        identities = self._identities(identities)
        staged = []
        with ExitStack() as locks:
            for identity in identities:
                locks.enter_context(self.run_lock(identity))
                record = self.load(identity, include_deleted=True)
                if record['organization']['deleted_at'] is None:
                    raise ValueError('Move runs to Trash before permanently deleting them.')
                self.validate_owned_tree(self.run_path(identity), self.runs)
            try:
                with self.connect_organization() as db:
                    db.execute('BEGIN IMMEDIATE')
                    for identity in identities:
                        target = self.run_path(identity)
                        staging = self.pending_delete / (identity + '.' + uuid.uuid4().hex)
                        self.validate_owned_tree(target, self.runs)
                        if self.pending_delete.resolve() != self.pending_delete:
                            raise ValueError('The deletion staging directory changed outside BuildTrail.')
                        target.replace(staging)
                        staged.append((target, staging))
                    db.executemany('DELETE FROM runs WHERE id = ?', [(i,) for i in identities])
            except BaseException:
                for target, staging in reversed(staged):
                    staging.replace(target)
                raise
            for _, staging in staged:
                self.validate_owned_tree(staging, self.pending_delete)
                shutil.rmtree(staging)

    def recover_deletions(self):
        for staging in self.pending_delete.iterdir():
            parts = staging.name.split('.')
            if len(parts) != 2 or any(len(p) != 32 or any(c not in '0123456789abcdef' for c in p) for p in parts):
                continue
            identity = parts[0]
            try:
                with self.run_lock(identity):
                    if not staging.exists():
                        continue  # Another window completed this deletion before lock acquisition.
                    self.validate_owned_tree(staging, self.pending_delete)
                    target = self.run_path(identity)
                    metadata = self.organization(identity)
                    if metadata['deleted_at'] is not None:
                        if target.exists():
                            raise ValueError('Both original and staged run copies exist; no recovery files were deleted.')
                        staging.replace(target)
                    else:
                        shutil.rmtree(staging)
            except RunBusyError:
                continue
