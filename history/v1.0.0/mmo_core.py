"""Movie matching, reviewed file plans and journalled, conflict-safe operations.

The rename workflow only changes directory entries: it never copies or hashes
movie content. Copy and move deliberately use full BLAKE3 verification.
"""
from __future__ import annotations

import ctypes
import difflib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from support import Cancelled, check_cancel, atomic_json, state_root, redact_secrets

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.m4v', '.mov', '.avi', '.wmv', '.mpg', '.mpeg',
                    '.ts', '.m2ts', '.webm', '.flv', '.vob', '.ogm', '.mxf', '.gxf',
                    '.lxf', '.rm', '.rmvb', '.asf', '.divx'}
SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.smi', '.sami', '.sup'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
SKIP_DIRS = {'extras', 'extra', 'trailers', 'trailer', 'samples', 'sample', 'featurettes',
             'behind the scenes', 'deleted scenes', '@eadir', '$recycle.bin', 'system volume information'}
GENERIC_STEMS = {'movie', 'video', 'film', 'main', 'feature', 'title', '00000', '00001'}
_QUALITY = re.compile(r'(?i)(?:^|[ ._\[(-])(?:\d{3,4}[pi]|[248]k|bluray|blu-ray|brrip|bdrip|'
                      r'webrip|web[- .]?dl|web|hdtv|dvdrip|remux|x26[45]|h[ .]?26[45]|'
                      r'hevc|avc|av1|aac|ac3|dts|truehd|hdr10\+?|hdr|dv|dovi|yify|yts|rarbg|'
                      r'proper|repack|extended|unrated|workprint)(?=$|[ ._\])0-9-])')
_YEAR = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')
_SAMPLE = re.compile(r'(?i)(?:^|[ ._-])(?:sample|trailer|featurette|behindthescenes)(?:$|[ ._-])')
_PART = re.compile(r'(?i)(?:^|[ ._-])((?:cd|disc|disk|dvd|pt|part)[ ._-]*([1-8]))\s*$')


class MovieError(Exception):
    pass


@dataclass
class MovieGuess:
    title: str
    year: int | None = None
    part: str = ''


@dataclass
class MovieMatch:
    title: str
    year: int | None
    tmdb_id: int | None = None
    overview: str = ''
    score: float = 0.0
    original_title: str = ''
    genres: list[str] = field(default_factory=list)


@dataclass
class FileChange:
    src: Path
    dst: Path
    kind: str = 'video'
    snapshot: dict = field(default_factory=dict)
    suffix: str = ''


@dataclass
class MovieGroup:
    source: Path
    title: str
    year: int | None
    destination: Path
    files: list[FileChange] = field(default_factory=list)
    approved: bool = True
    resolved: bool = True
    status: str = 'Ready'
    candidates: list[MovieMatch] = field(default_factory=list)
    tmdb_id: int | None = None
    overview: str = ''
    warnings: list[str] = field(default_factory=list)
    mode: str = 'rename'
    folder_source: Path | None = None
    folder_destination: Path | None = None
    source_root: Path | None = None
    output_root: Path | None = None
    folder_snapshot: dict = field(default_factory=dict)
    genres: list[str] = field(default_factory=list)

    @property
    def video_paths(self):
        return [item.src for item in self.files if item.kind == 'video']


@dataclass
class RunResult:
    manifest: Path
    completed: int = 0
    failures: int = 0
    cancelled: bool = False
    paths: list[Path] = field(default_factory=list)


def _normalise(value):
    value = unicodedata.normalize('NFKD', value)
    value = ''.join(char for char in value if not unicodedata.combining(char))
    return re.sub(r'[^\w]+', ' ', value.casefold()).strip()


def safe_filename(value, limit=170):
    value = unicodedata.normalize('NFC', str(value))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    while len(value.encode('utf-16-le')) // 2 > limit:
        value = value[:-1]
    value = value.rstrip(' .')
    if not value:
        raise ValueError('Enter a movie title containing usable filename characters.')
    if re.fullmatch(r'(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])', value):
        value = '_' + value
    return value


def movie_stem(title, year):
    if not title or not str(title).strip():
        raise ValueError('A movie title is required.')
    if isinstance(year, bool) or not str(year).isdigit() or not 1888 <= int(year) <= 2099:
        raise ValueError('Enter a four-digit release year between 1888 and 2099.')
    return f'{safe_filename(title)} ({int(year)})'


