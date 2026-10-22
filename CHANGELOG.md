# Changelog

## 0.5.0 — 2026-10-22

- Collapse unchanged log sections with three context lines beside changes in the desktop view and standalone HTML reports.
- Expand/collapse individual sections or all sections together in both panes.
- Search and issue navigation include collapsed lines and reveal selected results automatically.
- Preserve collapse and expanded sections in HTML exports; add CLI `--collapse`. Saved evidence and original line numbers remain unchanged.

## 0.4.0 — 2026-10-21

- Export verified side-by-side build logs to a standalone offline HTML report.
- Search both logs; navigate matches, errors/warnings and changes, with severity/build filters and synchronized scrolling.
- Preserve the log window's search, filters and theme; provide CLI HTML export with `logs BEFORE AFTER -o logs.html`.
- Escape all captured content, restrict script execution with a CSP hash, and disclose bounded previews and heuristic diagnostics.

## 0.3.0 — 2026-10-21

- Side-by-side saved build logs with original line numbers, aligned change rows, synchronized scrolling, and previous/next change navigation.
- Literal Unicode-aware search across both visible logs, case matching, highlights, wraparound navigation, and Ctrl+F/F3 shortcuts.
- Heuristic error/warning highlighting and navigation with severity and Before/After/Both filters.
- Background evidence verification/alignment, explicit preview limits and positional fallback, and unchanged raw log evidence.

## 0.2.0 — 2026-10-21

- Rename saved build runs and add multiline notes without rewriting original commands, recipes, logs, source snapshots, or hashes.
- Filter names/notes and show organization details in reviews, CLI listings, source comparisons, and new HTML reports.
- Multi-select Trash, restore, and confirmed permanent deletion of owned run evidence; preserve project outputs, saved recipes, exported reports, and shared AppTrail history.
- Cross-window active-run locks and recoverable staged deletion, with explicit rejection of linked deletion targets.

## 0.1.0 — 2026-10-21

- Standalone Windows GUI and source CLI with saved build recipes and persistent run history.
- Build commands, exit/timing, compiler/tool probes, bounded merged logs, dependency manifests before/after, and output SHA-256 evidence.
- Shared AppTrail checkpoints with saved exclusions, Git details, independent evidence copies, and linked/between-build source diffs.
- Offline HTML reviews comparing successful and failed builds without claiming stale outputs were freshly produced.
- Process-tree cancellation/timeouts and bounded log capture; Terminal dark default and persistent theme toggle.
