"""Self-contained, escaped HTML export of verified side-by-side saved logs."""
import base64
import hashlib
from html import escape

from . import __version__
from .logdiff import load_log, align_logs, unchanged_sections
from .organization import run_name
from .logsettings import read_context, validate_context
from ._apptrail.core import atomic_write
from ._apptrail.history import history_directory

CSS = r'''
:root{color-scheme:dark;--bg:#0c0c0c;--fg:#ccc;--panel:#161616;--border:#444;--remove:#482026;--add:#153820;--error:#ff8080;--warning:#e5c07b;--muted:#aaa}
body.light{color-scheme:light;--bg:#f3f3f3;--fg:#202020;--panel:#fff;--border:#aaa;--remove:#fbe5e8;--add:#e1f3e4;--error:#a80000;--warning:#785200;--muted:#505050}
*{box-sizing:border-box}body{margin:20px;background:var(--bg);color:var(--fg);font:15px/1.5 'Segoe UI',sans-serif}h1{margin:0}button,input,select{font:inherit;background:var(--panel);color:var(--fg);border:1px solid var(--border);padding:5px;margin:3px}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #61afef}.toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:5px}#query{width:280px}.status,footer{color:var(--muted)}.panes{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px}.pane{overflow:auto;height:65vh;border:1px solid var(--border);background:var(--panel)}h2{font-size:16px;overflow-wrap:anywhere}.line{height:24px;line-height:24px;white-space:pre;font:13px/24px Consolas,monospace;width:max-content;min-width:100%}.number{display:inline-block;width:7ch;text-align:right;margin-right:2ch;color:var(--muted);user-select:none}.before .changed{background:var(--remove)}.after .changed{background:var(--add)}.spacer{background:var(--bg)}.error .content{color:var(--error)}.warning .content{color:var(--warning)}.active-issue,.active-change{outline:1px solid #61afef;outline-offset:-1px}mark{background:#66551c;color:#fff}mark.current{background:#f2cf66;color:#101010}footer{margin-top:15px}noscript{color:var(--error)}@media print{body{color:black;background:white}.toolbar{display:none}.pane{height:auto;overflow:visible}.line{white-space:pre-wrap;height:auto;overflow-wrap:anywhere;width:auto}.panes{display:block}}
'''

CSS += '\n.fold-marker{display:none;height:24px;font:13px/24px Consolas,monospace;white-space:nowrap}.fold-marker button{margin:0;padding:0 8px;height:24px;vertical-align:top}.fold-hidden{display:none!important}\n'