def _parse_text(text):
    text = re.sub(r'\{(?:tmdb|imdb)-[^}]+\}', '', text, flags=re.I).strip()
    quality_marker = _QUALITY.search(text)
    year_text = text[:quality_marker.start()] if quality_marker else text
    years = [match for match in _YEAR.finditer(year_text) if match.start() > 0 and text[:match.start()].strip(' ._([{-')]
    year_match = years[-1] if years else None
    year = int(year_match.group(1)) if year_match else None
    title_part = text[:year_match.start()].rstrip(' ._([{ -') if year_match else text
    tail = text[year_match.end():].strip(' ._()[]-') if year_match else ''
    part = ''
    # A sequel's "Part 2" before the release year remains part of its title.
    match = _PART.search(tail)
    if match:
        part = 'pt' + match.group(2)
    quality = _QUALITY.search(title_part)
    if quality and quality.start() > 0:
        title_part = title_part[:quality.start()]
    if not year_match:
        quality = _QUALITY.search(title_part)
        if quality:
            trailing = title_part[quality.end():]
            split = _PART.search(trailing)
            if split:
                part = 'pt' + split.group(2)
            title_part = title_part[:quality.start()]
    title_part = re.sub(r'^[\[(][^\])]+[\])][ ._-]*', '', title_part)
    title_part = re.sub(r'[._]+', ' ', title_part)
    title_part = re.sub(r'\s+', ' ', title_part).strip(' []()-_')
    return MovieGuess(title_part, year, part)


def parse_movie(path):
    path = Path(path)
    guess = _parse_text(path.stem)
    parent = _parse_text(path.parent.name)
    generic = _normalise(guess.title) in GENERIC_STEMS or guess.title.isdigit() and len(guess.title) != 4
    if parent.title and parent.year is not None and (guess.year is None or generic):
        guess = MovieGuess(parent.title, parent.year, guess.part)
    return guess


def _is_link(path):
    try:
        attributes = path.lstat().st_file_attributes if hasattr(path.lstat(), 'st_file_attributes') else 0
        return path.is_symlink() or bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))
    except OSError:
        return True


def _under(path, root):
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
        return True
    except ValueError:
        return False


def discover_movies(root, recursive=True, exclude=None):
    root = Path(os.path.abspath(Path(root).expanduser()))
    if not root.exists() or _is_link(root):
        raise ValueError('Choose an existing folder or movie file without a symbolic link.')
    excluded = Path(os.path.abspath(Path(exclude).expanduser())) if exclude else None
    if root.is_file():
        return [root] if root.suffix.lower() in VIDEO_EXTENSIONS and not _SAMPLE.search(root.stem) else []
    found = []
    for directory, names, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        names[:] = sorted(name for name in names if not name.startswith('.') and name.casefold() not in SKIP_DIRS
                           and not _is_link(parent / name)
                           and not (excluded and _under(parent / name, excluded))) if recursive else []
        if excluded and _under(parent, excluded):
            continue
        for name in sorted(files):
            path = parent / name
            if path.suffix.lower() in VIDEO_EXTENSIONS and not name.startswith('.') and not _SAMPLE.search(path.stem) and not _is_link(path):
                found.append(path)
    return sorted(found, key=lambda path: str(path).casefold())


