"""Build recipes, bounded file evidence, and immutable per-run observations."""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import time
import uuid

from ._apptrail.core import atomic_write, snapshot_folder, load_snapshot, now, is_link
from ._apptrail.checkpoints import exclusion_rules, excluded, git_details
from ._apptrail.history import SnapshotHistory
from .process import execute
from .organization import RunOrganization, run_name

DEFAULT_EXCLUSIONS = ['.git/', '.venv/', 'venv/', '__pycache__/', 'node_modules/', 'build/', 'dist/', 'obj/', 'bin/']
DEPENDENCIES = ['requirements*.txt', 'pyproject.toml', 'uv.lock', 'poetry.lock', 'Pipfile*',
    'package.json', 'package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock',
    'Cargo.toml', 'Cargo.lock', 'go.mod', 'go.sum', '*.csproj', '*.fsproj', 'packages.lock.json',
    'Directory.Packages.props', 'vcpkg.json', 'vcpkg-configuration.json', 'conanfile.*',
    'CMakeLists.txt', 'CMakePresets.json', 'meson.build', 'meson_options.txt']


def data_directory():
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') if os.name == 'nt' else Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    return base / 'BuildTrail'


def default_recipe(project=''):
    return {'name': 'Release build', 'project': str(project), 'command': 'cmake --build build --config Release',
        'shell': 'powershell', 'timeout_seconds': 1800, 'output_patterns': ['build/**/*.exe', 'dist/**/*'],
        'exclusions': DEFAULT_EXCLUSIONS[:], 'capture_text': True, 'checkpoint_id': '',
        'tools': [['cmake', '--version'], ['cl'], ['ninja', '--version']]}


def validate_recipe(recipe):
    if not isinstance(recipe, dict):
        raise ValueError('Recipe must be a JSON object.')
    for key in ('name', 'project'):
        if not isinstance(recipe.get(key), str) or not recipe[key].strip():
            raise ValueError(f'Recipe needs {key}.')
    if len(recipe['name']) > 120 or any(ord(c) < 32 for c in recipe['name']):
        raise ValueError('Recipe names must be a single line of at most 120 characters.')
    if recipe.get('shell', 'powershell') not in ('powershell', 'argv'):
        raise ValueError('Choose powershell or argv execution.')
    def argv(value):
        return isinstance(value, list) and 0 < len(value) <= 100 and all(isinstance(a, str) and '\0' not in a and len(a) <= 16000 for a in value) and bool(value[0])
    if recipe.get('shell', 'powershell') == 'argv':
        if not argv(recipe.get('argv')):
            raise ValueError('argv must be an array of executable and argument strings.')
    elif not isinstance(recipe.get('command'), str) or not recipe['command'].strip() or '\0' in recipe['command'] or len(recipe['command']) > 32000:
        raise ValueError('Enter a build command of at most 32,000 characters.')
    if type(recipe.get('timeout_seconds')) is not int or not 1 <= recipe['timeout_seconds'] <= 86400:
        raise ValueError('Timeout must be 1–86,400 seconds.')
    if type(recipe.get('capture_text')) is not bool:
        raise ValueError('capture_text must be a boolean.')
    if not isinstance(recipe.get('exclusions'), list):
        raise ValueError('exclusions must be an array.')
    exclusion_rules(recipe['exclusions'])
    patterns = recipe.get('output_patterns')
    if not isinstance(patterns, list) or not 1 <= len(patterns) <= 100:
        raise ValueError('Enter 1–100 output glob patterns.')
    for pattern in patterns:
        if (not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 500
                or pattern.startswith(('/', '\\')) or ':' in pattern or '..' in pattern.replace('\\', '/').split('/')):
            raise ValueError('Output patterns must stay inside the project folder.')
    if not isinstance(recipe.get('tools'), list) or len(recipe['tools']) > 20 or any(not argv(tool) for tool in recipe['tools']):
        raise ValueError('Use at most 20 tool probes, each an array of executable and arguments.')
    checkpoint = recipe.get('checkpoint_id', '')
    if not isinstance(checkpoint, str) or (checkpoint and (len(checkpoint) != 64 or any(c not in '0123456789abcdef' for c in checkpoint))):
        raise ValueError('Invalid AppTrail checkpoint ID.')
    return recipe