SCRIPT = r'''
'use strict';
const $=id=>document.getElementById(id), body=document.body;
const panes=[$('before'),$('after')], cells=[...document.querySelectorAll('.line[data-number]')];
const entries=cells.map(el=>({el,content:el.querySelector('.content'),text:el.querySelector('.content').textContent}));
entries.sort((a,b)=>Number(a.el.dataset.row)-Number(b.el.dataset.row)|| (a.el.dataset.side==='before'?-1:1));
const issues=entries.filter(x=>x.el.dataset.severity), changes=[...document.querySelectorAll('#after .line[data-change]')];
let matches=[], mi=-1, ii=-1, ci=-1, filtered=[], timer, matchCount='0';
const groups=[...document.querySelectorAll('#before .fold-marker')].map(el=>({id:el.dataset.fold,start:Number(el.dataset.start),end:Number(el.dataset.end),expanded:el.dataset.expanded==='true'}));
function applyFolds(){const collapse=$('collapse').checked;
 for(const group of groups){const hidden=collapse&&!group.expanded;
  for(const pane of panes){const marker=pane.querySelector(`.fold-marker[data-fold="${group.id}"]`);marker.style.display=collapse?'block':'none';const button=marker.querySelector('button');button.textContent=`${hidden?'[+]':'[-]'} ${group.end-group.start+1} unchanged lines · ${hidden?'expand':'collapse'}`;button.setAttribute('aria-expanded',String(!hidden));
   for(const el of pane.querySelectorAll(`.line[data-fold="${group.id}"]`))el.classList.toggle('fold-hidden',hidden);
  }
 }
}
for(const marker of document.querySelectorAll('.fold-marker'))marker.querySelector('button').onclick=()=>{const group=groups.find(g=>g.id===marker.dataset.fold);group.expanded=!group.expanded;applyFolds();};
$('collapse').onchange=applyFolds;
for(const [id,value] of [['expand-all',true],['collapse-all',false]])$(id).onclick=()=>{$('collapse').checked=true;for(const g of groups)g.expanded=value;applyFolds();};
let syncing=false;
for(const pane of panes)pane.addEventListener('scroll',()=>{if(syncing)return;syncing=true;for(const other of panes)if(other!==pane)other.scrollTop=pane.scrollTop;syncing=false;});
function reveal(el){const group=groups.find(g=>g.id===el.dataset.fold);if($('collapse').checked&&group&&!group.expanded){group.expanded=true;applyFolds();}for(const p of panes)p.scrollTop=el.offsetTop-el.parentElement.offsetTop-p.clientHeight/3;}
function search(){clearTimeout(timer);timer=null;matches=[];mi=-1;
 const q=$('query').value, sensitive=$('case').checked, needle=sensitive?q:q.toLowerCase();let capped=false;
 for(const item of entries){item.content.replaceChildren(document.createTextNode(item.text));if(!q||capped)continue;
  const text=item.text, parts=sensitive?null:Array.from(text), boundaries=new Map([[0,0]]);let pos=0,original=0;
  if(parts)for(const c of parts){pos+=c.toLowerCase().length;original+=c.length;boundaries.set(pos,original);}
  const hay=sensitive?text:parts.map(c=>c.toLowerCase()).join('');let cursor=0,last=0,start;const fragment=document.createDocumentFragment();
  while((start=hay.indexOf(needle,cursor))!==-1){const end=start+needle.length;cursor=end;
   if(!sensitive&&(!boundaries.has(start)||!boundaries.has(end)))continue;
   if(matches.length===2000){capped=true;break;}
   const a=sensitive?start:boundaries.get(start),b=sensitive?end:boundaries.get(end);
   fragment.append(document.createTextNode(text.slice(last,a)));const mark=document.createElement('mark');mark.textContent=text.slice(a,b);fragment.append(mark);matches.push({mark,item});last=b;
  }
  fragment.append(document.createTextNode(text.slice(last)));item.content.replaceChildren(fragment);
 }
 matchCount=String(matches.length)+(capped?'+':'');
 $('search-status').textContent=q?`${matchCount} matches in preview text (including collapsed lines)`:'Ctrl+F · F3 / Shift+F3 · Literal search';
}
function moveMatch(d){if(timer)search();if(!matches.length)return;if(mi>=0)matches[mi].mark.classList.remove('current');mi=mi<0?(d>0?0:matches.length-1):(mi+d+matches.length)%matches.length;const m=matches[mi];m.mark.classList.add('current');reveal(m.item.el);const pane=m.item.el.parentElement;pane.scrollLeft=Math.max(0,m.mark.offsetLeft-pane.offsetLeft-pane.clientWidth/3);$('search-status').textContent=`Match ${mi+1}/${matchCount} · ${m.item.el.dataset.side} line ${m.item.el.dataset.number}`;}
function filterIssues(){ii=-1;for(const x of issues)x.el.classList.remove('active-issue');filtered=issues.filter(x=>(!$('severity').value||x.el.dataset.severity===$('severity').value)&&(!$('side').value||x.el.dataset.side===$('side').value));$('issue-status').textContent=`${filtered.length} error/warning matches in preview text · Heuristic detection · Ctrl+↑ / Ctrl+↓`;}
function moveIssue(d){if(!filtered.length)return;if(ii>=0)filtered[ii].el.classList.remove('active-issue');ii=ii<0?(d>0?0:filtered.length-1):(ii+d+filtered.length)%filtered.length;const x=filtered[ii];x.el.classList.add('active-issue');reveal(x.el);x.el.parentElement.scrollLeft=0;$('issue-status').textContent=`Issue ${ii+1}/${filtered.length} · ${x.el.dataset.side} line ${x.el.dataset.number} · ${x.el.dataset.severity}`;}
function moveChange(d){if(!changes.length)return;if(ci>=0)changes[ci].classList.remove('active-change');ci=ci<0?(d>0?0:changes.length-1):(ci+d+changes.length)%changes.length;changes[ci].classList.add('active-change');reveal(changes[ci]);}
$('query').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(search,200);});$('case').addEventListener('change',search);
$('clear').onclick=()=>{$('query').value='';search();};$('prev-match').onclick=()=>moveMatch(-1);$('next-match').onclick=()=>moveMatch(1);
$('severity').onchange=filterIssues;$('side').onchange=filterIssues;$('prev-issue').onclick=()=>moveIssue(-1);$('next-issue').onclick=()=>moveIssue(1);
$('prev-change').onclick=()=>moveChange(-1);$('next-change').onclick=()=>moveChange(1);$('theme').onclick=()=>body.classList.toggle('light');
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='f'){e.preventDefault();$('query').focus();$('query').select();}else if(e.key==='F3'){e.preventDefault();moveMatch(e.shiftKey?-1:1);}else if(e.ctrlKey&&(e.key==='ArrowDown'||e.key==='ArrowUp')){e.preventDefault();moveIssue(e.key==='ArrowDown'?1:-1);}});
applyFolds();search();filterIssues();
'''


