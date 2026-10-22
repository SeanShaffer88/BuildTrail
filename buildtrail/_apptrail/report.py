"""Self-contained reports. All captured strings are escaped as text."""
from html import escape
from .core import atomic_write
from . import __version__
from .textdiff import build_diff, diff_text, text_summary


def render_html(comparison):
    def e(value):
        return escape(str(value if value is not None else 'Unavailable'), quote=True)

    def file_details(entry):
        if entry is None:
            return '<em>Not captured</em>'
        pe = entry.get('pe') or {}
        fields = [('Bytes', entry['size']), ('SHA-256', entry['sha256']),
                  ('File version', entry.get('version')), ('Architecture', pe.get('architecture')),
                  ('Imports', ', '.join(pe.get('imports', [])) or None),
                  ('Delay imports', ', '.join(pe.get('delay_imports', [])) or None),
                  ('Capture error', entry.get('error')), ('Text capture', text_summary(entry))]
        return '<dl>' + ''.join(f'<dt>{e(k)}</dt><dd>{e(v)}</dd>' for k, v in fields) + '</dl>'

    cards = ''.join(f'<div class="card"><strong>{value}</strong><span>{e(key)}</span></div>'
                    for key, value in comparison['counts'].items())
    rows = []
    diff_budget = 2 * 1024 * 1024
    for change in comparison['changes']:
        if change['status'] == 'Unchanged':
            continue
        notes = '<ul>' + ''.join(f'<li>{e(n)}</li>' for n in change['notes']) + '</ul>'
        if diff_budget <= 0:
            diff_html = '<p>Text diff omitted: the report reached its 2 MiB diff budget. Use the desktop text diff view for this file.</p>'
        else:
            model = build_diff(change)
            lines = ''.join(f'<span class="diff-{line["kind"]}">{e(diff_text({"lines": [line]}, show_endings=True))}</span>' for line in model['lines'])
            if len(lines.encode('utf-8')) > diff_budget:
                diff_html = '<p>Text diff omitted: this file exceeds the remaining report diff budget. Use the desktop text diff view.</p>'
            else:
                diff_budget -= len(lines.encode('utf-8'))
                diff_html = f'<p>{e(model["message"])}</p><p><small>Before: {e(model["before_summary"])}<br>After: {e(model["after_summary"])}</small></p>'
                if lines:
                    diff_html += f'<pre class="text-diff">{lines}</pre>'
        rows.append(f'<tr><td><span class="badge">{e(change["status"])}</span></td>'
                    f'<td><strong>{e(change["path"])}</strong>{notes}<details><summary>Show saved text diff</summary>{diff_html}</details><details><summary>Show captured evidence</summary>'
                    f'<div class="evidence"><section><h3>Before</h3>{file_details(change["before"])}</section>'
                    f'<section><h3>After</h3>{file_details(change["after"])}</section></div></details></td></tr>')
    warnings = []
    for label in ('before', 'after'):
        snapshot = comparison[label]
        issues = [f'{w["path"]}: {w["message"]}' for w in snapshot['warnings']]
        issues += [f'{f["path"]}: {f["error"]}' for f in snapshot['files'] if f.get('error')]
        if issues:
            warnings.append(f'<h3>{label.title()} capture</h3><ul>' + ''.join(f'<li>{e(w)}</li>' for w in issues) + '</ul>')
    warning_html = '<aside><h2>Capture limitations</h2>' + ''.join(warnings) + '</aside>' if warnings else ''
    roots = []
    for label in ('before', 'after'):
        context = comparison.get('context', {}).get(label, {})
        name = f'<h3>{e(context["label"])}{" · Baseline" if context.get("baseline") else ""}</h3>' if context.get('label') else ''
        notes = f'<p class="capture-note">{e(context["notes"])}</p>' if context.get('notes') else ''
        checkpoint = comparison[label].get('checkpoint')
        if checkpoint:
            git = checkpoint['git']
            details = (f"{git['commit']} · {git['branch']} · {git['subject']} · {git['author']} · {git['committed_at']} · "
                       + ('uncommitted changes present' if git['dirty'] else 'clean working tree')
                       if git['status'] == 'available' else git.get('message', 'Git unavailable'))
            notes += f'<p>Git: {e(details)}</p><p>Exclusions: {e(", ".join(checkpoint["exclusions"]) or "none")}</p>'
        roots.append(f'<div><h2>{label.title()}</h2>{name}<p>{e(comparison[label]["root"])}</p>'
                     f'<small>{e(comparison[label]["started_at"])} — {e(comparison[label]["completed_at"])}</small>{notes}</div>')
    roots = ''.join(roots)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>AppTrail · Application change report</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6fa;color:#17263b;font:15px/1.6 'Segoe UI',sans-serif}}
