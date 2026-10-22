"""Build a standalone Windows GUI release using pinned PyInstaller tooling.

Run with a build-only Python environment containing requirements-build.txt.
All intermediate files are directed to --work-dir.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib


def make_icon(path):
    """Draw a simple trail mark directly into a multi-resolution Windows icon."""
    images = []
    for size in (16, 32, 48, 64, 128, 256):
        raw = bytearray()
        points = ((.25, .76), (.43, .59), (.36, .42), (.66, .23), (.76, .23))
        for y in range(size):
            raw.append(0)
            for x in range(size):
                u, v = (x + .5) / size, (y + .5) / size
                distance = 10.0
                for (ax, ay), (bx, by) in zip(points, points[1:]):
                    dx, dy = bx - ax, by - ay
                    t = max(0, min(1, ((u - ax) * dx + (v - ay) * dy) / (dx * dx + dy * dy)))
                    distance = min(distance, ((u - ax - t * dx) ** 2 + (v - ay - t * dy) ** 2) ** .5)
                raw.extend((231, 245, 255, 255) if distance < .047 else (32, 84, 146, 255))

        def chunk(kind, value):
            return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value))
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
        png += chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
        images.append((size, png))
    offset = 6 + 16 * len(images)
    header = bytearray(struct.pack('<HHH', 0, 1, len(images)))
    for size, png in images:
        header.extend(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(png), offset))
        offset += len(png)
    path.write_bytes(header + b''.join(png for _, png in images))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32' or struct.calcsize('P') != 8:
        parser.error('Build on 64-bit Windows using 64-bit Python.')
    from importlib.metadata import version
    if version('pyinstaller') != '6.22.3':
        parser.error('Install the pinned packaging/requirements-build.txt first.')
    source = Path(__file__).resolve().parent.parent
    work, output = args.work_dir.resolve(), args.output_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    licenses = work / 'licenses'
    licenses.mkdir(exist_ok=True)
    # CPython's combined license includes Python, Tcl/Tk and bundled third-party notices.
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        parser.error('The Python distribution LICENSE.txt is required for redistribution.')
    shutil.copy2(python_license, licenses / 'Python-and-bundled-components.txt')
    shutil.copy2(source / 'LICENSE', licenses / 'BuildTrail-MIT.txt')
    shutil.copy2(source / 'THIRD-PARTY-NOTICES.md', licenses / 'THIRD-PARTY-NOTICES.md')
    icon = work / 'BuildTrail.ico'
    make_icon(icon)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
        '--noupx', '--name', 'BuildTrail', '--icon', str(icon),
        '--version-file', str(source / 'packaging' / 'version.txt'),
        '--paths', str(source), '--distpath', str(output), '--workpath', str(work / 'build'),
        '--specpath', str(work), '--add-data', f'{licenses}:licenses',
        str(source / 'Launch-BuildTrail.pyw'),
    ], check=True, cwd=source)
    shutil.copytree(licenses, output / 'licenses', dirs_exist_ok=True)
    shutil.copy2(source / 'packaging' / 'Portable-README.txt', output / 'README.txt')
    executable = output / 'BuildTrail.exe'
    checksum = hashlib.sha256(executable.read_bytes()).hexdigest()
    (output / 'SHA256SUMS.txt').write_text(f'{checksum}  BuildTrail.exe\n', encoding='utf-8')
    sys.path.insert(0, str(source))
    from buildtrail import __version__
    manifest = {'app': 'BuildTrail', 'version': __version__, 'target': 'Windows x64',
                'python': sys.version, 'pyinstaller': version('pyinstaller'),
                'sha256': checksum, 'bytes': executable.stat().st_size}
    (output / 'build-info.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    archive = output.parent / f'BuildTrail-{__version__}-Windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(output.rglob('*')):
            if path.is_file():
                bundle.write(path, Path('BuildTrail') / path.relative_to(output))
    print(f'Executable: {executable}\nPortable ZIP: {archive}\nSHA-256: {checksum}')


if __name__ == '__main__':
    main()
