import argparse
import json
from pathlib import Path
import sys

from .core import BuildStore, run_build, validate_recipe
from .report import export_report
from .organization import run_name


def main():
    # Windowed executables have no streams; legacy Windows redirected streams
    # may not encode project paths or compiler output. Keep raw logs unchanged.
    import os
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')
    parser = argparse.ArgumentParser(description='BuildTrail: build evidence alongside AppTrail checkpoints')
    parser.add_argument('--data-dir', type=Path, help='Override local BuildTrail storage')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('gui')
    run = sub.add_parser('run', help='Execute a reviewed JSON recipe and save evidence')
    run.add_argument('recipe', type=Path)
    sub.add_parser('list', help='List saved runs')
    report = sub.add_parser('report', help='Export one run, or compare two runs in the given order')
    report.add_argument('before')
    report.add_argument('after', nargs='?')
    report.add_argument('-o', '--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command in (None, 'gui'):
            if args.data_dir:
                from .gui import BuildTrail
                import tkinter as tk
                root = tk.Tk()
                BuildTrail(root, BuildStore(args.data_dir))
                root.mainloop()
            else:
                from .gui import main as gui
                gui()
            return 0
        store = BuildStore(args.data_dir)
        if args.command == 'run':
            recipe = validate_recipe(json.loads(args.recipe.read_text(encoding='utf-8')))
            record = run_build(recipe, store, output=lambda text: print(text, end='', flush=True))
            print(f"\nSaved {record['id']} · {record['status']} · exit {record['exit_code']}")
            return 0 if record['status'] == 'success' else 1
        if args.command == 'list':
            for record in store.entries():
                print(record['id'], record['status'], run_name(record), record['started_at'])
            return 0
        before = store.load(args.before)
        after = store.load(args.after) if args.after else None
        from ._apptrail.history import history_directory
        target = args.output.resolve()
        history = history_directory().resolve()
        if target == history or history in target.parents:
            raise ValueError('Export outside AppTrail history.')
        export_report(store, before, after, target)
        print('Exported:', target)
        return 0
    except (OSError, ValueError, KeyError, KeyboardInterrupt) as exc:
        print('BuildTrail:', str(exc) or 'Interrupted', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