main{{max-width:1160px;margin:auto;padding:48px 28px}}header small{{color:#366dba;letter-spacing:2px;font-weight:700}}
h1{{font-size:38px;line-height:1.2;margin:12px 0}}h2{{font-size:18px}}h3{{font-size:15px}}
.muted,small{{color:#596b80}}.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:28px 0}}
.card{{flex:1;min-width:110px;background:white;border:1px solid #dce3ee;border-radius:12px;padding:16px}}
.card strong{{display:block;font-size:30px}}.card span{{color:#596b80}}.roots,.evidence{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}
.roots{{border-top:1px solid #dce3ee;padding:12px 0 26px}}p,td,dd,li{{overflow-wrap:anywhere}}.capture-note{{white-space:pre-wrap;background:#eaf0fa;padding:12px;border-radius:8px}}
table{{width:100%;border-collapse:collapse;background:white}}th,td{{text-align:left;vertical-align:top;padding:18px;border-bottom:1px solid #dce3ee}}
th{{background:#e9eef6;font-size:13px}}td:first-child{{width:120px}}.badge{{background:#eaf0fa;border-radius:6px;padding:4px 8px;font-size:12px;font-weight:700}}
ul{{padding-left:20px}}summary{{cursor:pointer;color:#245d9e}}dt{{font-size:12px;color:#596b80;margin-top:10px}}dd{{margin:0;font-family:Consolas,monospace;font-size:12px}}
aside{{background:#fff4d9;border:1px solid #e4c677;padding:12px 22px;border-radius:12px;margin:20px 0}}
footer{{margin-top:28px;color:#596b80;font-size:13px}}@media(max-width:650px){{.roots,.evidence{{grid-template-columns:1fr}}main{{padding:24px 12px}}th,td{{padding:10px}}}}
.text-diff{{white-space:pre;overflow:auto;font:12px/1.6 Consolas,monospace;border:1px solid #dce3ee;padding:12px}}.diff-add{{background:#e6ffed;color:#14532d}}.diff-remove{{background:#ffeef0;color:#8b1a2b}}.diff-header,.diff-hunk{{color:#245d9e}}.diff-meta{{color:#785200}}
@media print{{body{{background:white}}main{{padding:0}}details{{display:block}}.card{{break-inside:avoid}}}}
</style></head><body><main><header><small>APPTRAIL / LOCAL EVIDENCE</small>
<h1>Application change report</h1><p class="muted">Generated {e(comparison['created_at'])}. Content changes are verified by SHA-256.</p></header>
<div class="cards">{cards}</div><div class="roots">{roots}</div>{warning_html}
<h2>Changes and evidence</h2><table><thead><tr><th>Result</th><th>File and observations</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="2">No differences found in captured files.</td></tr>'}</tbody></table>
<footer>AppTrail {__version__} · Times use UTC. Snapshots are sequential observations, not atomic backups.
Added/removed means present in only one capture; unreadable directories can affect those results.
Renames appear as removal and addition. Imports are static declarations, including delay imports; they do not prove a DLL is missing or was loaded.
Architecture changes are observations, not a diagnosis; managed AnyCPU behavior is not inferred.
This report may include saved source/config contents in text diffs. Review it before sharing.
Text capture is limited to 256 KiB / 3,000 lines per eligible file and 8 MiB per snapshot; report diff markup is limited to 2 MiB.
No network requests or scripts are used.</footer></main></body></html>'''


def export_report(comparison, path):
    atomic_write(path, render_html(comparison))
