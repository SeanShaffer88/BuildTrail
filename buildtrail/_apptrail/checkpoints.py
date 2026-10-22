"""Project checkpoint capture policy and read-only local Git observations."""
import fnmatch
from pathlib import Path
import subprocess

DEFAULT_EXCLUSIONS = '.git/\n__pycache__/\n.venv/\nnode_modules/'


def exclusion_rules(value):
    rules = value.splitlines() if isinstance(value, str) else list(value)
    if len(rules) > 200 or any(not isinstance(r, str) or len(r) > 500 for r in rules):
        raise ValueError('Use at most 200 exclusion patterns, each at most 500 characters.')
    rules = [r.strip().replace('\\', '/') for r in rules if r.strip()]
    if any(r.startswith(('!', '/')) or ':' in r or '..' in r.split('/') for r in rules):
        raise ValueError('Use relative glob patterns without !, drive letters, or parent paths.')
    return rules


def excluded(path, rules):
    path = path.casefold()
    parts = path.split('/')
    for rule in rules:
        pattern = rule.casefold().rstrip('/')
        if '/' not in pattern:
            if any(fnmatch.fnmatchcase(part, pattern) for part in parts):
                return True
        elif any(fnmatch.fnmatchcase('/'.join(parts[:i]), pattern) for i in range(1, len(parts) + 1)):
            return True
    return False


def git_details(folder):
    def git(*args):
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(folder), *args],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('Git metadata unavailable (folder may not be a repository or have a commit).')
        return result.stdout.strip()
    try:
        top = git('rev-parse', '--show-toplevel')
        commit = git('rev-parse', '--verify', 'HEAD')
        return {'status': 'available', 'repository': top, 'commit': commit,
                'branch': git('rev-parse', '--abbrev-ref', 'HEAD'),
                'subject': git('log', '-1', '--format=%s'),
                'author': git('log', '-1', '--format=%an'),
                'committed_at': git('log', '-1', '--format=%cI'),
                'dirty': bool(git('status', '--porcelain', '--untracked-files=normal'))}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'status': 'unavailable', 'message': str(exc)}


def capture_checkpoint(history, folder, name, exclusions, capture_text=True,
                       progress=lambda message: None, cancelled=lambda: False):
    from .core import snapshot_folder
    name = name.strip()
    if not name or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError('Choose a single-line checkpoint name of 1–120 characters.')
    root = Path(folder).resolve(strict=True)
    if root == history.directory or root in history.directory.parents or history.directory in root.parents:
        raise ValueError('Choose a project folder outside AppTrail history.')
    rules = exclusion_rules(exclusions)
    snapshot = snapshot_folder(root, progress, cancelled, capture_text=capture_text, exclusions=rules)
    snapshot['checkpoint'] = {'name': name, 'exclusions': rules, 'git': git_details(root)}
    if cancelled():
        raise InterruptedError('Checkpoint cancelled.')
    identity = history.add(snapshot, label=name)
    history.set_baseline(identity)
    return identity, snapshot


def compare_current(history, identity, progress=lambda message: None, cancelled=lambda: False):
    from .core import snapshot_folder, compare
    before = history.load(identity)
    policy = before.get('checkpoint')
    if not policy:
        raise ValueError('Select a project checkpoint created with Save checkpoint.')
    root = Path(before['root']).resolve(strict=True)
    if root == history.directory or root in history.directory.parents or history.directory in root.parents:
        raise ValueError('The project folder overlaps AppTrail history.')
    after = snapshot_folder(root, progress, cancelled,
                            capture_text=before.get('text_capture', {}).get('enabled', False),
                            exclusions=policy['exclusions'])
    after['checkpoint'] = {'name': 'Current project', 'exclusions': policy['exclusions'],
                           'git': git_details(root)}
    if cancelled():
        raise InterruptedError('Comparison cancelled.')
    current = history.add(after, label='Current · ' + policy['name'][:110])
    paths = (str(history.path(identity)), str(history.path(current)))
    result = compare(before, after)
    result['context'] = history.comparison_context(paths)
    return ('comparison', result, paths)
