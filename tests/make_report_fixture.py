"""Create two synthetic real runs and a portable HTML report for browser checks."""
import sys
from pathlib import Path
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buildtrail.core import BuildStore, default_recipe, run_build
from buildtrail._apptrail.history import SnapshotHistory
from buildtrail._apptrail.checkpoints import capture_checkpoint
from buildtrail.report import render_report


def main():
    target = Path(sys.argv[1]).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='BuildTrail-demo-') as temp:
        base = Path(temp).resolve()
        project = base / 'demo'
        project.mkdir()
        (project / 'source.py').write_text('value = 1\n')
        (project / 'requirements.txt').write_text('demo-package==1.0\n')
        store, history = BuildStore(base / 'builds'), SnapshotHistory(base / 'history')
        checkpoint, _ = capture_checkpoint(history, project, 'Before feature', ['dist/'])
        recipe = default_recipe(project)
        recipe.update(shell='argv', argv=[sys.executable, '-c', "from pathlib import Path; Path('dist').mkdir(); Path('dist/demo.exe').write_bytes(b'demo artifact'); print('build succeeded'); print('warning: deprecated'); print('😀 a+b SHARED'); print('İstanbul'); print('x ' * 2100)"],
            tools=[[sys.executable, '--version']], output_patterns=['dist/**/*'], checkpoint_id=checkpoint)
        good = run_build(recipe, store, history)
        (project / 'source.py').write_text('value = broken\n')
        (project / 'requirements.txt').write_text('demo-package==2.0\n')
        recipe['argv'] = [sys.executable, '-c', "import sys; print('compiler error: broken value'); print('warning: deprecated'); print('😀 a+b shared'); print('İstanbul'); print('x ' * 2100); print('<script>window.buildtrailInjected = true</script>'); sys.exit(2)"]
        bad = run_build(recipe, store, history)
        assert good['status'] == 'success' and bad['status'] == 'failed'
        store.update_metadata(good['id'], 'Known-good release', 'Smoke tests passed.\nKeep as the release reference.')
        store.update_metadata(bad['id'], 'Feature branch compile failure', 'Investigating issue #42.\nPrevious output remains on disk.')
        from buildtrail.logreport import export_log_report
        export_log_report(store, good, bad, target.with_suffix('.logs.html'), query='a+b', match_case=True,
            issue_kind='Warnings', issue_side='After', theme='light')
        report = render_report(store, store.load(good['id']), store.load(bad['id']))
        # Public demo only: normalize temporary project/interpreter paths.
        from html import escape
        report = report.replace(escape(str(base), quote=True), 'C:/BuildTrail-Demo')
        report = report.replace(escape(str(base).replace('\\', '\\\\'), quote=True), 'C:/BuildTrail-Demo')
        report = report.replace(escape(sys.executable, quote=True), 'python.exe')
        report = report.replace(escape(sys.executable.replace('\\', '\\\\'), quote=True), 'python.exe')
        target.write_text(report.replace('\r\n', '\n').replace('\r', '\n'), encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
