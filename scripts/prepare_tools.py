"""Prepare pinned Windows media tools and retain their source/licence materials.

All downloads and builds stay inside this project. No tools are installed into
the system PATH. FFprobe is built from unmodified FFmpeg source with optional
external libraries disabled, using an installed Visual Studio C++ compiler.
"""

from __future__ import annotations

import argparse
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import ssl
import tarfile
import time
import zlib
from urllib.request import Request, urlopen
from urllib.error import URLError
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'build' / 'tool-downloads'
TOOLS = ROOT / 'tools'
SOURCES = ROOT / 'vendor-sources'
ARTIFACTS = {
    'mediainfo': {
        'version': '26.10',
        'url': 'https://mediaarea.net/download/binary/mediainfo/26.10/MediaInfo_CLI_26.10_Windows_x64.zip',
        'sha256': '4cc94bd272903914c04f4f9a9f53a6c94b5ea6c6454cf738d29fa182c41e8a29',
        'hash_basis': 'recorded from official HTTPS download; no publisher checksum located',
    },
    'mkvpropedit': {
        'version': '103.0',
        'url': 'https://mkvtoolnix.download/windows/releases/103.0/mkvtoolnix-64-bit-103.0.zip',
        'sha256': 'c860a10c707cb1ee587d1464f6171842f2ada3bb6f031dfff55fec75542f0417',
        'hash_basis': 'publisher SHA-256',
    },
    'mkv-source': {
        'version': '103.0',
        'url': 'https://mkvtoolnix.download/sources/mkvtoolnix-103.0.tar.xz',
        'sha256': 'd609bc395c409eade05d0a3a5aedd26e09739e232ed03b0f937cf8bd9448c590',
        'hash_basis': 'publisher SHA-256',
    },
    'ffmpeg-source': {
        'version': '9.0.2', 'url': 'https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz',
        'sha256': '8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e',
        'hash_basis': 'recorded from official HTTPS download',
    },
    'make-source': {
        'version': '4.4.1', 'url': 'https://ftp.gnu.org/gnu/make/make-4.4.1.tar.gz',
        'sha256': 'dd16fb1d67bfab79a72f5e8390735c49e3e8e70b4945a15ab1f81ddb78658fb3',
        'hash_basis': 'recorded from official HTTPS download',
    },
}
FFMPEG_FLAGS = [
    '--toolchain=msvc', '--disable-autodetect', '--disable-network',
    '--disable-doc', '--disable-debug', '--disable-x86asm', '--disable-hwaccels',
    '--disable-encoders', '--disable-muxers', '--disable-avdevice',
    '--disable-avfilter', '--disable-swscale', '--disable-swresample',
    '--disable-programs', '--enable-ffprobe', '--enable-w32threads',
    '--disable-pthreads', '--disable-stripping', '--extra-cflags=-MT',
]


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _download_artifact_once(artifact):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / artifact['url'].rsplit('/', 1)[-1]
    if path.exists() and sha256(path) != artifact['sha256']:
        path.unlink()  # Discard an invalid cache entry before fetching again.
    if not path.exists():
        for directory in (ROOT / 'ffprobe', ROOT / 'mkvtoolnix', SOURCES / 'ffprobe', SOURCES / 'mkvtoolnix'):
            retained = directory / path.name
            if retained.is_file() and sha256(retained) == artifact['sha256']:
                shutil.copy2(retained, path)
                break
    if not path.exists():
        print('Downloading ' + path.name, flush=True)
        temporary = path.with_suffix(path.suffix + '.part')
        size = artifact.get('estimated_bytes', 0)
        with temporary.open('wb') as output:
            if size > 32 * 1024 * 1024:
                # Explicit ranges avoid CDN stalls on very large source files.
                for offset in range(0, size, 4 * 1024 * 1024):
                    end = min(size - 1, offset + 4 * 1024 * 1024 - 1)
                    request = Request(artifact['url'], headers={'Range': f'bytes={offset}-{end}'})
                    with urlopen(request, timeout=45) as response:
                        if response.status != 206:
                            if offset:
                                raise RuntimeError('The source server stopped supporting range downloads.')
                            while block := response.read(256 * 1024):
                                output.write(block)
                            break
                        block = response.read(end - offset + 1)
                        if len(block) != end - offset + 1:
                            raise RuntimeError('A ranged source download was incomplete.')
                        output.write(block)
            else:
                with urlopen(artifact['url'], timeout=45) as response:
                    while block := response.read(256 * 1024):
                        output.write(block)
        actual = sha256(temporary)
        if actual != artifact['sha256']:
            temporary.unlink()
            raise RuntimeError(
                f"SHA-256 mismatch: {path.name}; expected {artifact['sha256']}, got {actual}"
            )
        temporary.replace(path)
    if sha256(path) != artifact['sha256']:
        raise RuntimeError('SHA-256 mismatch: ' + path.name)
    return path


