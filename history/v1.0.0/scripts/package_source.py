"""Create a clean MMO source archive using an explicit file allowlist."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    '.gitignore', 'ATTRIBUTION.md', 'CHANGELOG.md', 'CONTRIBUTING.md',
    'LICENSE', 'DISTRIBUTION-LICENCE.md', 'README.md', 'mmo.py', 'mmo_core.py', 'media_tools.py',
    'support.py', 'ui_theme.py', 'requirements.txt', 'requirements-dev.txt',
)
SOURCE_TREES = {
    '.github': {'.yml', '.yaml'},
    'assets': {'.png', '.ico'},
    'docs': {'.md', '.png', '.jpg', '.jpeg', '.webp'},
    'packaging': {'.spec', '.txt'},
    'scripts': {'.py', '.ps1'},
    'tests': {'.py', '.json', '.txt'},
}
EXCLUDED_PARTS = {
    '__pycache__', '.pytest_cache', '.git', '.venv', 'venv',
    '.build', 'build', 'dist', 'data', 'vault', 'reports',
}


def source_files() -> list[Path]:
    files = []
    for relative in ROOT_FILES:
        path = PROJECT_ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f'Required project file is missing: {relative}')
        files.append(path)
    for directory, extensions in SOURCE_TREES.items():
        for path in (PROJECT_ROOT / directory).rglob('*'):
            relative = path.relative_to(PROJECT_ROOT)
            if (not path.is_file() or path.is_symlink()
                    or any(part.lower() in EXCLUDED_PARTS for part in relative.parts)
                    or path.suffix.lower() not in extensions
                    or path.name.lower() in {'settings.json', 'creds.json', 'credentials.json'}):
                continue
            files.append(path)
    return sorted(set(files), key=lambda path: path.relative_to(PROJECT_ROOT).as_posix())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=PROJECT_ROOT / 'dist' / 'MetaMovie-Organiser-Source.zip')
    args = parser.parse_args()
    files = source_files()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, 'w', compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, 'MetaMovie-Organiser/' + path.relative_to(PROJECT_ROOT).as_posix())
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(f'{digest}  {output.name}\n', encoding='ascii')
    print(f'Source archive: {output}')
    print(f'Included files: {len(files)}')
    print(f'Checksum: {output}.sha256')


if __name__ == '__main__':
    main()