class TMDBClient:
    """Movie-only TMDB client; ambiguous matches always require human selection."""
    def __init__(self, key, language='en', cancel_event=None):
        self.key = key.strip()
        if not self.key:
            raise MovieError('Enter a TMDB API key or use filename-only matching.')
        self.language = language or 'en'
        self.cancel_event = cancel_event
        self.session = requests.Session()
        if self.key.startswith('eyJ'):
            self.session.headers['Authorization'] = 'Bearer ' + self.key
        else:
            self.session.params['api_key'] = self.key

    def _get(self, endpoint, **params):
        for attempt in range(5):
            check_cancel(self.cancel_event)
            try:
                response = self.session.get('https://api.themoviedb.org/3' + endpoint,
                                            params=params, timeout=(7, 25))
                if response.status_code in (401, 403):
                    raise MovieError('TMDB rejected this key or account access.')
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 4:
                        raise MovieError('TMDB is unavailable or rate-limited. Try again later.')
                    try:
                        delay = min(30.0, max(0.2, float(response.headers.get('Retry-After', 0)) or 0.6 * 2 ** attempt))
                    except ValueError:
                        delay = 0.6 * 2 ** attempt
                    if self.cancel_event:
                        self.cancel_event.wait(delay)
                    else:
                        time.sleep(delay)
                    continue
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 4:
                    raise MovieError('Could not read TMDB movie data: ' + redact_secrets(type(exc).__name__)) from exc
                if self.cancel_event:
                    self.cancel_event.wait(0.5 * 2 ** attempt)
                else:
                    time.sleep(0.5 * 2 ** attempt)
        raise MovieError('TMDB request failed.')

    def test(self):
        return self._get('/authentication').get('success', False)

    def search(self, title, year=None):
        params = {'query': title, 'language': self.language, 'include_adult': 'false'}
        if year:
            params['year'] = year
        raw = self._get('/search/movie', **params).get('results', [])
        if not raw and year:
            params.pop('year')
            raw = self._get('/search/movie', **params).get('results', [])
        results = []
        for item in raw:
            date = str(item.get('release_date', ''))[:4]
            result_year = int(date) if date.isdigit() else None
            names = [item.get('title', ''), item.get('original_title', '')]
            score = max(difflib.SequenceMatcher(None, _normalise(title), _normalise(name)).ratio() for name in names)
            if year and result_year:
                score = max(0.0, score - min(abs(year - result_year), 10) * 0.04)
            results.append(MovieMatch(item.get('title') or item.get('original_title') or title, result_year,
                                      item.get('id'), item.get('overview') or '', score,
                                      item.get('original_title') or ''))
        return sorted(results, key=lambda match: match.score, reverse=True)

    def details(self, tmdb_id):
        item = self._get(f'/movie/{int(tmdb_id)}', language=self.language)
        year = str(item.get('release_date', ''))[:4]
        return MovieMatch(item.get('title') or item.get('original_title') or '',
                          int(year) if year.isdigit() else None, int(tmdb_id), item.get('overview') or '',
                          1.0, item.get('original_title') or '', [genre['name'] for genre in item.get('genres', []) if genre.get('name')])

    def match(self, guess):
        candidates = self.search(guess.title, guess.year)
        exact = [item for item in candidates if guess.year is not None and item.year == guess.year
                 and _normalise(guess.title) in {_normalise(item.title), _normalise(item.original_title)}]
        return (exact[0] if len(exact) == 1 else None), candidates


def _snapshot(path):
    data = path.lstat()
    return {'dev': data.st_dev, 'ino': data.st_ino, 'size': data.st_size,
            'mtime_ns': data.st_mtime_ns, 'type': stat.S_IFMT(data.st_mode)}


def _matches_snapshot(path, expected):
    try:
        current = _snapshot(path)
        if current['type'] == stat.S_IFDIR:
            return all(current[key] == expected.get(key) for key in ('dev', 'ino', 'type'))
        return current == expected
    except OSError:
        return False


def _tree_snapshot(folder):
    result = {}
    for directory, names, files in os.walk(folder, followlinks=False):
        parent = Path(directory)
        for name in names + files:
            path = parent / name
            value = _snapshot(path)
            if value['type'] == stat.S_IFDIR:
                value = {key: value[key] for key in ('dev', 'ino', 'type')}
            result[path.relative_to(folder).as_posix()] = value
        names[:] = [name for name in names if not _is_link(parent / name)]
    return result