def render_log_report(store, before, after, *, query='', match_case=False,
                      issue_kind='Errors & warnings', issue_side='Both logs', theme='dark',
                      collapse=False, expanded=(), context_lines=None):
    context_lines = read_context(store.directory / 'preferences.json') if context_lines is None else validate_context(context_lines)
    before, left = load_log(store, before)
    after, right = load_log(store, after)
    model = align_logs(left['lines'], right['lines'])
    e = lambda value: escape(str(value), quote=True)
    diagnostic = {(x['side'], x['row']): x['severity'] for x in model['diagnostics']}
    panes = []
    change_rows = set(model['changes'])
    folds = unchanged_sections(model['rows'], context_lines)
    starts = dict(folds)
    fold_rows = {row: start for start, end in folds for row in range(start, end + 1)}
    expanded = set(expanded)
    for side, record in (('before', before), ('after', after)):
        lines = []
        for index, row in enumerate(model['rows'], 1):
            if index in starts:
                lines.append(f'<div class="fold-marker" data-fold="{index}" data-start="{index}" data-end="{starts[index]}" data-expanded="{str(index in expanded).lower()}"><button type="button" aria-label="Toggle unchanged section starting at aligned row {index}"></button></div>')
            number = row[side + '_number']
            kind = diagnostic.get((side, index), '')
            classes = 'line ' + kind + (' changed' if row['kind'] != 'equal' else '') + (' spacer' if number is None else '')
            attrs = f'data-row="{index}" data-side="{side}"'
            if index in fold_rows:
                attrs += f' data-fold="{fold_rows[index]}"'
            if number is not None:
                attrs += f' data-number="{number}"'
            if kind:
                attrs += f' data-severity="{kind}"'
            if index in change_rows:
                attrs += ' data-change="true"'
            lines.append(f'<div class="{classes}" {attrs}><span class="number">{number or ""}</span><span class="content">{e(row[side] or "")}</span></div>')
        heading = f'{side.title()} · {run_name(record)} · {record["status"]} · exit {record["exit_code"]} · {record["started_at"]}'
        panes.append(f'<section><h2>{e(heading)}</h2><div id="{side}" class="pane {side}" tabindex="0" aria-label="{side.title()} build log">' + ''.join(lines) + '</div></section>')
    notices = [f'{side}: {note}' for side, log in (('Before', left), ('After', right)) for note in log['notices']]
    if model['positional_fallback']:
        notices.append('Large edited section aligned by position; surrounding equal lines remain anchored.')
    def options(values, selected):
        return ''.join(f'<option value="{e(value)}"'+(' selected' if label == selected else '')+f'>{e(label)}</option>' for value, label in values)
    script_hash = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-{script_hash}'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>BuildTrail · Side-by-side build logs</title><style>{CSS}</style></head><body class="{'light' if theme == 'light' else 'dark'}">
<h1>BuildTrail · Side-by-side build logs</h1><p>{len(left['lines'])} before / {len(right['lines'])} after visible lines · {len(model['changes'])} change section(s). {e(' '.join(notices))}</p>
<div class="toolbar"><label for="query">Find in both logs</label><input id="query" value="{e(query)}" type="search"><label><input id="case" type="checkbox"{' checked' if match_case else ''}>Match case</label><button id="prev-match">Previous match</button><button id="next-match">Next match</button><button id="clear">Clear</button><button id="theme">Toggle theme</button></div>
<p id="search-status" class="status" role="status"></p><div class="toolbar"><label for="severity">Severity</label><select id="severity">{options([('', 'Errors & warnings'), ('error','Errors'), ('warning','Warnings')], issue_kind)}</select><label for="side">Build</label><select id="side">{options([('', 'Both logs'), ('before','Before'), ('after','After')], issue_side)}</select><button id="prev-issue">Previous issue</button><button id="next-issue">Next issue</button><button id="prev-change">Previous change</button><button id="next-change">Next change</button></div>
<div class="toolbar"><label><input id="collapse" type="checkbox"{' checked' if collapse else ''}>Collapse unchanged lines ({context_lines} context lines)</label><button id="expand-all">Expand all</button><button id="collapse-all">Collapse all</button></div>
<p id="issue-status" class="status" role="status"></p><noscript>Enable JavaScript for search and navigation. Both logs remain readable below.</noscript><div class="panes">{''.join(panes)}</div>
<footer>BuildTrail {__version__} · Original line numbers · Times UTC · Log hashes verified at export. Saved bytes unchanged; ANSI/control codes removed for display. Search includes collapsed lines; navigating to a hidden match or issue expands its section. Search and navigation cover this bounded preview only (up to 1,000,000 characters, 10,000 lines and 20,000 characters per line per log; up to 2,000 search matches). Errors/warnings are heuristic matches. Case-insensitive search uses browser Unicode lowercasing. This standalone report uses embedded JavaScript and makes no network requests. Logs and run names may contain private information; review before sharing.</footer><script>{SCRIPT}</script></body></html>'''


def export_log_report(store, before, after, path, **settings):
    target = path.resolve()
    for location in (store.directory.resolve(), history_directory().resolve()):
        if target == location or location in target.parents:
            raise ValueError('Export reports outside managed BuildTrail and AppTrail history.')
    atomic_write(target, render_log_report(store, before, after, **settings))