def download_artifact(artifact):
    for attempt in range(3):
        try:
            return _download_artifact_once(artifact)
        except (URLError, TimeoutError, ConnectionError, IncompleteRead, ssl.SSLError):
            temporary = CACHE / (artifact['url'].rsplit('/', 1)[-1] + '.part')
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            print(f'Temporary download failure; retrying {artifact["url"]} '
                  f'({attempt + 2}/3)', flush=True)
            time.sleep(2 ** (attempt + 1))


def download(name):
    return download_artifact(ARTIFACTS[name])


def unzip(archive, destination, select=None, prefix=''):
    destination.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive) as source:
        for item in source.infolist():
            if item.is_dir() or (select and not select(item.filename)):
                continue
            if prefix and not item.filename.startswith(prefix):
                continue
            relative = item.filename[len(prefix):]
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError('Unsafe archive entry')
            if target.is_file() and target.stat().st_size == item.file_size:
                checksum = 0
                with target.open('rb') as existing:
                    while block := existing.read(1024 * 1024):
                        checksum = zlib.crc32(block, checksum)
                if checksum == item.CRC:
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as origin, target.open('wb') as output:
                shutil.copyfileobj(origin, output)


def untar(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        for item in source.getmembers():
            target = (destination / item.name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError('Unsafe source archive entry')
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(item) as origin, target.open('wb') as output:
                    shutil.copyfileobj(origin, output)
                target.chmod(item.mode)
            # Source symlinks are unnecessary for these in-source Windows builds.


def run(command, *, cwd=None, env=None, log=None):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with Path(log).open('w', encoding='utf-8') as output:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=output,
                                    stderr=subprocess.STDOUT, creationflags=flags)
        if result.returncode:
            raise RuntimeError(f'Native build command failed; inspect {Path(log).relative_to(ROOT)}')
        return ''
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                            text=True, errors='replace', creationflags=flags)
    if result.returncode:
        raise RuntimeError('Native tool command failed: ' + str(command[0]))
    return result.stdout + result.stderr