def _same_entry(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _path_key(path):
    return os.path.abspath(path).casefold()


def _occupied(path, own=None):
    if not path.parent.exists():
        return False
    for sibling in path.parent.iterdir():
        if sibling.name.casefold() == path.name.casefold() and not (own is not None and _same_entry(sibling, own)):
            return True
    return False


def _subtitle_suffix(path, videos, sole_movie):
    for video in sorted(videos, key=lambda value: len(value.stem), reverse=True):
        if path.stem.casefold() == video.stem.casefold():
            return video, path.suffix.lower()
        if path.stem.casefold().startswith(video.stem.casefold() + '.'):
            remainder = path.stem[len(video.stem):]
            if re.fullmatch(r'(?i)(?:\.[a-z0-9_-]{1,20})+', remainder):
                return video, remainder + path.suffix.lower()
    if sole_movie and len(videos) == 1 and re.fullmatch(r'(?i)(?:[a-z]{2,3}|english|french|german|spanish)(?:[ ._-](?:forced|sdh|cc))?', path.stem):
        return videos[0], '.' + path.stem.replace(' ', '.').replace('_', '.') + path.suffix.lower()
    return None


def _refresh_destinations(group):
    if group.year is None or not group.title.strip():
        group.resolved = False
        group.approved = False
        group.status = 'Needs title/year'
        return group
    stem = movie_stem(group.title, group.year)
    if group.mode == 'rename':
        destination = group.folder_source.parent / stem if group.folder_source else group.source.parent / stem
    else:
        if group.output_root is None:
            raise ValueError('Choose a destination for Copy or Move.')
        destination = group.output_root / stem
    group.folder_destination = destination if group.folder_source and group.mode == 'rename' else None
    group.destination = destination
    for item in group.files:
        if item.kind == 'poster':
            item.dst = destination / (stem + item.suffix if item.suffix else item.src.name)
        elif item.kind == 'subtitle':
            item.dst = destination / (stem + item.suffix)
        elif item.kind == 'video':
            item.dst = destination / (stem + item.suffix + item.src.suffix.lower())
    return group


def revise_group(group, title, year, tmdb_id=None):
    movie_stem(title, year)
    group.title = title.strip()
    group.year = int(year)
    group.tmdb_id = tmdb_id
    group.resolved = True
    group.approved = True
    group.status = 'Ready'
    return _refresh_destinations(group)


def validate_plan(plan):
    folders = {}
    files = {}
    for group in plan:
        if not group.resolved:
            group.approved = False
            continue
        _refresh_destinations(group)
        group.status = 'Ready'
        own_folder = group.folder_source if group.mode == 'rename' else None
        if _occupied(group.destination, own_folder):
            group.status = 'Conflict'
        key = _path_key(group.destination)
        if key in folders and folders[key] is not group:
            group.status = folders[key].status = 'Conflict'
        folders[key] = group
        for change in group.files:
            source_at_destination = change.src
            if group.folder_source and group.folder_destination and _under(change.src, group.folder_source):
                source_at_destination = group.folder_destination / change.src.relative_to(group.folder_source)
            if _occupied(change.dst, change.src if change.src.parent == change.dst.parent else source_at_destination):
                group.status = 'Conflict'
            key = _path_key(change.dst)
            if key in files:
                other, other_change = files[key]
                if other_change is not change:
                    group.status = other.status = 'Conflict'
            files[key] = group, change
        if group.status == 'Ready' and group.mode == 'rename':
            folder_same = not group.folder_source or str(group.folder_source) == str(group.folder_destination)
            if folder_same and all(str(item.src) == str(item.dst) for item in group.files):
                group.status = 'Already correct'
    return plan


def build_plan(root, destination=None, mode='rename', recursive=True, matcher=None,
               cancel_event=None, log=None, progress=None):
    if mode not in {'rename', 'copy', 'move'}:
        raise ValueError('Choose Rename in place, Copy or Move.')
    root = Path(os.path.abspath(Path(root).expanduser()))
    source_root = root if root.is_dir() else root.parent
    output = Path(os.path.abspath(Path(destination).expanduser())) if destination else None
    if mode != 'rename' and output is None:
        raise ValueError('Choose a destination folder for Copy or Move.')
    if output and _is_link(output) and output.exists():
        raise ValueError('The destination cannot be a symbolic link or junction.')
    exclude = output if mode != 'rename' and output and output != source_root else None
    if progress:
        progress(0, 1, 'Finding movie files')
    videos = discover_movies(root, recursive, exclude)
    guesses = {path: parse_movie(path) for path in videos}
    buckets = []
    used = set()
    for path in videos:
        if path in used:
            continue
        guess = guesses[path]
        parts = [other for other in videos if other.parent == path.parent and guesses[other].part
                 and _normalise(guesses[other].title) == _normalise(guess.title) and guesses[other].year == guess.year] if guess.part else [path]
        used.update(parts)
        buckets.append(parts)
    groups = []
    parent_videos = {}
    for path in videos:
        parent_videos.setdefault(path.parent, []).append(path)
    for number, group_videos in enumerate(buckets, 1):
        check_cancel(cancel_event)
        path = group_videos[0]
        guess = guesses[path]
        group = MovieGroup(path, guess.title, guess.year, path.parent, mode=mode,
                           source_root=source_root, output_root=output)
        sole = set(parent_videos[path.parent]) == set(group_videos)
        if mode == 'rename' and path.parent != source_root and sole:
            group.folder_source = path.parent
            group.folder_snapshot = _tree_snapshot(path.parent)
        for video in group_videos:
            suffix = ' - ' + guesses[video].part if guesses[video].part else ''
            group.files.append(FileChange(video, video, 'video', _snapshot(video), suffix))
        companions = list(path.parent.iterdir())
        if sole:
            for name in ('Subs', 'Subtitles'):
                subtitle_directory = path.parent / name
                if subtitle_directory.is_dir() and not _is_link(subtitle_directory):
                    companions.extend(subtitle_directory.iterdir())
        for companion in sorted(companions, key=lambda value: str(value).casefold()):
            if not companion.is_file() or _is_link(companion):
                continue
            if companion.suffix.lower() in SUBTITLE_EXTENSIONS:
                association = _subtitle_suffix(companion, group_videos, sole)
                if association:
                    video, suffix = association
                    split = ' - ' + guesses[video].part if guesses[video].part else ''
                    group.files.append(FileChange(companion, companion, 'subtitle', _snapshot(companion), split + suffix))
                elif sole:
                    group.warnings.append(f'Unassigned subtitle left unchanged: {companion.name}')
            elif companion.suffix.lower() in IMAGE_EXTENSIONS:
                if sole and companion.stem.casefold() in {'poster', 'folder', 'cover', 'fanart', 'background', 'banner'}:
                    group.files.append(FileChange(companion, companion, 'poster', _snapshot(companion)))
                else:
                    for video in group_videos:
                        if companion.stem.casefold().startswith(video.stem.casefold() + '-'):
                            label = companion.stem[len(video.stem):]
                            if label.casefold() in {'-poster', '-fanart', '-background', '-banner'}:
                                group.files.append(FileChange(companion, companion, 'poster', _snapshot(companion), label + companion.suffix.lower()))
                                break
        if matcher:
            try:
                result = matcher.match(guess) if hasattr(matcher, 'match') else matcher(guess)
                if isinstance(result, tuple):
                    chosen, group.candidates = result
                elif isinstance(result, MovieMatch):
                    chosen = result
                    group.candidates = [result]
                else:
                    chosen = None
                    group.candidates = list(result or [])
                if chosen and chosen.year:
                    group.title, group.year, group.tmdb_id = chosen.title, chosen.year, chosen.tmdb_id
                    group.overview, group.genres = chosen.overview, list(chosen.genres)
                else:
                    group.resolved = group.approved = False
                    group.status = 'Choose movie'
                    group.warnings.append('Choose a TMDB match or enter the title and release year manually.')
            except Cancelled:
                raise
            except Exception as exc:
                group.resolved = group.approved = False
                group.status = 'Matching failed'
                group.warnings.append(redact_secrets(exc))
        if not group.title or group.year is None:
            group.resolved = group.approved = False
            group.status = 'Needs title/year'
        if re.search(r'(?i)\{edition-', path.stem + path.parent.name):
            group.resolved = group.approved = False
            group.status = 'Review edition'
            group.warnings.append('An edition label was found. Review the title before choosing its destination.')
        if group.resolved:
            _refresh_destinations(group)
        groups.append(group)
        if log:
            log(f'{path.name}: {group.title} ({group.year or "year needed"}) · {group.status}')
        if progress:
            progress(number, len(buckets), 'Matching movies' if matcher else 'Planning movie names')
    validate_plan(groups)
    return groups


def _check_parent_links(path):
    for parent in [path.parent, *path.parent.parents]:
        if parent.exists() and _is_link(parent):
            raise OSError('A destination parent is a link or junction; operation stopped.')


def _rename_noreplace(src, dst):
    """Exclusive same-filesystem rename; never falls back to copying bytes."""
    if _occupied(dst, src):
        raise FileExistsError(f'Destination is occupied: {dst}')
    _check_parent_links(dst)
    if os.name == 'nt':
        os.rename(src, dst)
        return
    if sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, 'renameat2', None)
        if function:
            result = function(-100, os.fsencode(src), -100, os.fsencode(dst), 1)
            if result:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(dst))
            return
    if src.is_file():
        os.link(src, dst)
        src.unlink()
        return
    raise OSError('This platform does not provide an exclusive directory rename. Use Windows or a supported Linux filesystem.')


