"""Retain dependency licences and matching Mutagen source in a Windows build."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import shutil
import sys
from zipfile import ZipFile

from packaging.requirements import Requirement

DISTRIBUTIONS = ('requests', 'blake3', 'keyring', 'cryptography', 'Pillow', 'pymediainfo', 'mutagen')


def installed_dependencies():
    pending = list(DISTRIBUTIONS) + ['pyinstaller']
    seen = set()
    while pending:
        name = pending.pop()
        distribution = metadata.distribution(name)
        canonical = distribution.metadata['Name'].lower().replace('_', '-')
        if canonical in seen:
            continue
        seen.add(canonical)
        yield distribution
        # Include only dependencies active for this build, without optional extras.
        for text in distribution.requires or ():
            requirement = Requirement(text)
            if requirement.marker is None or requirement.marker.evaluate({'extra': ''}):
                pending.append(requirement.name)


def collect(output: Path, source_archive: Path) -> None:
    output = output.resolve()
    third_party = output / 'third_party'
    for distribution in installed_dependencies():
        name = distribution.metadata['Name']
        count = 0
        for relative in distribution.files or ():
            parts = Path(relative).parts
            if 'licenses' in parts:
                suffix = Path(*parts[parts.index('licenses') + 1:])
            elif (any(part.endswith('.dist-info') for part in parts)
                  and Path(relative).name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'AUTHORS'))):
                suffix = Path(relative).name
            else:
                continue
            source = Path(distribution.locate_file(relative))
            if source.is_file():
                destination = third_party / 'licenses' / name.lower() / suffix
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                count += 1
        if not count:
            raise RuntimeError(f'No installed licence material found for {name}; inspect this dependency before distributing.')

    python_licence = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_licence.is_file():
        destination = third_party / 'licenses' / 'python' / 'LICENSE.txt'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(python_licence, destination)

    distribution = metadata.distribution('mutagen')
    source_root = third_party / 'source' / f'mutagen-{distribution.version}'
    count = 0
    for relative in distribution.files or ():
        path = Path(relative)
        if not path.parts or path.parts[0] != 'mutagen' or path.suffix != '.py':
            continue
        source = Path(distribution.locate_file(relative))
        destination = source_root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        count += 1
    if not count:
        raise RuntimeError('Installed Mutagen Python source was not found.')
    shutil.copy2(third_party / 'licenses' / 'mutagen' / 'COPYING', source_root / 'COPYING')

    # MediaInfo's native-library licence is provided inside the wheel package.
    distribution = metadata.distribution('pymediainfo')
    native_licence = Path(distribution.locate_file('pymediainfo/License.html'))
    if not native_licence.is_file():
        raise RuntimeError('MediaInfo native-library License.html was not found.')
    shutil.copy2(native_licence, third_party / 'licenses' / 'pymediainfo' / 'License.html')

    source_root = third_party / 'source'
    source_root.mkdir(parents=True, exist_ok=True)
    with ZipFile(source_archive) as archive:
        for entry in archive.infolist():
            destination = (source_root / entry.filename).resolve()
            if not destination.is_relative_to(source_root.resolve()):
                raise ValueError('The source archive contains a path outside the source directory.')
        archive.extractall(source_root)
    print(f'Retained dependency licences, {count} Mutagen source files and the application source in {third_party}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('source_archive', type=Path)
    args = parser.parse_args()
    collect(args.output, args.source_archive)


if __name__ == '__main__':
    main()