class BuildStore(RunOrganization):
    def __init__(self, directory=None):
        self.directory = Path(directory if directory is not None else data_directory()).resolve()
        self.runs = self.directory / 'runs'
        self.runs.mkdir(parents=True, exist_ok=True)
        if self.runs.resolve() != self.runs or is_link(self.runs.stat(follow_symlinks=False)):
            raise ValueError('Run storage must not be a link or reparse point.')
        self.initialize_organization()

    def run_path(self, identity):
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('Invalid build ID.')
        path = self.runs / identity
        if self.runs.resolve() != self.runs or path.resolve().parent != self.runs or (path.exists() and is_link(path.stat(follow_symlinks=False))):
            raise ValueError('Run path escapes or links outside the managed library.')
        return path

    def save(self, record):
        atomic_write(self.run_path(record['id']) / 'run.json', json.dumps({k: v for k, v in record.items() if k != 'organization'}, indent=2, ensure_ascii=False))

    def load(self, identity, include_deleted=False):
        record = json.loads((self.run_path(identity) / 'run.json').read_text(encoding='utf-8'))
        if not isinstance(record, dict) or record.get('schema') != 'buildtrail.run.v1' or record.get('id') != identity:
            raise ValueError('Invalid BuildTrail run record.')
        validate_recipe(record.get('recipe'))
        if record.get('status') not in ('unfinished', 'success', 'failed', 'error', 'cancelled', 'timed_out') or not isinstance(record.get('started_at'), str):
            raise ValueError('Invalid build status or timestamp.')
        for key in ('tools', 'warnings', 'dependencies', 'dependencies_after', 'outputs_before', 'outputs_after'):
            if not isinstance(record.get(key), list):
                raise ValueError('Invalid build evidence list: ' + key)
        if not isinstance(record.get('evidence_hashes'), dict):
            raise ValueError('Invalid evidence fingerprints.')
        record['organization'] = self.organization(identity)
        if record['organization']['deleted_at'] is not None and not include_deleted:
            raise ValueError('This run is in Trash. Restore it before reviewing or comparing it.')
        return record

    def entries(self, query='', deleted=False):
        result = []
        for path in self.runs.glob('*/run.json'):
            try:
                record = self.load(path.parent.name, include_deleted=True)
                if bool(record['organization']['deleted_at']) != bool(deleted):
                    continue
                text = ' '.join((run_name(record), record['organization']['notes'], record['recipe']['name'], record['recipe']['project'], record['started_at'], record['status']))
                if query.strip().casefold() in text.casefold():
                    result.append(record)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(result, key=lambda r: (r.get('started_ns', 0), r['id']), reverse=True)

    def recipes(self):
        try:
            recipes = json.loads((self.directory / 'recipes.json').read_text(encoding='utf-8'))
            if not isinstance(recipes, list):
                return []
            return [validate_recipe(r) for r in recipes]
        except FileNotFoundError:
            return []

    def save_recipe(self, recipe):
        validate_recipe(recipe)
        recipes = [r for r in self.recipes() if (r['name'], r['project']) != (recipe['name'], recipe['project'])]
        atomic_write(self.directory / 'recipes.json', json.dumps(recipes + [recipe], indent=2, ensure_ascii=False))

    def evidence(self, record, filename):
        if filename not in record.get('evidence_hashes', {}):
            raise ValueError(f'{filename} evidence was not captured.')
        path = self.run_path(record['id']) / filename
        if hashlib.sha256(path.read_bytes()).hexdigest() != record['evidence_hashes'][filename]:
            raise ValueError(f'{filename} changed outside BuildTrail; saved evidence cannot be verified.')
        return path


def files(root, exclusions=(), cancelled=lambda: False):
    pending = [root]
    while pending:
        if cancelled():
            raise InterruptedError('Build capture cancelled.')
        folder = pending.pop()
        with os.scandir(folder) as stream:
            children = sorted(stream, key=lambda e: e.name.casefold())
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if excluded(relative, exclusions):
                continue
            info = path.stat(follow_symlinks=False)
            if is_link(info):
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                yield path, relative


def fingerprint(path, relative, cancelled=lambda: False, capture=False):
    result = {'path': relative, 'sha256': None, 'size': None, 'error': None}
    try:
        before = path.stat(follow_symlinks=False)
        if is_link(before) or not stat.S_ISREG(before.st_mode):
            raise OSError('Not a regular file.')
        digest, size, text = hashlib.sha256(), 0, bytearray()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                if cancelled():
                    raise InterruptedError('Build capture cancelled.')
                size += len(chunk)
                digest.update(chunk)
                if capture and size <= 256 * 1024:
                    text.extend(chunk)
        after = path.stat(follow_symlinks=False)
        if is_link(after) or size != before.st_size or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise OSError('File changed during hashing.')
        result.update(sha256=digest.hexdigest(), size=size)
        if capture and size <= 256 * 1024:
            try:
                result['text'] = text.decode('utf-8-sig')
            except UnicodeError:
                result['text_omitted'] = 'Not UTF-8 text.'
        elif capture:
            result['text_omitted'] = 'Larger than 256 KiB.'
    except OSError as exc:
        result['error'] = str(exc)
    return result