def _record(journal, path, record):
    journal['records'].append(record)
    atomic_json(path, journal)
    return record


def _mkdir(path, journal, journal_path):
    if path.exists():
        if not path.is_dir() or _is_link(path):
            raise FileExistsError(f'Destination is not a regular folder: {path}')
        return
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    _check_parent_links(path)
    for directory in reversed(missing):
        record = _record(journal, journal_path, {'action': 'mkdir', 'dst': str(directory), 'status': 'planned'})
        directory.mkdir()
        record.update(status='done', destination_snapshot=_snapshot(directory))
        atomic_json(journal_path, journal)


def _journal_rename(src, dst, journal, journal_path, expected=None, tree=None):
    if str(src) == str(dst):
        return
    if expected and not _matches_snapshot(src, expected):
        raise OSError(f'Source changed after preview: {src.name}')
    if tree is not None and _tree_snapshot(src) != tree:
        raise OSError(f'Folder contents changed after preview: {src.name}')
    if _path_key(src) == _path_key(dst):
        temporary = src.parent / ('.mmo-rename-' + uuid.uuid4().hex)
        _journal_rename(src, temporary, journal, journal_path, expected, tree)
        _journal_rename(temporary, dst, journal, journal_path, _snapshot(temporary), tree)
        return
    record = _record(journal, journal_path, {'action': 'rename', 'src': str(src), 'dst': str(dst),
                                             'source_snapshot': _snapshot(src), 'status': 'planned'})
    if src.is_dir():
        record['tree'] = _tree_snapshot(src)
        atomic_json(journal_path, journal)
    _rename_noreplace(src, dst)
    record.update(status='done', destination_snapshot=_snapshot(dst))
    atomic_json(journal_path, journal)


