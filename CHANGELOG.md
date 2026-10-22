# Changelog

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
