"""Archive prepared native-tool sources separately from the small GitHub source ZIP."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'dist/1.1.0/MetaMovie-Organiser-NativeToolSources.zip')
    args = parser.parse_args()
    source = ROOT / 'vendor-sources'
    required = [source / 'ffprobe/ffmpeg-9.0.2.tar.xz', source / 'ffprobe/BUILD-RECIPE.txt',
                source / 'mkvtoolnix/mkvtoolnix-103.0.tar.xz', source / 'mkvtoolnix/SOURCE-ARTIFACTS.json']
    if not all(path.is_file() for path in required):
        raise SystemExit('Native source material is incomplete. Run scripts/prepare_tools.py first.')
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(path for path in source.rglob('*') if path.is_file() and not path.is_symlink()
                   and '__pycache__' not in path.parts)
    with ZipFile(output, 'w', compression=ZIP_STORED) as archive:
        for path in files:
            archive.write(path, 'NativeToolSources/' + path.relative_to(source).as_posix())
        archive.write(ROOT / 'docs/native-tool-sources.md', 'NativeToolSources/docs/native-tool-sources.md')
        archive.write(ROOT / 'packaging/native-sources.json', 'NativeToolSources/packaging/native-sources.json')
        archive.write(ROOT / 'scripts/prepare_tools.py', 'NativeToolSources/scripts/prepare_tools.py')
        archive.writestr('NativeToolSources/README.md',
            '# Native tool source materials\n\n'
            'See [the inventory and provenance](docs/native-tool-sources.md). '
            'Original archives and licence files are retained without alteration.\n\n'
            'Use this kit with the matching MMO application source archive for the full build. '
            'To rebuild only FFprobe with installed Visual Studio C++ Build Tools and Git for Windows, '
            'run `python scripts/prepare_tools.py --only ffprobe`. The included FFmpeg/GNUmake '
            'archives are reused without downloading them again.\n')
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(f'{digest}  {output.name}\n', encoding='ascii')
    print(f'Native source archive: {output} ({output.stat().st_size / 2**20:.1f} MiB)')


if __name__ == '__main__':
    main()