def _hash_file(path, cancel_event=None):
    from blake3 import blake3
    digest = blake3()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            check_cancel(cancel_event)
            digest.update(block)
    return digest.hexdigest()


def _copy_verified(src, dst, record, journal, journal_path, cancel_event=None, progress=None):
    from blake3 import blake3
    if not _matches_snapshot(src, record['source_snapshot']):
        raise OSError('Source changed after preview; source retained.')
    if _occupied(dst):
        raise FileExistsError('Approved destination is now occupied; review again.')
    _check_parent_links(dst)
    digest = blake3()
    last_progress = 0.0
    try:
        with dst.open('xb') as target:
            record.update(status='copying', destination_snapshot=_snapshot(dst))
            atomic_json(journal_path, journal)
            with src.open('rb') as source:
                count = 0
                for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
                    check_cancel(cancel_event)
                    digest.update(block)
                    target.write(block)
                    count += len(block)
                    if progress and time.monotonic() - last_progress >= 0.1:
                        progress(count, max(record['source_snapshot']['size'], 1), f'Copying {src.name}')
                        last_progress = time.monotonic()
            target.flush()
            os.fsync(target.fileno())
        shutil.copystat(src, dst)
        record.update(digest=digest.hexdigest(), destination_snapshot=_snapshot(dst))
        atomic_json(journal_path, journal)
        if _hash_file(dst, cancel_event) != record['digest']:
            raise OSError('Destination checksum mismatch; source retained.')
        if not _matches_snapshot(src, record['source_snapshot']):
            raise OSError('Source changed during transfer; source retained.')
        record['status'] = 'verified'
        atomic_json(journal_path, journal)
        check_cancel(cancel_event)
        if record['action'] == 'move':
            if _hash_file(src, cancel_event) != record['digest'] or not _matches_snapshot(src, record['source_snapshot']):
                raise OSError('Source changed during transfer; source retained.')
            record['source_removal_pending'] = True
            atomic_json(journal_path, journal)
            src.unlink()
            record['source_removed'] = True
        record.update(status='done', destination_snapshot=_snapshot(dst))
        atomic_json(journal_path, journal)
    except BaseException:
        if record.get('destination_snapshot') and dst.exists():
            record['destination_snapshot'] = _snapshot(dst)
            if record['status'] == 'copying':
                record['status'] = 'partial'
            atomic_json(journal_path, journal)
        raise