def output_match(relative, patterns):
    value = relative.casefold()
    for raw in patterns:
        pattern = raw.replace('\\', '/').casefold()
        variants = {pattern}
        # **/ also matches zero directories (dist/**/* includes dist/app.exe).
        pending = [pattern]
        while pending:
            item = pending.pop()
            start = item.find('**/')
            if start >= 0:
                shorter = item[:start] + item[start + 3:]
                if shorter not in variants:
                    variants.add(shorter)
                    pending.append(shorter)
        if any(fnmatch.fnmatchcase(value, p) for p in variants):
            return True
    return False


def collect_outputs(root, patterns, cancelled=lambda: False):
    return [fingerprint(path, relative, cancelled) for path, relative in files(root, ['.git/'], cancelled)
            if output_match(relative, patterns)]


def dependency_evidence(root, exclusions, cancelled):
    result, budget = [], 2 * 1024 * 1024
    for path, relative in files(root, exclusions, cancelled):
        if any(fnmatch.fnmatchcase(path.name.casefold(), p.casefold()) for p in DEPENDENCIES):
            item = fingerprint(path, relative, cancelled, capture=budget > 0)
            if 'text' in item:
                size = len(item['text'].encode('utf-8'))
                if size > budget:
                    del item['text']
                    item['text_omitted'] = 'Dependency text budget reached.'
                else:
                    budget -= size
            result.append(item)
    return result


def table_changes(before, after, before_complete=True, after_complete=True):
    old = {f['path'].casefold(): f for f in before}
    new = {f['path'].casefold(): f for f in after}
    rows = []
    for key in sorted(old.keys() | new.keys()):
        a, b = old.get(key), new.get(key)
        state = ('Unknown' if (a is None and not before_complete) or (b is None and not after_complete) or any(v and not v.get('sha256') for v in (a, b)) else
                 'Added' if a is None else 'Removed' if b is None else
                 'Unchanged' if a['sha256'] == b['sha256'] else 'Modified')
        rows.append({'path': (b or a)['path'], 'state': state, 'before': a, 'after': b})
    return rows


def command_argv(recipe):
    if recipe.get('shell') == 'argv':
        return recipe['argv']
    if os.name != 'nt':
        raise ValueError('PowerShell recipes require Windows; use argv recipes on other platforms.')
    return ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
        "$ErrorActionPreference = 'Stop'; & {\n" + recipe['command'] +
        "\n}; if ($LASTEXITCODE -is [int]) { exit $LASTEXITCODE }"]


