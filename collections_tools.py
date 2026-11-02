"""Read-only TMDB film-series inventories and conservative completeness reports.

Official API references:
https://developer.themoviedb.org/reference/movie-details
https://developer.themoviedb.org/reference/collection-details
https://developer.themoviedb.org/reference/search-movie

Only titles, release years and numeric provider IDs reach TMDB. Filesystem
paths stay in the local report/cache destination chosen by the application.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from mmo_core import TMDBClient, MovieGuess, discover_movies, parse_movie, _is_link
from support import Cancelled, atomic_json, check_cancel, redact_secrets

MISSING_STATUS = 'Not found among confirmed matches'
CACHE_VERSION = 1
STABLE_TTL = 7 * 24 * 60 * 60
UPCOMING_TTL = 24 * 60 * 60
UNCERTAINTY_NOTE = ('Missing means not found among confirmed matches. Unmatched files may affect completeness; '
                    'the report does not prove that a movie is absent from your library.')


@dataclass
class CollectionReport:
    collections: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    source: str = 'TMDB movie collections'
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notes: list[str] = field(default_factory=lambda: [UNCERTAINTY_NOTE])
    as_of: str = ''

    def to_dict(self):
        return asdict(self)


def _normalise(value):
    text = unicodedata.normalize('NFKD', str(value))
    return re.sub(r'[^\w]+', ' ', ''.join(char for char in text if not unicodedata.combining(char)).casefold()).strip()


def _path_key(path):
    return os.path.normcase(os.path.abspath(path))


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit():
        return None
    identifier = int(value)
    return identifier if 0 < identifier <= 2 ** 31 - 1 else None


def _release_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _date_argument(today):
    if today is None:
        return datetime.now(timezone.utc).date()
    if isinstance(today, datetime):
        return today.astimezone(timezone.utc).date() if today.tzinfo else today.date()
    if isinstance(today, date):
        return today
    result = _release_date(today)
    if result is None:
        raise ValueError('Use a complete YYYY-MM-DD date for the collections report.')
    return result


def _identity_agrees(details, guess):
    released = _release_date(details.get('release_date'))
    names = {normal for normal in (_normalise(details.get('title', '')), _normalise(details.get('original_title', ''))) if normal}
    return bool(guess.title and guess.year is not None and released and released.year == guess.year
                and _normalise(guess.title) in names)


def _local_nfo(path):
    candidates = [path.with_suffix('.nfo')]
    generic = path.parent / 'movie.nfo'
    if generic not in candidates and generic.is_file() and len(discover_movies(path.parent, recursive=False)) == 1:
        candidates.append(generic)
    for nfo in candidates:
        if not nfo.is_file() or _is_link(nfo):
            continue
        if nfo.stat().st_size > 2 * 1024 * 1024:
            return None, None, 'Local NFO exceeds the supported 2 MiB size; it was not parsed.'
        try:
            content = nfo.read_bytes()
            text = content.decode('utf-16') if content.startswith((b'\xff\xfe', b'\xfe\xff')) else content.decode('utf-8-sig')
            if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
                return None, None, 'Local NFO contains a DTD or entity declaration; it was not parsed.'
            root = ET.fromstring(text)
            if root.tag.rsplit('}', 1)[-1].casefold() != 'movie':
                return None, None, 'Local NFO is not movie metadata; its identifiers were ignored.'
            identifiers, title, year = set(), '', None
            for element in root:
                name = element.tag.rsplit('}', 1)[-1].casefold()
                if name == 'title':
                    title = (element.text or '').strip()
                elif name == 'year' and re.fullmatch(r'\d{4}', (element.text or '').strip()):
                    year = int(element.text.strip())
                elif name == 'tmdbid' or name == 'uniqueid' and element.get('type', '').casefold() == 'tmdb':
                    identifier = _positive_id((element.text or '').strip())
                    if identifier is None:
                        return None, None, 'Local NFO contains an invalid TMDB movie ID; no automatic ID was accepted.'
                    identifiers.add(identifier)
            if len(identifiers) > 1:
                return None, None, 'Local NFO declares conflicting TMDB movie IDs; review the file.'
            if identifiers:
                return identifiers.pop(), MovieGuess(title, year), ''
        except (OSError, UnicodeError, ET.ParseError, ValueError) as exc:
            return None, None, 'Local NFO could not be parsed: ' + type(exc).__name__
    return None, None, ''


def _known_match(value):
    """Return a reviewed TMDB ID, ignoring identifiers from other providers."""
    if isinstance(value, dict):
        if str(value.get('provider', 'TMDB')).casefold() != 'tmdb':
            return None
        return _positive_id(value.get('tmdb_id') or value.get('provider_id') or value.get('id'))
    if hasattr(value, 'tmdb_id'):
        if str(getattr(value, 'provider', 'TMDB')).casefold() != 'tmdb':
            return None
        return _positive_id(value.tmdb_id or getattr(value, 'provider_id', None))
    return _positive_id(value)


class _Cache:
    def __init__(self, path, today, force_refresh=False):
        self.path = Path(path) if path else None
        self.today = today
        self.force_refresh = force_refresh
        self.data = {'version': CACHE_VERSION, 'matches': {}, 'movies': {}, 'collections': {}}
        self.dirty = False
        self.errors = []
        if self.path and self.path.is_file() and not _is_link(self.path):
            try:
                if self.path.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError('Collections cache exceeds 32 MiB')
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if loaded.get('version') == CACHE_VERSION:
                    for key in ('matches', 'movies', 'collections'):
                        if isinstance(loaded.get(key), dict):
                            self.data[key] = loaded[key]
            except (OSError, ValueError, AttributeError) as exc:
                self.errors.append({'stage': 'cache', 'error': 'Collections cache was ignored: ' + redact_secrets(exc)})

    def get(self, section, key):
        if self.force_refresh:
            return None
        entry = self.data[section].get(key)
        if not isinstance(entry, dict) or 'value' not in entry:
            return None
        try:
            age = time.time() - float(entry['fetched_at'])
            ttl = float(entry.get('ttl', STABLE_TTL))
            if not 0 <= age <= min(ttl, STABLE_TTL):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return entry['value']

    def put(self, section, key, value):
        ttl = STABLE_TTL
        items = value.get('parts', []) if isinstance(value, dict) and section == 'collections' else [value]
        if section != 'matches' and any(not _release_date(item.get('release_date')) or _release_date(item.get('release_date')) > self.today
                                        for item in items if isinstance(item, dict)):
            ttl = UPCOMING_TTL
        self.data[section][key] = {'fetched_at': time.time(), 'ttl': ttl, 'value': value}
        self.dirty = True

    def save(self):
        if self.path and self.dirty:
            if self.path.exists() and _is_link(self.path):
                raise OSError('The collections cache destination is a symbolic link or junction.')
            atomic_json(self.path, self.data)
            self.dirty = False


def _movie_entry(item, paths=None, status=''):
    released = _release_date(item.get('release_date'))
    return {'id': int(item['id']), 'title': item.get('title') or item.get('original_title') or 'Untitled movie',
            'year': released.year if released else None, 'release_date': released.isoformat() if released else '',
            'paths': list(paths or []), 'status': status}


def analyse_collections(paths, api_key, language='en', cancel_event=None, progress=None,
                        cache_path=None, known_matches=None, include_unreleased=False,
                        today=None, force_refresh=False):
    """Compare confirmed local movie IDs against TMDB's canonical collections.

    A reviewed ID may identify a badly named file. A filename or NFO match must
    instead have one exact normalised title and release year. The collection
    endpoint's future/TBA parts remain separate from released missing films.
    """
    as_of = _date_argument(today)
    check_cancel(cancel_event)
    client = TMDBClient(api_key, language, cancel_event)
    cache = _Cache(cache_path, as_of, force_refresh)
    report = CollectionReport(as_of=as_of.isoformat())
    report.errors.extend(cache.errors)
    report.notes.append('Collections follow TMDB movie membership. Custom box sets and broader cinematic universes are not inferred.')
    local_paths = {}
    for value in paths:
        path = Path(os.path.abspath(Path(value).expanduser()))
        local_paths.setdefault(_path_key(path), path)
    reviewed = {_path_key(Path(path)): value for path, value in (known_matches or {}).items()}
    inventory, collection_ids = {}, {}
    memory, failed = {'movies': {}, 'collections': {}, 'matches': {}}, {}

    def fetch(section, identifier):
        key = f'{language}:{identifier}'
        if key in memory[section]:
            return memory[section][key]
        cached = cache.get(section, key)
        if isinstance(cached, dict) and _positive_id(cached.get('id')) == identifier:
            memory[section][key] = cached
            return cached
        endpoint = '/movie/' if section == 'movies' else '/collection/'
        response = client._get(endpoint + str(identifier), language=language)
        if not isinstance(response, dict) or _positive_id(response.get('id')) != identifier:
            raise ValueError('TMDB returned an unexpected movie or collection identity.')
        if section == 'movies' and not (response.get('title') or response.get('original_title')):
            raise ValueError('TMDB did not supply usable movie details for this identity.')
        cache.put(section, key, response)
        memory[section][key] = response
        return response

    def reject(path, guess, reason):
        report.unmatched.append({'path': str(path), 'title': guess.title, 'year': guess.year, 'reason': reason})

    try:
        for index, path in enumerate(local_paths.values(), 1):
            check_cancel(cancel_event)
            guess = parse_movie(path)
            if progress:
                progress(index - 1, max(len(local_paths), 1), f'Confirming movie {index}/{len(local_paths)}')
            try:
                if not path.is_file() or _is_link(path):
                    reject(path, guess, 'The movie path is missing, unreadable or a link; it was not matched.')
                    continue
                identifier = _known_match(reviewed.get(_path_key(path)))
                confidence = 'Reviewed TMDB match' if identifier else ''
                identity_guess = guess
                if identifier is None:
                    nfo_id, nfo_guess, warning = _local_nfo(path)
                    if warning:
                        report.errors.append({'path': str(path), 'stage': 'nfo', 'error': warning})
                    if nfo_id:
                        identifier, confidence = nfo_id, 'Validated local NFO'
                        if (nfo_guess.title and nfo_guess.year and guess.title and guess.year
                                and (_normalise(nfo_guess.title) != _normalise(guess.title) or nfo_guess.year != guess.year)):
                            reject(path, guess, 'The filename and local NFO disagree on the movie title or year; review this match.')
                            continue
                        identity_guess = MovieGuess(nfo_guess.title or guess.title, nfo_guess.year or guess.year)
                    elif warning and ('invalid TMDB' in warning or 'conflicting TMDB' in warning):
                        reject(path, guess, warning)
                        continue
                if identifier is None:
                    if not guess.title or guess.year is None:
                        reject(path, guess, 'A title and release year are needed for a conservative match.')
                        continue
                    key = hashlib.sha256(json.dumps([_normalise(guess.title), guess.year, language], ensure_ascii=False).encode('utf-8')).hexdigest()
                    if key in failed:
                        raise ValueError(failed[key])
                    matched = memory['matches'].get(key) or cache.get('matches', key)
                    identifier = _positive_id(matched.get('id')) if isinstance(matched, dict) else None
                    if identifier is None:
                        try:
                            match, candidates = client.match(guess)
                        except Cancelled:
                            raise
                        except Exception as exc:
                            failed[key] = redact_secrets(exc)
                            raise
                        if match is None or _positive_id(getattr(match, 'tmdb_id', None)) is None:
                            reject(path, guess, 'No unique exact title and release year match; review this movie before trusting completeness.')
                            continue
                        identifier = int(match.tmdb_id)
                        cache.put('matches', key, {'id': identifier})
                        memory['matches'][key] = {'id': identifier}
                    confidence = 'Exact title and release year'
                details = fetch('movies', identifier)
                if confidence != 'Reviewed TMDB match' and not _identity_agrees(details, identity_guess):
                    reject(path, guess, 'The declared or searched movie identity does not agree with its title and release year.')
                    continue
                movie = inventory.setdefault(identifier, {'details': details, 'paths': [], 'confidence': confidence})
                movie['paths'].append(str(path))
                membership = details.get('belongs_to_collection')
                if membership is not None:
                    collection_id = _positive_id(membership.get('id')) if isinstance(membership, dict) else None
                    if collection_id is None:
                        report.errors.append({'path': str(path), 'stage': 'membership', 'error': 'TMDB supplied an invalid collection ID.'})
                        movie['collection_error'] = True
                    else:
                        collection_ids[collection_id] = membership.get('name') or ''
                        movie['collection_id'] = collection_id
            except Cancelled:
                raise
            except Exception as exc:
                reason = redact_secrets(exc)
                reject(path, guess, 'Movie lookup failed: ' + reason)
                report.errors.append({'path': str(path), 'stage': 'movie', 'error': reason})
            finally:
                if progress:
                    progress(index, max(len(local_paths), 1), 'Confirming local movie identities')
        for index, (identifier, hinted_name) in enumerate(sorted(collection_ids.items()), 1):
            check_cancel(cancel_event)
            if progress:
                progress(index - 1, len(collection_ids), 'Comparing TMDB film collections')
            try:
                collection = fetch('collections', identifier)
                raw_parts = collection.get('parts')
                if not isinstance(raw_parts, list) or not raw_parts:
                    raise ValueError('TMDB did not supply a usable movie list for this collection.')
                parts = {}
                member_issues = False
                for item in raw_parts:
                    if not isinstance(item, dict) or _positive_id(item.get('id')) is None:
                        member_issues = True
                        report.errors.append({'collection_id': identifier, 'stage': 'collection-part', 'error': 'An invalid collection member was skipped.'})
                        continue
                    if item.get('media_type') not in (None, 'movie') or item.get('video') is True:
                        continue
                    parts[int(item['id'])] = item
                if not parts:
                    raise ValueError('No movie members were available for this collection.')
                owned, missing, upcoming = [], [], []
                for movie_id, part in parts.items():
                    if movie_id in inventory:
                        owned.append(_movie_entry(part, inventory[movie_id]['paths'], 'Owned'))
                        continue
                    released = _release_date(part.get('release_date'))
                    production = part.get('status')
                    if released and released <= as_of and production not in {'Rumored', 'Planned', 'In Production', 'Post Production', 'Canceled'}:
                        missing.append(_movie_entry(part, status=MISSING_STATUS))
                    else:
                        upcoming.append(_movie_entry(part, status='Upcoming' if released and released > as_of else 'Release date unknown' if released is None else 'Not confirmed released'))
                for movie_id, movie in inventory.items():
                    if movie.get('collection_id') == identifier and movie_id not in parts:
                        member_issues = True
                        owned.append(_movie_entry(movie['details'], movie['paths'], 'Owned · membership differs from current parts list'))
                        report.errors.append({'collection_id': identifier, 'movie_id': movie_id, 'stage': 'membership',
                                              'error': 'A confirmed movie belongs to this collection but is absent from its current parts list.'})
                order = lambda item: (item['release_date'] or '9999-99-99', item['title'].casefold(), item['id'])
                owned.sort(key=order)
                missing.sort(key=order)
                upcoming.sort(key=order)
                report.collections.append({'id': identifier, 'name': collection.get('name') or hinted_name or f'Collection {identifier}',
                                           'owned': owned, 'missing': missing, 'upcoming': upcoming,
                                           'wanted': list(missing) + (list(upcoming) if include_unreleased else []),
                                           'complete': not missing and not member_issues,
                                           'membership_complete': not member_issues})
            except Cancelled:
                raise
            except Exception as exc:
                report.errors.append({'collection_id': identifier, 'name': hinted_name, 'stage': 'collection', 'error': redact_secrets(exc)})
            finally:
                if progress:
                    progress(index, len(collection_ids), 'Compared film collections')
        check_cancel(cancel_event)
    except Cancelled:
        try:
            cache.save()
        except OSError:
            pass
        raise
    finally:
        session = getattr(client, 'session', None)
        if session is not None and hasattr(session, 'close'):
            session.close()
    try:
        cache.save()
    except OSError as exc:
        report.errors.append({'stage': 'cache', 'error': 'Could not save collections cache: ' + redact_secrets(exc)})
    report.collections.sort(key=lambda collection: collection['name'].casefold())
    missing_ids = {movie['id'] for collection in report.collections for movie in collection['missing']}
    upcoming_ids = {movie['id'] for collection in report.collections for movie in collection['upcoming']}
    report.stats = {'library_paths': len(local_paths), 'confirmed_paths': sum(len(movie['paths']) for movie in inventory.values()),
                    'unique_movies': len(inventory), 'collections': len(report.collections), 'missing': len(missing_ids),
                    'upcoming': len(upcoming_ids), 'unmatched': len(report.unmatched), 'errors': len(report.errors),
                    'complete_collections': sum(collection['complete'] for collection in report.collections),
                    'incomplete_collections': sum(not collection['complete'] for collection in report.collections),
                    'non_collection_movies': sum(not movie.get('collection_id') and not movie.get('collection_error') for movie in inventory.values())}
    report.stats['wanted'] = len(missing_ids | upcoming_ids) if include_unreleased else len(missing_ids)
    if report.unmatched:
        report.notes.append(f'{len(report.unmatched)} local file(s) were not confirmed. Review them before treating any missing list as complete.')
    if report.errors:
        report.notes.append('Some lookups or local metadata checks failed. This is a partial report; read the error list.')
    return report


def _csv_safe(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value


def export_collections(report, path, format=None):
    path = Path(path)
    format = (format or path.suffix.lstrip('.')).casefold()
    data = report.to_dict() if isinstance(report, CollectionReport) else report
    if format == 'json':
        atomic_json(path, data)
        return
    if format != 'csv':
        raise ValueError('Choose JSON or CSV for a collections report.')
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ['collection_id', 'collection', 'status', 'movie_id', 'title', 'year', 'release_date', 'paths', 'reason']
    with path.open('w', encoding='utf-8-sig', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for collection in data['collections']:
            for category in ('owned', 'missing', 'upcoming'):
                for movie in collection[category]:
                    row = {'collection_id': collection['id'], 'collection': collection['name'], 'status': movie['status'],
                           'movie_id': movie['id'], 'title': movie['title'], 'year': movie['year'] or '',
                           'release_date': movie['release_date'], 'paths': ' | '.join(movie.get('paths', [])), 'reason': ''}
                    writer.writerow({key: _csv_safe(value) for key, value in row.items()})
        for movie in data['unmatched']:
            writer.writerow({'status': 'Unmatched', 'title': _csv_safe(movie.get('title', '')), 'year': movie.get('year') or '',
                             'paths': _csv_safe(movie['path']), 'reason': _csv_safe(movie['reason'])})
        for error in data.get('errors', []):
            writer.writerow({'collection_id': error.get('collection_id', ''), 'collection': _csv_safe(error.get('name', '')),
                             'status': 'Error', 'paths': _csv_safe(error.get('path', '')), 'reason': _csv_safe(error.get('error', ''))})