def execute_plan(plan, mode='rename', journal_dir=None, cancel_event=None, log=None, progress=None):
    if mode not in {'rename', 'copy', 'move'}:
        raise ValueError('Unknown file operation.')
    validate_plan(plan)
    directory = Path(journal_dir) if journal_dir else state_root() / 'manifests'
    directory.mkdir(parents=True, exist_ok=True)
    journal_path = directory / f'movie-{time.time_ns()}-{uuid.uuid4().hex[:8]}.json'
    journal = {'version': 1, 'mode': mode, 'started_at': datetime.now(timezone.utc).isoformat(),
               'records': [], 'undone': False, 'failure_count': 0, 'cancelled': False}
    atomic_json(journal_path, journal)
    result = RunResult(journal_path)
    eligible = [group for group in plan if group.approved and group.resolved and group.status not in {'Conflict', 'Already correct'}]
    for number, group in enumerate(eligible, 1):
        try:
            check_cancel(cancel_event)
            if group.mode != mode:
                raise ValueError('The selected operation changed after preview. Preview again.')
            if _occupied(group.destination, group.folder_source if mode == 'rename' else None):
                raise FileExistsError(f'Movie destination is now occupied: {group.destination}')
            if progress:
                progress(number - 1, len(eligible), f'{"Renaming" if mode == "rename" else mode.title()} · {group.title}')
            if mode == 'rename' and group.folder_source:
                if group.folder_snapshot and _tree_snapshot(group.folder_source) != group.folder_snapshot:
                    raise OSError('Folder contents changed after preview; preview again.')
                _journal_rename(group.folder_source, group.destination, journal, journal_path,
                                _snapshot(group.folder_source), group.folder_snapshot)
            else:
                _mkdir(group.destination, journal, journal_path)
            for change in group.files:
                check_cancel(cancel_event)
                actual_source = change.src
                if mode == 'rename' and group.folder_source:
                    actual_source = group.destination / change.src.relative_to(group.folder_source)
                if str(actual_source) == str(change.dst):
                    if change.kind == 'video':
                        result.paths.append(change.dst)
                    continue
                if mode == 'rename':
                    _journal_rename(actual_source, change.dst, journal, journal_path, change.snapshot)
                else:
                    record = _record(journal, journal_path, {'action': mode, 'src': str(actual_source),
                                                               'dst': str(change.dst), 'status': 'planned',
                                                               'source_snapshot': change.snapshot,
                                                               'source_removed': False})
                    def bytes_progress(done, total, phase):
                        if progress:
                            progress(number - 1 + done / max(total, 1), len(eligible), phase)
                    _copy_verified(actual_source, change.dst, record, journal, journal_path, cancel_event, bytes_progress)
                if change.kind == 'video':
                    result.paths.append(change.dst)
                if log:
                    log(f'{"RENAMED" if mode == "rename" else mode.upper()}: {change.src.name} → {change.dst}')
            result.completed += 1
        except Cancelled:
            result.cancelled = journal['cancelled'] = True
            if log:
                log('Cancelled safely. Recorded operations remain available through Undo.')
            break
        except Exception as exc:
            result.failures += 1
            journal.setdefault('errors', []).append({'movie': str(group.source), 'error': redact_secrets(exc)})
            if log:
                log(f'FAILED: {group.title}: {redact_secrets(exc)}')
        finally:
            journal['failure_count'] = result.failures
            atomic_json(journal_path, journal)
        if progress:
            progress(number, len(eligible), 'Finishing movie operations')
    journal['finished_at'] = datetime.now(timezone.utc).isoformat()
    journal['completed'] = result.completed
    atomic_json(journal_path, journal)
    return result


def latest_manifest(journal_dir=None):
    directory = Path(journal_dir) if journal_dir else state_root() / 'manifests'
    for path in sorted(directory.glob('movie-*.json'), key=lambda value: value.stat().st_mtime_ns, reverse=True):
        try:
            journal = json.loads(path.read_text(encoding='utf-8'))
            if not journal.get('undone') and any(record.get('status') in {'done', 'partial', 'verified', 'copying', 'unresolved'} or record.get('status') == 'planned' and os.path.lexists(record.get('dst', '')) for record in journal.get('records', [])):
                return path
        except (OSError, ValueError):
            continue
    return None


