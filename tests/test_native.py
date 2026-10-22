"""Real MSVC consumer: successful build, compiler failure, and retained EXE."""
import os
from pathlib import Path
import tempfile
import unittest

from buildtrail.core import BuildStore, default_recipe, run_build
from buildtrail._apptrail.history import SnapshotHistory


def vcvars():
    for variable in ('ProgramFiles(x86)', 'ProgramFiles'):
        root = Path(os.environ.get(variable, 'C:/missing')) / 'Microsoft Visual Studio'
        for path in root.glob('**/VC/Auxiliary/Build/vcvarsall.bat'):
            return path
    return None


@unittest.skipUnless(os.name == 'nt', 'MSVC Windows integration')
class NativeTests(unittest.TestCase):
    def test_real_msvc_success_then_compile_failure(self):
        compiler_env = vcvars()
        if compiler_env is None:
            self.skipTest('Visual Studio C++ Build Tools not installed')
        with tempfile.TemporaryDirectory(prefix='BuildTrail MSVC ') as temp:
            base = Path(temp).resolve()
            project = base / 'project'
            project.mkdir()
            (project / 'dist').mkdir()
            (project / 'main.c').write_text('#include <stdio.h>\nint main(void) { puts("BuildTrail"); return 0; }\n')
            (project / 'build.cmd').write_text(f'@echo off\ncall "{compiler_env}" x64 >nul\nif errorlevel 1 exit /b 1\ncl /nologo /W4 /WX main.c /Fo:dist/main.obj /Fe:dist/demo.exe\nexit /b %errorlevel%\n')
            (project / 'version.cmd').write_text(f'@echo off\ncall "{compiler_env}" x64 >nul\ncl\nexit /b %errorlevel%\n')
            recipe = default_recipe(project)
            recipe.update(shell='argv', argv=['cmd.exe', '/d', '/c', 'build.cmd'],
                tools=[['cmd.exe', '/d', '/c', 'version.cmd']], output_patterns=['dist/*.exe'])
            store, history = BuildStore(base / 'builds'), SnapshotHistory(base / 'snapshots')
            good = run_build(recipe, store, history)
            self.assertEqual(good['status'], 'success', good['warnings'])
            self.assertIn('Compiler Version', good['tools'][0]['output'])
            self.assertEqual(good['output_changes'][0]['state'], 'Added')
            (project / 'main.c').write_text('#error intentional BuildTrail integration failure\n')
            bad = run_build(recipe, store, history)
            self.assertEqual(bad['status'], 'failed')
            self.assertNotEqual(bad['exit_code'], 0)
            self.assertEqual(bad['output_changes'][0]['state'], 'Unchanged')
            self.assertIn('intentional BuildTrail', store.evidence(bad, 'build.log').read_text())
