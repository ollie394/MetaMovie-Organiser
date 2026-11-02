"""Movie-only TheTVDB v4 provider.

Endpoint and response contracts are based on TheTVDB's official OpenAPI:
https://github.com/thetvdb/v4-api/blob/main/docs/swagger.yml
Search's language parameter filters a movie's primary language; it is not a
display-language preference. Display titles instead use returned translations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import difflib
import math
import re
import time
import unicodedata

import requests

from mmo_core import MovieError, MovieMatch
from support import check_cancel


class TVDBError(MovieError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _normalise(value):
    value = unicodedata.normalize('NFKD', str(value or ''))
    value = ''.join(character for character in value if not unicodedata.combining(character))
    return re.sub(r'[^\w]+', ' ', value.casefold()).strip()


def _year(value):
    value = str(value or '').strip()
    return int(value[:4]) if re.match(r'^\d{4}(?:$|-)', value) else None


def _movie_id(value):
    value = str(value or '').strip()
    if value.startswith('movie-'):
        value = value[6:]
    if not value.isdigit() or int(value) <= 0:
        raise TVDBError('TVDB returned an invalid movie identifier.')
    return int(value)


def _genres(values):
    if not isinstance(values, list):
        return []
    return [name for value in values
            if (name := str(value.get('name') or '').strip() if isinstance(value, dict)
                else str(value or '').strip())]


class TVDBClient:
    """API-key login, movie matching and translated movie details."""

    BASE_URL = 'https://api4.thetvdb.com/v4'
    MAX_ATTEMPTS = 5
    provider = 'TVDB'

    def __init__(self, key, language='en', cancel_event=None, pin=''):
        self.key = str(key or '').strip()
        if not self.key:
            raise TVDBError('Enter a TVDB API key or use filename-only matching.')
        self.pin = str(pin or '').strip()
        self.language = str(language or 'en').strip().lower().replace('_', '-').split('-')[0]
        self.cancel_event = cancel_event
        self.session = requests.Session()
        self.session.headers['Accept'] = 'application/json'
        self._token = None
        self._languages = None
        self._details_cache = {}

    def _wait(self, delay):
        if self.cancel_event is not None:
            self.cancel_event.wait(delay)
        else:
            time.sleep(delay)
        check_cancel(self.cancel_event)

    def _retry_delay(self, response, attempt):
        fallback = min(30.0, 0.6 * 2 ** attempt)
        value = response.headers.get('Retry-After') if response is not None else None
        if not value:
            return fallback
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = (date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return fallback
        return min(30.0, max(0.0, delay)) if math.isfinite(delay) else fallback

    def _request(self, method, endpoint, *, authenticated=True, **kwargs):
        if authenticated and not self._token:
            self._login()
        refreshed = False
        for attempt in range(self.MAX_ATTEMPTS):
            check_cancel(self.cancel_event)
            response = None
            try:
                response = self.session.request(method, self.BASE_URL + endpoint,
                                                timeout=(7, 25), allow_redirects=False, **kwargs)
                status = response.status_code
                if status == 401 and authenticated and not refreshed:
                    refreshed = True
                    self._token = None
                    self._login()
                    continue
                if status in (401, 403):
                    raise TVDBError('TVDB rejected this API key, subscriber PIN or account access.', status)
                if status == 429 or 500 <= status < 600:
                    if attempt + 1 == self.MAX_ATTEMPTS:
                        raise TVDBError('TVDB is unavailable or rate-limited. Try again later.', status)
                    self._wait(self._retry_delay(response, attempt))
                    continue
                if not 200 <= status < 300:
                    raise TVDBError(f'TVDB returned HTTP {status} for a movie-data request.', status)
                try:
                    payload = response.json()
                except ValueError:
                    raise TVDBError('TVDB returned unreadable JSON movie data.') from None
                if not isinstance(payload, dict):
                    raise TVDBError('TVDB returned an unexpected movie-data response.')
                if str(payload.get('status', 'success')).lower() not in {'success', 'ok'}:
                    raise TVDBError('TVDB reported an unsuccessful movie-data request.')
                return payload
            except requests.RequestException as exc:
                if attempt + 1 == self.MAX_ATTEMPTS:
                    # requests exception text may contain credential-bearing
                    # URLs or headers. Report the category, never its raw text.
                    raise TVDBError('Could not contact TVDB after retries (' + type(exc).__name__ + ').') from None
                self._wait(self._retry_delay(None, attempt))
            finally:
                if response is not None:
                    response.close()
        raise TVDBError('TVDB request failed after renewing its session.')

    def _login(self):
        check_cancel(self.cancel_event)
        self.session.headers.pop('Authorization', None)
        body = {'apikey': self.key}
        if self.pin:
            body['pin'] = self.pin
        payload = self._request('POST', '/login', authenticated=False, json=body)
        data = payload.get('data')
        token = data.get('token') if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise TVDBError('TVDB login returned no session token.')
        self._token = token.strip()
        self.session.headers['Authorization'] = 'Bearer ' + self._token

    def _get(self, endpoint, **params):
        return self._request('GET', endpoint, params=params)

    def _language_map(self):
        if self._languages is None:
            data = self._get('/languages').get('data')
            if not isinstance(data, list):
                raise TVDBError('TVDB returned an unexpected language list.')
            self._languages = {}
            for item in data:
                if not isinstance(item, dict) or not item.get('id'):
                    continue
                identifier = str(item['id']).lower()
                self._languages[identifier] = identifier
                if item.get('shortCode'):
                    self._languages[str(item['shortCode']).lower()] = identifier
        return self._languages

    def _language_code(self):
        languages = self._language_map()
        if self.language in languages:
            return languages[self.language]
        raise TVDBError('TVDB does not recognise the preferred language code. Choose a supported language in settings.')

    def _candidate(self, title, year, identifier, overview='', score=0.0,
                   original_title='', genres=None):
        candidate = MovieMatch(title=title, year=year, tmdb_id=None, overview=overview,
                               score=score, original_title=original_title, genres=genres or [])
        candidate.provider = 'TVDB'
        candidate.provider_id = identifier
        return candidate

    def test(self):
        self._languages = None
        self._language_map()
        return True

    def search(self, title, year=None):
        check_cancel(self.cancel_event)
        title = str(title or '').strip()
        if not title:
            raise TVDBError('Enter a movie title to search TVDB.')
        language = self._language_code()
        english = self._language_map().get('en')
        params = {'query': title, 'type': 'movie', 'limit': 50}
        if year is not None:
            params['year'] = int(year)
        data = self._get('/search', **params).get('data')
        if data == [] and year is not None:
            params.pop('year')
            data = self._get('/search', **params).get('data')
        if not isinstance(data, list):
            raise TVDBError('TVDB returned an unexpected movie-search response.')
        results, seen = [], set()
        for item in data:
            check_cancel(self.cancel_event)
            if not isinstance(item, dict) or str(item.get('type', '')).casefold() != 'movie':
                continue
            try:
                identifier = _movie_id(item.get('tvdb_id') or item.get('id') or item.get('objectID'))
            except TVDBError:
                continue
            if identifier in seen:
                continue
            seen.add(identifier)
            translations = item.get('translations')
            translations = translations if isinstance(translations, dict) else {}
            overviews = item.get('overviews')
            overviews = overviews if isinstance(overviews, dict) else {}
            original = str(item.get('name') or item.get('title') or '').strip()
            translated = translations.get(language) or translations.get(english) or original
            if not isinstance(translated, str) or not translated.strip():
                continue
            translated = translated.strip()
            result_year = _year(item.get('year')) or _year(item.get('first_air_time'))
            names = [translated, original]
            names += [alias for alias in item.get('aliases', []) if isinstance(alias, str)]
            score = max(difflib.SequenceMatcher(None, _normalise(title), _normalise(name)).ratio() for name in names)
            if year is not None and result_year is not None:
                score = max(0.0, score - min(abs(int(year) - result_year), 10) * 0.04)
            overview = overviews.get(language) or overviews.get(english) or item.get('overview') or ''
            results.append(self._candidate(translated, result_year, identifier, str(overview), score,
                                           original, _genres(item.get('genres'))))
        return sorted(results, key=lambda candidate: candidate.score, reverse=True)

    def details(self, identifier):
        identifier = _movie_id(identifier)
        if identifier in self._details_cache:
            return self._details_cache[identifier]
        language = self._language_code()
        english = self._language_map().get('en')
        data = self._get(f'/movies/{identifier}/extended', meta='translations', short='true').get('data')
        if not isinstance(data, dict) or _movie_id(data.get('id')) != identifier:
            raise TVDBError('TVDB returned an unexpected movie-detail record.')
        translations = data.get('translations')
        translations = translations if isinstance(translations, dict) else {}
        names = translations.get('nameTranslations') or []
        overviews = translations.get('overviewTranslations') or []
        def translated_value(entries, code, field):
            return next((str(item[field]).strip() for item in entries if isinstance(item, dict)
                         and item.get('language') == code and item.get(field)), '')
        name = translated_value(names, language, 'name')
        overview = translated_value(overviews, language, 'overview')
        if english:
            name = name or translated_value(names, english, 'name')
            overview = overview or translated_value(overviews, english, 'overview')
        # Some records omit expanded translations. The dedicated endpoint has
        # the same documented language IDs; a missing translation is a 404.
        if not translations:
            for code in dict.fromkeys(code for code in (language, english) if code):
                try:
                    item = self._get(f'/movies/{identifier}/translations/{code}').get('data')
                except TVDBError as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                if not isinstance(item, dict):
                    raise TVDBError('TVDB returned an unexpected movie translation.')
                name = name or str(item.get('name') or '').strip()
                overview = overview or str(item.get('overview') or '').strip()
                if name and overview:
                    break
        original = str(data.get('name') or '').strip()
        name = name or original
        if not name:
            raise TVDBError('TVDB movie details contain no usable title.')
        first_release = data.get('first_release')
        first_release = first_release if isinstance(first_release, dict) else {}
        year = _year(data.get('year')) or _year(first_release.get('date'))
        candidate = self._candidate(name, year, identifier, overview, 1.0, original, _genres(data.get('genres')))
        self._details_cache[identifier] = candidate
        return candidate

    def match(self, guess):
        candidates = self.search(guess.title, guess.year)
        exact = [candidate for candidate in candidates
                 if guess.year is not None and candidate.year == guess.year
                 and _normalise(guess.title) in {_normalise(candidate.title), _normalise(candidate.original_title)}]
        return (exact[0] if len(exact) == 1 else None), candidates