def undo_manifest(path, cancel_event=None, log=None, progress=None):
    path = Path(path)
    journal = json.loads(path.read_text(encoding='utf-8'))
    if journal.get('version') != 1 or not isinstance(journal.get('records'), list):
        raise ValueError('This is not an MMO recovery record.')
    result = RunResult(path)
    if journal.get('undone'):
        return result
    records = list(reversed(journal['records']))
    for number, record in enumerate(records, 1):
        if record.get('status') == 'planned':
            # Recover the small crash window between an exclusive rename and
            # publishing its completion checkpoint. Ownership is already in
            # the pre-mutation journal, unlike a newly created copy or folder.
            pending_src = Path(record['src']) if record.get('src') else None
            pending_dst = Path(record['dst'])
            if record['action'] == 'rename' and pending_src is not None and not os.path.lexists(pending_src) and _matches_snapshot(pending_dst, record.get('source_snapshot', {})):
                record.update(status='done', destination_snapshot=_snapshot(pending_dst))
                atomic_json(path, journal)
            elif os.path.lexists(pending_dst):
                record['status'] = 'unresolved'
                record['undo_error'] = 'No ownership checkpoint exists for this destination; inspect it manually.'
                atomic_json(path, journal)
            else:
                record['status'] = 'undone'
                atomic_json(path, journal)
                continue
        if record.get('status') == 'unresolved':
            if not os.path.lexists(record['dst']):
                record['status'] = 'undone'
                atomic_json(path, journal)
                continue
            result.failures += 1
            if log:
                log(f'UNDO SKIPPED: {record["dst"]}: {record.get("undo_error", "Inspect this path manually.")}')
            continue
        if record.get('status') not in {'done', 'partial', 'verified', 'copying'}:
            continue
        try:
            check_cancel(cancel_event)
            dst = Path(record['dst'])
            src = Path(record['src']) if record.get('src') else None
            if (record['action'] == 'move' and record.get('undo_copy') and src is not None
                    and not os.path.lexists(dst) and _matches_snapshot(src, record['undo_copy'].get('snapshot', {}))
                    and record.get('digest') and _hash_file(src, cancel_event) == record['digest']):
                record['status'] = 'undone'
                record.pop('undo_copy', None)
                result.completed += 1
                atomic_json(path, journal)
                continue
            if not _matches_snapshot(dst, record.get('destination_snapshot', {})):
                raise OSError('Recorded destination changed or is missing; left unchanged.')
            action = record['action']
            if action == 'mkdir':
                dst.rmdir()  # Only an unchanged, empty, application-owned folder.
            elif action == 'rename':
                if 'tree' in record and _tree_snapshot(dst) != record['tree']:
                    raise OSError('Folder contents changed after the run; left in place.')
                if src is None or not src.parent.is_dir() or _occupied(src, dst):
                    raise FileExistsError('The original location is unavailable or occupied.')
                _rename_noreplace(dst, src)
                result.paths.append(src)
            elif action in {'copy', 'move'}:
                if record.get('digest') and _hash_file(dst, cancel_event) != record['digest']:
                    raise OSError('Destination content changed; left unchanged.')
                source_missing = src is not None and not os.path.lexists(src)
                if action == 'move' and source_missing and (not record.get('digest') or record['status'] == 'partial'):
                    raise OSError('The original is missing and the transfer is incomplete; partial data retained for inspection.')
                if action == 'move' and (record.get('source_removed') or source_missing):
                    if src is None or not src.parent.is_dir():
                        raise FileExistsError('The original location is unavailable or occupied.')
                    _check_parent_links(src)
                    # An interrupted undo copy has its own ownership record.
                    if record.get('undo_copy'):
                        partial = record['undo_copy']
                        if not _matches_snapshot(src, partial.get('snapshot', {})):
                            raise OSError('An interrupted restore changed; inspect it manually.')
                        src.unlink()
                        record.pop('undo_copy', None)
                    if _occupied(src):
                        raise FileExistsError('The original location is unavailable or occupied.')
                    from blake3 import blake3
                    digest = blake3()
                    with src.open('xb') as target:
                        record['undo_copy'] = {'snapshot': _snapshot(src)}
                        atomic_json(path, journal)
                        try:
                            with dst.open('rb') as origin:
                                for block in iter(lambda: origin.read(4 * 1024 * 1024), b''):
                                    check_cancel(cancel_event)
                                    target.write(block)
                                    digest.update(block)
                            target.flush()
                            os.fsync(target.fileno())
                        finally:
                            target.flush()
                            record['undo_copy']['snapshot'] = _snapshot(src)
                            atomic_json(path, journal)
                    shutil.copystat(dst, src)
                    record['undo_copy']['snapshot'] = _snapshot(src)
                    atomic_json(path, journal)
                    if digest.hexdigest() != record.get('digest') or _hash_file(src, cancel_event) != record.get('digest'):
                        raise OSError('Restored checksum mismatch; destination retained.')
                    if not _matches_snapshot(dst, record['destination_snapshot']):
                        raise OSError('Destination changed during restore; both files retained.')
                    dst.unlink()
                    record.pop('undo_copy', None)
                    result.paths.append(src)
                else:
                    dst.unlink()
            else:
                raise ValueError('Unknown recovery operation.')
            record['status'] = 'undone'
            result.completed += 1
            if log:
                log(f'UNDONE: {dst}')
        except Cancelled:
            result.cancelled = True
            break
        except Exception as exc:
            result.failures += 1
            record['undo_error'] = redact_secrets(exc)
            if log:
                log(f'UNDO SKIPPED: {record.get("dst")}: {redact_secrets(exc)}')
        finally:
            atomic_json(path, journal)
        if progress:
            progress(number, len(records), 'Undoing recorded changes')
    journal['undone'] = all(record.get('status') in {'undone', 'planned'} for record in journal['records'])
    if journal['undone']:
        journal['undone_at'] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, journal)
    return result
