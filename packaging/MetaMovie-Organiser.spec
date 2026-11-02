# Build on Windows: python -m PyInstaller packaging/MetaMovie-Organiser.spec
from pathlib import Path
import sys

from PyInstaller.utils.hooks import (
    collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata,
)

if sys.platform != 'win32':
    raise SystemExit('Build the Windows application on Windows.')

project_root = Path(SPECPATH).parent
hiddenimports = collect_submodules('keyring.backends', on_error='ignore')
hiddenimports += [
    'blake3', 'xxhash', 'tvdb_provider', 'collections_tools', 'cryptography.hazmat.primitives.ciphers.aead',
    'PIL.ImageTk', 'pymediainfo', 'mutagen.mp4',
]

# The Windows pymediainfo wheel supplies MediaInfo.dll next to its module.
binaries = collect_dynamic_libs('pymediainfo')
if not any(Path(source).name.lower() == 'mediainfo.dll' for source, _ in binaries):
    raise SystemExit('MediaInfo.dll is missing. Install the Windows pymediainfo wheel before building.')

datas = [(str(project_root / 'assets'), 'assets')]
for relative in ('mediainfo/MediaInfo.exe', 'ffprobe/ffprobe.exe',
                 'mkvpropedit/mkvpropedit.exe', 'mkvpropedit/mkvextract.exe'):
    if not (project_root / 'tools' / relative).is_file():
        raise SystemExit('Bundled media tools are missing. Run python scripts/prepare_tools.py before building.')
datas += [(str(project_root / 'tools'), 'tools')]
datas += collect_data_files('pymediainfo', includes=['License.html'])
# Retain the pure-Python tag library source alongside its distribution licence.
datas += collect_data_files('mutagen', include_py_files=True)
for distribution in ('requests', 'blake3', 'keyring', 'cryptography', 'Pillow', 'pymediainfo', 'mutagen'):
    datas += copy_metadata(distribution)

analysis = Analysis(
    [str(project_root / 'mmo.py')],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(project_root / 'packaging' / 'hooks')],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name='MetaMovie-Organiser',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(project_root / 'assets' / 'MMO.ico'),
    version=str(project_root / 'packaging' / 'version_info.txt'),
    contents_directory='_internal',
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name='MetaMovie-Organiser',
)