def msvc_environment():
    vswhere = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    if not vswhere.is_file():
        raise RuntimeError('Install Visual Studio C++ Build Tools to compile the bundled FFprobe.')
    path = run([str(vswhere), '-latest', '-products', '*', '-requires',
                'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath']).strip()
    script = Path(path) / 'VC/Auxiliary/Build/vcvars64.bat'
    if not script.is_file():
        raise RuntimeError('A Visual Studio x64 C++ environment was not found.')
    # Capture only into this child environment. Never print or save the user's
    # environment, which may contain unrelated credentials.
    command = f'cmd.exe /d /s /c "call "{script}" >nul && set"'
    result = subprocess.run(command,
                            capture_output=True, text=True, errors='replace',
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Visual Studio environment setup failed.')
    environment = dict(os.environ)
    for line in result.stdout.splitlines():
        name, separator, value = line.partition('=')
        if separator and name:
            environment[name] = value
    return environment


def git_shell(explicit=None):
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return candidate
    git = shutil.which('git')
    candidates = [Path(git).resolve().parents[1] / 'usr/bin/sh.exe'] if git else []
    candidates += [Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git/usr/bin/sh.exe']
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError('Git for Windows shell not found. Pass --shell with its usr/bin/sh.exe path.')


def posix_path(path):
    path = Path(path).resolve()
    return '/' + path.drive[0].lower() + path.as_posix()[2:]


def retain(archive, directory):
    destination = SOURCES / directory
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, destination / Path(archive).name)


def prepare_ffprobe(shell=None):
    source_archive = download('ffmpeg-source')
    make_archive = download('make-source')
    source_root = ROOT / 'build/native-src'
    ffmpeg = source_root / 'ffmpeg-9.0.2'
    make_root = source_root / 'make-4.4.1'
    if not ffmpeg.exists():
        untar(source_archive, source_root)
    if not make_root.exists():
        untar(make_archive, source_root)
    environment = msvc_environment()
    make = make_root / 'WinRel/gnumake.exe'
    # The upstream batch build recreates WinRel. Resolve it within our known
    # project build tree before invoking that upstream build script.
    if not make.parent.resolve().is_relative_to((ROOT / 'build').resolve()):
        raise ValueError('The make build directory is outside this project.')
    if not make.is_file():
        print('Building local GNU make 4.4.1', flush=True)
        run(['cmd.exe', '/d', '/c', 'build_w32.bat', '--without-guile'],
            cwd=make_root, env=environment, log=ROOT / 'build/native-logs/make.log')
    shell = git_shell(shell)
    vc_tools = Path(environment.get('VCToolsInstallDir', '')) / 'bin/Hostx64/x64'
    path_prefix = ':'.join([posix_path(vc_tools), posix_path(make.parent), posix_path(shell.parent)])
    export = 'export PATH=' + shlex.quote(path_prefix) + ':"$PATH"; '
    executable = ffmpeg / 'ffprobe.exe'
    if not executable.is_file():
        config = ffmpeg / 'ffbuild/config.mak'
        configured = config.is_file() and all(flag in config.read_text() for flag in FFMPEG_FLAGS)
        if not configured:
            print('Configuring minimal FFprobe 9.0.2', flush=True)
            run([str(shell), '-c', export + './configure ' + shlex.join(FFMPEG_FLAGS)],
                cwd=ffmpeg, env=environment, log=ROOT / 'build/native-logs/ffmpeg-configure.log')
        print('Building FFprobe without optional external libraries', flush=True)
        run([str(shell), '-c', export + shlex.quote(posix_path(make)) +
             ' ' + shlex.quote('SHELL=' + shell.as_posix()) +
             ' CCDEP= CXXDEP= ASDEP= HOSTCCDEP=' +
             f' -j{min(8, os.cpu_count() or 2)} ffprobe.exe'], cwd=ffmpeg, env=environment,
            log=ROOT / 'build/native-logs/ffmpeg-build.log')
    destination = TOOLS / 'ffprobe'
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / 'ffprobe.exe'
    if not target.exists() or sha256(executable) != sha256(target):
        shutil.copy2(executable, target)
    version = run([str(destination / 'ffprobe.exe'), '-version'])
    if not version.startswith('ffprobe version 9.0.2'):
        raise RuntimeError('Built FFprobe version did not match the pinned source.')
    for name in ('COPYING.LGPLv2.1', 'COPYING.LGPLv3', 'LICENSE.md'):
        if (ffmpeg / name).exists():
            shutil.copy2(ffmpeg / name, destination / name)
    retain(source_archive, 'ffprobe')
    retain(make_archive, 'ffprobe')
    shutil.copy2(Path(__file__), SOURCES / 'ffprobe' / 'prepare_tools.py')
    (SOURCES / 'ffprobe' / 'BUILD-RECIPE.txt').write_text(
        'Unmodified FFmpeg 9.0.2 and GNU make 4.4.1 sources.\n'
        'Windows x64; Visual Studio C++ Build Tools; Git for Windows shell.\n'
        'GNU make: build_w32.bat --without-guile\n'
        'FFmpeg in-source: ./configure ' + shlex.join(FFMPEG_FLAGS) + '\n'
        'Then gnumake SHELL=<Git usr/bin/sh.exe> CCDEP= CXXDEP= ASDEP= HOSTCCDEP= -j8 ffprobe.exe\n'
        'Dependency-file generation is disabled for the clean native Windows build.\n\n' + version, encoding='utf-8')
    return {'version': '9.0.2', 'origin': ARTIFACTS['ffmpeg-source']['url'],
            'executable': 'ffprobe/ffprobe.exe', 'sha256': sha256(destination / 'ffprobe.exe'),
            'version_output': version, 'corresponding_source': 'vendor-sources/ffprobe'}


def prepare_mediainfo():
    archive = download('mediainfo')
    destination = TOOLS / 'mediainfo'
    unzip(archive, destination)
    version = run([str(destination / 'MediaInfo.exe'), '--Version'])
    if '26.10' not in version:
        raise RuntimeError('MediaInfo version did not match the pinned archive.')
    return {**ARTIFACTS['mediainfo'], 'executable': 'mediainfo/MediaInfo.exe', 'version_output': version,
            'executable_sha256': sha256(destination / 'MediaInfo.exe')}


def prepare_mkv():
    archive = download('mkvpropedit')
    destination = TOOLS / 'mkvpropedit'
    unzip(archive, destination, prefix='mkvtoolnix/', select=lambda name:
          name in {'mkvtoolnix/mkvpropedit.exe', 'mkvtoolnix/mkvextract.exe'}
          or name.startswith(('mkvtoolnix/doc/', 'mkvtoolnix/locale/')) or name.lower().endswith('.dll'))
    version = run([str(destination / 'mkvpropedit.exe'), '--version'])
    extract_version = run([str(destination / 'mkvextract.exe'), '--version'])
    if 'v103.0' not in version or 'v103.0' not in extract_version:
        raise RuntimeError('MKVToolNix version did not match the pinned archive.')
    retain(download('mkv-source'), 'mkvtoolnix')
    return {**ARTIFACTS['mkvpropedit'], 'executable': 'mkvpropedit/mkvpropedit.exe',
            'mkvextract': 'mkvpropedit/mkvextract.exe', 'version_output': version,
            'executable_sha256': sha256(destination / 'mkvpropedit.exe'),
            'mkvextract_sha256': sha256(destination / 'mkvextract.exe'),
            'source_provenance': 'Official application source plus pinned dependency sources; see docs/native-tool-sources.md for verified versions and recipe-commit inference.'}


def prepare_mkv_sources():
    inventory = json.loads((ROOT / 'packaging/native-sources.json').read_text(encoding='utf-8'))
    record = inventory['mkvtoolnix']
    retain(download('mkv-source'), 'mkvtoolnix')
    for artifact in record['artifacts']:
        retain(download_artifact(artifact), 'mkvtoolnix')
    shutil.copy2(ROOT / 'packaging/native-sources.json', SOURCES / 'mkvtoolnix/SOURCE-ARTIFACTS.json')
    shutil.copy2(ROOT / 'docs/native-tool-sources.md', SOURCES / 'mkvtoolnix/PROVENANCE.md')
    # Keep the complete Qt licence texts accessible without unpacking its source.
    qt = next(artifact for artifact in record['artifacts'] if artifact['name'] == 'qtbase')
    licence_directory = TOOLS / 'mkvpropedit' / 'doc' / 'licenses' / 'Qt'
    with tarfile.open(CACHE / qt['url'].rsplit('/', 1)[-1]) as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if member.isfile() and 'LICENSES' in parts:
                relative = Path(*parts[parts.index('LICENSES') + 1:])
                destination = licence_directory / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open('wb') as output:
                    shutil.copyfileobj(source, output)
    return {'artifacts': len(record['artifacts']) + 1,
            'estimated_bytes': record['estimated_total_bytes'] + 11217068,
            'recipe_commit': record['recipe_commit'], 'provenance_note': record['recipe_commit_basis']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', choices=['all', 'mediainfo', 'mkvpropedit', 'ffprobe', 'mkv-sources'], default='all')
    parser.add_argument('--shell', help='Git for Windows usr/bin/sh.exe used to build FFprobe')
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('Prepare the Windows media tools on Windows.')
    TOOLS.mkdir(parents=True, exist_ok=True)
    path = TOOLS / 'tools-manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'architecture': 'Windows x64', 'tools': {}}
    for name, prepare in [('mediainfo', prepare_mediainfo), ('mkvpropedit', prepare_mkv),
                          ('ffprobe', lambda: prepare_ffprobe(args.shell)), ('mkv-sources', prepare_mkv_sources)]:
        if args.only in {'all', name}:
            result = prepare()
            manifest = json.loads(path.read_text(encoding='utf-8')) if path.exists() else manifest
            manifest['tools'][name] = result
            path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
            print('Prepared ' + name, flush=True)


if __name__ == '__main__':
    main()