def run_build(recipe, store, history=None, cancelled=lambda: False, output=lambda message: None):
    recipe = json.loads(json.dumps(validate_recipe(recipe)))
    root = Path(recipe['project']).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Choose a project directory.')
    history = history if history is not None else SnapshotHistory()
    for location in (store.directory, history.directory):
        if root == location or root in location.parents or location in root.parents:
            raise ValueError('Project folder overlaps BuildTrail or AppTrail history. Choose a narrower folder.')
    recipe['project'] = str(root)
    baseline = None
    if recipe.get('checkpoint_id'):
        baseline = history.load(recipe['checkpoint_id'])
        if not baseline.get('checkpoint') or history.root_key(baseline['root']) != history.root_key(str(root)):
            raise ValueError('The selected AppTrail checkpoint belongs to another project or is not a checkpoint.')
        recipe['exclusions'] = baseline['checkpoint']['exclusions']
        recipe['capture_text'] = baseline.get('text_capture', {}).get('enabled', False)
    identity = uuid.uuid4().hex
    directory = store.run_path(identity)
    directory.mkdir()
    with store.run_lock(identity):
        record = {'schema': 'buildtrail.run.v1', 'id': identity, 'recipe': recipe, 'started_at': now(),
            'started_ns': time.time_ns(), 'completed_at': None, 'status': 'unfinished', 'exit_code': None, 'warnings': [], 'tools': [],
            'dependencies': [], 'dependencies_after': [], 'outputs_before': [], 'outputs_after': [],
            'dependencies_complete': False, 'dependencies_after_complete': False,
            'outputs_before_complete': False, 'outputs_after_complete': False,
            'evidence_hashes': {}, 'machine': {'os': platform.platform(), 'architecture': platform.machine()}}
        store.save(record)
        start = time.monotonic()
        def evidence(name, content):
            atomic_write(directory / name, json.dumps(content, indent=2, ensure_ascii=False))
            record['evidence_hashes'][name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        try:
            if cancelled():
                raise InterruptedError('Build cancelled before capture.')
            output('Capturing source checkpoint…\n')
            source = snapshot_folder(root, cancelled=cancelled, capture_text=recipe['capture_text'], exclusions=recipe['exclusions'])
            source['checkpoint'] = {'name': ('Build · ' + recipe['name'])[:120],
                'exclusions': recipe['exclusions'], 'git': git_details(root)}
            evidence('source.json', source)
            record['source_id'] = history.add(source, label=source['checkpoint']['name'])
            if source['warnings'] or any(f.get('error') for f in source['files']):
                record['warnings'].append('Source capture has issues; inspect AppTrail evidence before drawing conclusions.')
            if baseline:
                evidence('baseline.json', baseline)
            output('Capturing dependency manifests and existing outputs…\n')
            record['dependencies'] = dependency_evidence(root, recipe['exclusions'], cancelled)
            record['dependencies_complete'] = True
            record['outputs_before'] = collect_outputs(root, recipe['output_patterns'], cancelled)
            record['outputs_before_complete'] = True
            for index, tool in enumerate(recipe['tools']):
                if cancelled():
                    raise InterruptedError('Build cancelled during tool probes.')
                output('Probing ' + tool[0] + '…\n')
                log = directory / f'tool-{index}.log'
                entry = {'argv': tool, 'resolved_executable': shutil.which(tool[0])}
                try:
                    entry.update(execute(tool, root, log, 10, cancelled, max_log_bytes=65536))
                    entry['output'] = log.read_text(encoding='utf-8', errors='replace')
                    record['evidence_hashes'][log.name] = hashlib.sha256(log.read_bytes()).hexdigest()
                except OSError as exc:
                    entry['error'] = str(exc)
                record['tools'].append(entry)
                if entry.get('error') or entry.get('state') == 'timed_out' or entry.get('log_truncated'):
                    record['warnings'].append('Tool probe incomplete: ' + tool[0])
            if cancelled():
                raise InterruptedError('Build cancelled before command launch.')
            record['build_started_at'] = now()
            output('Running build command…\n')
            execution = execute(command_argv(recipe), root, directory / 'build.log', recipe['timeout_seconds'], cancelled, output)
            record.update(execution)
            record['evidence_hashes']['build.log'] = hashlib.sha256((directory / 'build.log').read_bytes()).hexdigest()
            record['status'] = ('success' if execution['exit_code'] == 0 else 'failed') if execution['state'] == 'completed' else execution['state']
            output('\nCapturing outputs and post-build dependencies…\n')
            # Cancellation still preserves outputs of the stopped command; hashing remains cancellable.
            record['outputs_after'] = collect_outputs(root, recipe['output_patterns'], cancelled)
            record['outputs_after_complete'] = True
            record['dependencies_after'] = dependency_evidence(root, recipe['exclusions'], cancelled)
            record['dependencies_after_complete'] = True
            record['output_changes'] = table_changes(record['outputs_before'], record['outputs_after'])
            if not record['outputs_after']:
                record['warnings'].append('No output files matched the declared patterns.')
            if any(f.get('error') for f in record['outputs_after'] + record['outputs_before'] + record['dependencies'] + record['dependencies_after']):
                record['warnings'].append('Some file evidence could not be read or was unstable.')
            if record.get('log_truncated'):
                record['warnings'].append('Build log reached the 10 MiB capture limit; remaining output was drained but not saved.')
        except (InterruptedError, KeyboardInterrupt) as exc:
            record['status'] = 'cancelled'
            record['warnings'].append(str(exc))
        except Exception as exc:
            record['status'] = 'error'
            record['warnings'].append(str(exc))
        finally:
            log = directory / 'build.log'
            if log.exists():
                record['evidence_hashes']['build.log'] = hashlib.sha256(log.read_bytes()).hexdigest()
            record['completed_at'] = now()
            record['total_seconds'] = round(time.monotonic() - start, 3)
            store.save(record)
        return record


def source_comparison(store, before, after=None):
    from ._apptrail.core import compare
    if after is None:
        old = load_snapshot(store.evidence(before, 'baseline.json'))
        new = load_snapshot(store.evidence(before, 'source.json'))
        label = 'Linked checkpoint'
    else:
        old = load_snapshot(store.evidence(before, 'source.json'))
        new = load_snapshot(store.evidence(after, 'source.json'))
        label = run_name(before)
    result = compare(old, new)
    result['context'] = {'before': {'label': label, 'notes': before.get('organization', {}).get('notes', '') if after is not None else ''}, 'after': {'label': run_name(after or before), 'notes': (after or before).get('organization', {}).get('notes', '')}}
    return result
