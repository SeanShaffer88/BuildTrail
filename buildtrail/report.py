"""Escaped, offline HTML reports combining build and source observations."""
import difflib
from html import escape
import json

from . import __version__
from .core import table_changes, source_comparison
from .organization import run_name
from ._apptrail.core import atomic_write
from ._apptrail.textdiff import build_diff, diff_text


def render_report(store, before, after=None):
    def e(value):
        return escape(str(value if value is not None else 'Unavailable'), quote=True)
    def section(title, body):
        return f'<section><h2>{e(title)}</h2>{body}</section>'
    def pre(value):
        return '<pre>' + e(value) + '</pre>'
    def table(rows, columns):
        head = ''.join(f'<th>{e(c)}</th>' for c in columns)
        body = ''.join('<tr>' + ''.join(f'<td>{e(v)}</td>' for v in row) + '</tr>' for row in rows)
        return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body or "<tr><td>No evidence in this section.</td></tr>"}</tbody></table></div>'
    def hashes(rows):
        return table([(r['state'], r['path'], (r['before'] or {}).get('sha256'),
                       (r['after'] or {}).get('sha256'), (r['after'] or r['before']).get('error') or '') for r in rows],
                     ['Change', 'File', 'Before SHA-256', 'After SHA-256', 'Capture issue'])
    def source_html(comparison):
        result = pre(' · '.join(f'{key}: {value}' for key, value in comparison['counts'].items()))
        for side in ('before', 'after'):
            snapshot = comparison[side]
            checkpoint = snapshot.get('checkpoint', {})
            result += f'<h3>{side.title()} · {e(checkpoint.get("name", "Source"))}</h3>'
            result += pre(json.dumps({'root': snapshot['root'], 'captured_at': snapshot['completed_at'],
                'git': checkpoint.get('git'), 'exclusions': checkpoint.get('exclusions'),
                'warnings': snapshot['warnings']}, indent=2, ensure_ascii=False))
        budget = 512 * 1024
        for change in comparison['changes']:
            if change['status'] == 'Unchanged':
                continue
            result += f'<details><summary>{e(change["status"])} · {e(change["path"])}</summary>'
            result += pre('\n'.join(change['notes']))
            model = build_diff(change)
            text = diff_text(model, show_endings=True)
            if len(text.encode('utf-8')) <= budget:
                budget -= len(text.encode('utf-8'))
                result += pre(model['message'] + '\n' + text)
            else:
                result += '<p>Source diff text omitted: 512 KiB report budget reached.</p>'
            result += '</details>'
        return result

    parts = []
    runs = [('Before build', before), ('After build', after)] if after else [('Build', before)]
    for title, run in runs:
        info = {key: run.get(key) for key in ('id', 'status', 'exit_code', 'started_at', 'build_started_at',
            'completed_at', 'duration_seconds', 'total_seconds', 'machine', 'source_id', 'warnings',
            'outputs_before_complete', 'outputs_after_complete', 'dependencies_complete', 'dependencies_after_complete')}
        info['run_name'] = run_name(run)
        info['notes'] = run.get('organization', {}).get('notes', '')
        info['recipe'] = run['recipe']
        parts.append(section(title, pre(json.dumps(info, ensure_ascii=False, indent=2))))
        parts.append(section(title + ' · tool/compiler probes', pre(json.dumps(run['tools'], ensure_ascii=False, indent=2))))
        parts.append(section(title + ' · outputs created or changed during this run',
            hashes(run.get('output_changes', [])) + '<p>Unchanged files were already present; they are not proof that this run produced them.</p>'))
        deps = table_changes(run.get('dependencies', []), run.get('dependencies_after', []), run['dependencies_complete'], run['dependencies_after_complete'])
        parts.append(section(title + ' · dependency manifests before / after command', hashes(deps)))
        if 'build.log' in run.get('evidence_hashes', {}):
            try:
                log = store.evidence(run, 'build.log').read_text(encoding='utf-8', errors='replace')
                parts.append(section(title + ' · build log', '<p>Preview limited to 200,000 characters; full captured log is stored with the build.</p>' + pre(log[:200000])))
            except (OSError, ValueError) as exc:
                parts.append(section(title + ' · build log unavailable', pre(str(exc))))
        if 'baseline.json' in run.get('evidence_hashes', {}):
            try:
                parts.append(section(title + ' · linked AppTrail checkpoint → build source', source_html(source_comparison(store, run))))
            except (OSError, ValueError) as exc:
                parts.append(section(title + ' · checkpoint evidence unavailable', pre(str(exc))))
    if after:
        if before['recipe']['project'].casefold() != after['recipe']['project'].casefold():
            parts.insert(0, section('Different projects', '<p>These runs used different project folders. File matching is by relative path.</p>'))
        parts.append(section('Output differences between builds', hashes(table_changes(before['outputs_after'], after['outputs_after'], before['outputs_after_complete'], after['outputs_after_complete']))))
        # Use dependency evidence at build start to explain the inputs of each command.
        deps = table_changes(before['dependencies'], after['dependencies'], before['dependencies_complete'], after['dependencies_complete'])
        dep_html = hashes(deps)
        budget = 256 * 1024
        for row in deps:
            if row['state'] == 'Unchanged':
                continue
            old, new = row['before'] or {}, row['after'] or {}
            if 'text' in old or 'text' in new:
                diff = ''.join(difflib.unified_diff(old.get('text', '').splitlines(True), new.get('text', '').splitlines(True), fromfile='before/' + row['path'], tofile='after/' + row['path']))
                size = len(diff.encode('utf-8'))
                if size <= budget:
                    dep_html += '<details><summary>' + e(row['path']) + '</summary>' + pre(diff) + '</details>'
                    budget -= size
                else:
                    dep_html += '<p>Dependency text diff omitted: report budget reached.</p>'
        parts.append(section('Dependency input differences between builds', dep_html))
        def tool_signature(run):
            return {json.dumps(tool['argv']): {k: tool.get(k) for k in ('output', 'exit_code', 'state', 'error', 'log_truncated')} for tool in run['tools']}
        old, new = tool_signature(before), tool_signature(after)
        parts.append(section('Tool/compiler differences between builds', table([
            (key, 'Unchanged' if old.get(key) == new.get(key) else 'Changed',
             json.dumps(old.get(key), ensure_ascii=False), json.dumps(new.get(key), ensure_ascii=False)) for key in sorted(old.keys() | new.keys())],
            ['Probe', 'Result', 'Before', 'After'])))
        try:
            parts.append(section('AppTrail source changes between builds', source_html(source_comparison(store, before, after))))
        except (OSError, ValueError) as exc:
            parts.append(section('Source comparison unavailable', pre(str(exc))))
        try:
            a = store.evidence(before, 'build.log').read_text(encoding='utf-8', errors='replace')[:200000]
            b = store.evidence(after, 'build.log').read_text(encoding='utf-8', errors='replace')[:200000]
            diff = list(difflib.unified_diff(a.splitlines(True), b.splitlines(True), fromfile='before.log', tofile='after.log'))
            parts.append(section('Build log differences', '<p>Compared the first 200,000 characters per log; showing at most 300 diff lines.</p>' + pre(''.join(diff[:300]))))
        except (OSError, ValueError) as exc:
            parts.append(section('Log comparison unavailable', pre(str(exc))))
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>BuildTrail · Build evidence</title><style>
*{box-sizing:border-box}body{background:#0c0c0c;color:#ccc;font:15px/1.6 'Segoe UI',sans-serif;margin:0}main{max-width:1250px;margin:auto;padding:36px 24px}h1{font-size:36px;color:#eee}h2{color:#82aaff;font-size:21px}h3{font-size:16px}section{border:1px solid #333;background:#161616;padding:20px;margin:20px 0;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 Consolas,monospace}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;vertical-align:top;border-bottom:1px solid #333;padding:10px;overflow-wrap:anywhere;max-width:400px}.scroll{overflow:auto}th{color:#8fbf87}summary{cursor:pointer;color:#76d7e4}details{padding:10px 0}footer{color:#aaa}@media print{body,section{background:white;color:black}h1,h2,h3,th,summary{color:black}details{display:block}}
</style></head><body><main><h1>BuildTrail · Build evidence</h1><p>Saved build commands, compiler/tool observations, dependency manifests, output hashes, and AppTrail source checkpoints.</p>''' + ''.join(parts) + f'''<footer>BuildTrail {__version__} · Times are UTC. Merged logs use UTF-8 replacement decoding. Manifest evidence is not a complete installed/transitive dependency inventory. Tool probes run in the recipe's inherited environment before the build command; environment changes made inside the command are not reflected in earlier probes. Snapshots are sequential observations, not atomic backups. Exit code zero does not guarantee correct or newly produced artifacts. Commands and logs may contain private information; review before sharing. No scripts or network requests are used by this report.</footer></main></body></html>'''


def export_report(store, before, after, path):
    target = path.resolve()
    for location in (store.directory,):
        if target == location or location in target.parents:
            raise ValueError('Export reports outside the managed BuildTrail library.')
    atomic_write(target, render_report(store, before, after))
