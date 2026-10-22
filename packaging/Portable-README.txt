BuildTrail 0.7.0 — Windows x64 portable edition

Source and updates: https://github.com/SeanShaffer88/BuildTrail
MIT licensed; see licenses/. Python and Tcl/Tk notices are included.

Extract the ZIP and double-click BuildTrail.exe. No Python installation,
command window, administrator access, or account is required for BuildTrail.
Your actual compiler/build tools must already be installed.

1. Choose the project folder and enter a named build recipe.
2. Enter a PowerShell build command, output globs, exclusions, and tool probes.
3. Optionally link an AppTrail checkpoint or click Save checkpoint.
4. Save the recipe, then click Run build & capture evidence.
5. In Build history, select two runs and click Compare selected (2).
6. Open the full HTML review or export it for sharing.

AppTrail source checkpoints are stored in the same per-user history used by
AppTrail. BuildTrail also keeps source/baseline copies with each run.
Builds and saved recipes: %LOCALAPPDATA%\BuildTrail
AppTrail history: %LOCALAPPDATA%\AppTrail\history

PowerShell runs without a profile and inherits BuildTrail's environment.
Tool probes execute BEFORE the build script, so set up the compiler environment
before launching BuildTrail, or put equivalent setup in each probe wrapper.
For multiple native commands, check each exit code explicitly; the default
wrapper returns the last native command's exit code. Nonterminating PowerShell
errors are treated as failures. Interactive stdin is disabled.

Compiler/dependency probes are explicit executable/argument arrays, one JSON
array per line. Example: ["cmake", "--version"] or ["cl"]. You can capture
installed package evidence with a probe such as ["python", "-m", "pip", "freeze"].
BuildTrail does not install or restore dependencies automatically.

Failed builds still retain logs and post-build output hashes. Unchanged outputs
were already on disk and are not proof of a successful rebuild. Cancellation
and timeouts stop the owned process tree. Output capture may be incomplete if
cancelled; missing evidence is not reported as a confirmed removal.
Logs are merged stdout/stderr, limited to 10 MiB; raw captured bytes are retained.
Tool probes are limited to 10 seconds and 64 KiB each.
Dependency evidence covers recognized manifests and lockfiles, not a complete
transitive or installed-package inventory. UTF-8 text capture is bounded.
Snapshots are sequential observations, not atomic backups or signed attestations.
Commands, logs, dependency files, and source snapshots can contain private data.
Review reports before sharing. Environment variable values are not inventoried.
The Windows executable is unsigned. Windows 11 x64 is the validated platform.

Organize build runs:
- Select a run and choose Edit name & notes. Details persist across restarts and
  appear in reviews and newly exported reports. Original evidence stays unchanged.
- Filter history by run name, notes, original recipe, project, date, or result.
- Move selected runs to Trash, then enable Show Trash to restore them.
- Delete permanently is available only in Trash and defaults to No confirmation.
  It removes run-owned logs/evidence/copies/notes; project files, actual build
  outputs, saved recipes, exported reports, and shared AppTrail history remain.
- Trash does not free run-file storage until permanent deletion.
- Close versions older than 0.2.0 before organizing; they do not support run locks
  or Trash. Active captures in 0.2.0+ are protected across windows.
- organization.sqlite3 contains names/notes/Trash state; include it in backups.

Side-by-side build logs:
- Select two history rows and click Compare logs (2), or use Side-by-side logs
  from Review & logs after comparing two runs. Older capture is Before.
- Original line numbers, red/green change rows, synchronized vertical scrolling,
  independent horizontal scrolling, and previous/next change controls are included.
- Search both visible logs with literal Unicode-aware case-folded matching.
  Ctrl+F focuses search; F3 / Shift+F3 jumps next/previous, wrapping at the ends.
- Previous/next issue navigates heuristic errors/warnings. Filter by severity and
  Before/After/Both logs. Ctrl+Down / Ctrl+Up also jumps between issues.
- Logs are verified against saved hashes before display. Nothing is executed or
  rewritten. Trash runs must be restored first; missing/unverified logs fail clearly.
- Preview: first 1,000,000 decoded characters and 10,000 lines per log; long lines
  show up to 20,000 characters. Search highlights up to 2,000 visible matches.
  Limits and capture truncation are shown in the window. Raw captured bytes remain.
- Terminal escape/control codes are removed for display, with line endings
  normalized. Large edited middles may align by position, explicitly noted.
- Error/warning matches are heuristics for common compiler/log formats, not a
  complete parser or a substitute for the captured exit code.

SIDE-BY-SIDE HTML EXPORT
Choose Export HTML in the log comparison window to save a standalone offline
report with search, error/warning and change navigation. Current search, filters
and theme are preserved. JavaScript enables interactive controls; no network is
used. CLI: BuildTrail.exe --data-dir LIBRARY logs BEFORE AFTER -o logs.html
Saved logs are verified again before export; preview limits are disclosed.

COLLAPSE UNCHANGED LOG LINES
Enable Collapse unchanged lines in the log window or HTML report. Chosen context
lines remain beside changes (default 3); section markers expand/collapse both panes together.
Expand all and Collapse all are available. Search/issue navigation opens hidden
results. Exports retain the collapse option and expanded sections. CLI supports
logs BEFORE AFTER --collapse (add -o logs.html for direct HTML export).

REMEMBER CONTEXT LINES
Choose 0-100 Context lines in the log window. Arrows apply immediately; press
Enter or leave the field after typing. The choice is saved between sessions in
BuildTrail preferences and preserved in HTML exports. CLI exports also reuse it;
--context-lines N overrides it for one view/export without saving. Changing
context resets individually expanded sections to the new ranges.

OTHER REMEMBERED LOG OPTIONS
Collapse mode, Match case, severity and Before/After/Both filters save immediately
when changed and restore in new log windows/sessions. HTML export preserves the
open window settings; direct CLI exports reuse saved options. CLI overrides:
--collapse / --no-collapse, --match-case / --no-match-case,
--severity all|errors|warnings, --build both|before|after. Overrides are transient.
Search text and individual expanded sections are per comparison, not saved as
preferences. Standalone reports do not modify app preferences.
