# BuildTrail development

- Keep runtime standard-library only unless a concrete requirement justifies a dependency.
- Run `python -m unittest discover -s tests -v`. Windows integration covers Tk, PowerShell, owned process trees, and MSVC when installed. Browser checks use `tests/make_report_fixture.py` and `tests/report_browser.cjs`.
- Recipes and probes are executable user configuration. Loading, importing, viewing, or reporting must never execute them.
- Do not classify unchanged outputs as freshly built, absent incomplete evidence as confirmed removal, or manifest files as a complete installed dependency graph.
- Preserve AppTrail's shared history and snapshot compatibility. Vendored provenance is in THIRD-PARTY-NOTICES.md; document any modifications there and test cross-app behavior.
- Release Windows GUI builds with the pinned packaging script and license notices. Default CI builds should not upload redundant storage artifacts.
