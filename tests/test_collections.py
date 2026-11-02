"""Collections use fake provider responses and disposable filenames only."""
from __future__ import annotations

import copy
import csv
import json
import threading
import time
from datetime import date
from pathlib import Path

import pytest

import collections_tools as collections
import mmo_core as core
from support import Cancelled


def details(identifier, title, released, collection_id=10):
    return {'id': identifier, 'title': title, 'original_title': title, 'release_date': released,
            'belongs_to_collection': {'id': collection_id, 'name': 'Alpha Collection'} if collection_id else None}


@pytest.fixture
def provider(monkeypatch):
    movies = {1: details(1, 'Alpha', '2000-01-01'), 2: details(2, 'Beta', '2001-02-03'),
              5: details(5, 'Stand Alone', '1999-04-05', None),
              6: details(6, 'Remake', '2020-01-01', None), 7: details(7, 'Remake', '2020-02-02', None)}
    parts = [{'id': 1, 'title': 'Alpha', 'release_date': '2000-01-01'},
             {'id': 2, 'title': 'Beta', 'release_date': '2001-02-03'},
             {'id': 3, 'title': 'Third Film', 'release_date': '2002-03-04'},
             {'id': 4, 'title': 'Future Film', 'release_date': '2027-01-01'},
             {'id': 8, 'title': 'TBA Film', 'release_date': ''},
             {'id': 9, 'title': 'Released Today', 'release_date': '2026-11-02'},
             {'id': 10, 'title': 'Still Planned', 'release_date': '2026-01-01', 'status': 'Planned'},
             {'id': 11, 'title': 'A TV Series', 'release_date': '2001-01-01', 'media_type': 'tv'},
             {'id': 12, 'title': 'Other Video', 'release_date': '2001-01-01', 'video': True}]
    state = {'movies': movies, 'collections': {10: {'id': 10, 'name': 'Alpha Collection', 'parts': parts}},
             'calls': [], 'fail_movies': set(), 'fail_collections': set(), 'closed': 0,
             'cancel_on_movie': None}

    class Session:
        def close(self):
            state['closed'] += 1

    class FakeClient:
        match = core.TMDBClient.match
        def __init__(self, key, language='en', cancel_event=None):
            if not key:
                raise core.MovieError('A TMDB key is required')
            self.language, self.cancel_event, self.session = language, cancel_event, Session()

        def search(self, title, year=None):
            state['calls'].append(('search', title, year))
            return [core.MovieMatch(movie['title'], int(movie['release_date'][:4]), movie['id'],
                                   original_title=movie['original_title'])
                    for movie in state['movies'].values()
                    if collections._normalise(title) in {collections._normalise(movie['title']), collections._normalise(movie['original_title'])}]

        def _get(self, endpoint, **params):
            state['calls'].append(('get', endpoint, params))
            category, identifier = endpoint.strip('/').split('/')
            identifier = int(identifier)
            if category == 'movie':
                if identifier in state['fail_movies']:
                    raise core.MovieError('Temporary movie lookup failure')
                response = copy.deepcopy(state['movies'][identifier])
                if state['cancel_on_movie'] == identifier and self.cancel_event:
                    self.cancel_event.set()
                return response
            if identifier in state['fail_collections']:
                raise core.MovieError('Temporary collection lookup failure')
            return copy.deepcopy(state['collections'][identifier])

    monkeypatch.setattr(collections, 'TMDBClient', FakeClient)
    monkeypatch.setattr(core.requests.sessions.Session, 'request', lambda *args, **kwargs: pytest.fail('Collections tests must never access HTTP'))
    return state


def movie(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'disposable filename fixture, not real movie content')
    return path


def analyse(paths, **kwargs):
    return collections.analyse_collections(paths, 'test-private-key', today=date(2026, 11, 2), **kwargs)


def test_inventory_deduplicates_versions_and_separates_missing_future_and_tba(tmp_path, provider):
    paths = [movie(tmp_path, 'Alpha.2000.1080p.mkv'), movie(tmp_path, 'Alpha.2000.2160p.mp4'),
             movie(tmp_path, 'Beta (2001).mp4'), movie(tmp_path, 'Stand Alone (1999).mp4'),
             movie(tmp_path, 'Unknown Movie.mkv'), movie(tmp_path, 'Remake (2020).mp4')]
    report = analyse(paths)
    assert report.stats == {'library_paths': 6, 'confirmed_paths': 4, 'unique_movies': 3, 'collections': 1,
                            'missing': 2, 'upcoming': 3, 'unmatched': 2, 'errors': 0,
                            'complete_collections': 0, 'incomplete_collections': 1, 'non_collection_movies': 1, 'wanted': 2}
    collection = report.collections[0]
    assert [item['id'] for item in collection['owned']] == [1, 2]
    assert len(collection['owned'][0]['paths']) == 2
    assert {item['id'] for item in collection['missing']} == {3, 9}
    assert all(item['status'] == collections.MISSING_STATUS for item in collection['missing'])
    assert {item['id'] for item in collection['upcoming']} == {4, 8, 10}
    assert not collection['complete']
    assert any('Unmatched files may affect completeness' in note for note in report.notes)
    assert len([call for call in provider['calls'] if call[:2] == ('search', 'Alpha')]) == 1
    assert provider['closed'] == 1


def test_future_option_adds_wanted_without_claiming_future_movies_are_missing(tmp_path, provider):
    path = movie(tmp_path, 'Alpha (2000).mkv')
    report = analyse([path], include_unreleased=True)
    assert report.stats['missing'] == 3
    assert report.stats['upcoming'] == 3
    assert report.stats['wanted'] == 6
    assert len(report.collections[0]['wanted']) == 6
    assert all(item['id'] not in {4, 8, 10} for item in report.collections[0]['missing'])


def test_complete_released_collection_remains_complete_with_future_part(tmp_path, provider):
    provider['collections'][10]['parts'] = provider['collections'][10]['parts'][:2] + [provider['collections'][10]['parts'][3]]
    report = analyse([movie(tmp_path, 'Alpha (2000).mkv'), movie(tmp_path, 'Beta (2001).mkv')])
    assert report.collections[0]['complete'] and report.stats['missing'] == 0
    assert report.stats['complete_collections'] == 1 and report.stats['upcoming'] == 1


def test_current_date_is_injectable_and_newly_released_parts_are_reclassified(tmp_path, provider):
    path = movie(tmp_path, 'Alpha (2000).mkv')
    before = collections.analyse_collections([path], 'test', today='2026-11-01')
    after = collections.analyse_collections([path], 'test', today='2027-01-02')
    assert 9 not in {item['id'] for item in before.collections[0]['missing']}
    assert {4, 9} <= {item['id'] for item in after.collections[0]['missing']}
    assert 8 not in {item['id'] for item in after.collections[0]['missing']}
    with pytest.raises(ValueError):
        collections.analyse_collections([path], 'test', today='2026')


def test_unknown_and_ambiguous_names_never_become_automatic_matches(tmp_path, provider):
    report = analyse([movie(tmp_path, 'Remake (2020).mkv'), movie(tmp_path, 'Movie without year.mkv')])
    assert report.stats['unique_movies'] == 0 and report.stats['unmatched'] == 2
    assert not [call for call in provider['calls'] if call[0] == 'get']
    assert report.collections == [] and report.errors == []


def test_reviewed_tmdb_id_can_identify_a_badly_named_file_without_search(tmp_path, provider):
    path = movie(tmp_path, 'Unidentified release.mkv')
    report = analyse([path], known_matches={str(path): 1})
    assert report.stats['unique_movies'] == 1 and not report.unmatched
    assert not [call for call in provider['calls'] if call[0] == 'search']


def test_tvdb_identifier_is_never_sent_as_a_tmdb_movie_id(tmp_path, provider):
    path = movie(tmp_path, 'Alpha (2000).mkv')
    report = analyse([path], known_matches={path: {'provider': 'TVDB', 'provider_id': 999}})
    assert report.stats['unique_movies'] == 1
    assert ('get', '/movie/999', {'language': 'en'}) not in provider['calls']
    assert any(call[0] == 'search' for call in provider['calls'])


def test_local_nfo_id_is_checked_against_details_and_never_follows_urls(tmp_path, provider):
    path = movie(tmp_path, 'Alpha (2000).mkv')
    path.with_suffix('.nfo').write_text('<movie><title>Alpha</title><year>2000</year><uniqueid type="tmdb">1</uniqueid><url>https://untrusted.invalid/film</url></movie>')
    report = analyse([path])
    assert report.stats['unique_movies'] == 1
    assert not [call for call in provider['calls'] if call[0] == 'search']
    assert all('untrusted' not in str(call) for call in provider['calls'])
    path.with_suffix('.nfo').write_text('<movie><uniqueid type="tmdb">2</uniqueid></movie>')
    bad = analyse([path])
    assert bad.stats['unique_movies'] == 0 and 'does not agree' in bad.unmatched[0]['reason']


def test_nfo_metadata_and_filename_disagreement_requires_review(tmp_path, provider):
    path = movie(tmp_path, 'Alpha (2000).mkv')
    path.with_suffix('.nfo').write_text('<movie><title>Beta</title><year>2001</year><uniqueid type="tmdb">2</uniqueid></movie>')
    report = analyse([path])
    assert report.stats['unique_movies'] == 0 and 'disagree' in report.unmatched[0]['reason']
    assert not provider['calls']


@pytest.mark.parametrize('identifier', ['0', '-3', 'https://untrusted.invalid/id', '2147483648'])
def test_invalid_nfo_identifier_is_not_used(tmp_path, provider, identifier):
    path = movie(tmp_path, 'Alpha (2000).mkv')
    path.with_suffix('.nfo').write_text(f'<movie><uniqueid type="tmdb">{identifier}</uniqueid></movie>')
    report = analyse([path])
    assert report.stats['unique_movies'] == 0 and report.errors
    assert not provider['calls']


def test_nfo_entities_and_external_paths_are_not_read(tmp_path, provider, monkeypatch):
    path = movie(tmp_path, 'Alpha (2000).mkv')
    secret = tmp_path / 'not-for-reading.txt'
    secret.write_text('private sentinel')
    path.with_suffix('.nfo').write_text(f'<!DOCTYPE movie [<!ENTITY local SYSTEM "{secret.as_uri()}">]><movie><title>&local;</title></movie>')
    original = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda item: pytest.fail('External entity file must not be read') if item == secret else original(item))
    report = analyse([path])
    assert report.stats['unique_movies'] == 1
    assert any(error['stage'] == 'nfo' and 'entity' in error['error'] for error in report.errors)


def test_lookup_errors_are_partial_not_zero_success(tmp_path, provider):
    provider['fail_movies'].add(1)
    paths = [movie(tmp_path, 'Alpha (2000).mkv'), movie(tmp_path, 'Stand Alone (1999).mkv')]
    report = analyse(paths)
    assert report.stats['unique_movies'] == 1 and report.stats['unmatched'] == 1 and report.stats['errors'] == 1
    assert any('partial report' in note for note in report.notes)


def test_collection_failure_keeps_confirmed_movie_count_and_reports_error(tmp_path, provider):
    provider['fail_collections'].add(10)
    report = analyse([movie(tmp_path, 'Alpha (2000).mkv')])
    assert report.stats['unique_movies'] == 1 and report.stats['collections'] == 0
    assert report.errors[0]['stage'] == 'collection'
    assert report.stats['errors'] == 1


def test_empty_or_malformed_collection_is_not_reported_as_complete(tmp_path, provider):
    provider['collections'][10]['parts'] = []
    report = analyse([movie(tmp_path, 'Alpha (2000).mkv')])
    assert report.collections == [] and report.errors


def test_invalid_member_prevents_false_complete_collection(tmp_path, provider):
    provider['collections'][10]['parts'] = [{'id': 1, 'title': 'Alpha', 'release_date': '2000-01-01'},
                                          {'title': 'Unidentified member', 'release_date': '2001-01-01'}]
    report = analyse([movie(tmp_path, 'Alpha (2000).mkv')])
    assert len(report.collections) == 1 and report.stats['missing'] == 0
    assert not report.collections[0]['complete'] and not report.collections[0]['membership_complete']
    assert report.errors and report.stats['complete_collections'] == 0


def test_movie_identity_mismatch_is_not_accepted_from_provider(tmp_path, provider):
    provider['movies'][1]['id'] = 2
    path = movie(tmp_path, 'Alpha (2000).mkv')
    report = analyse([path], known_matches={path: 1})
    assert report.stats['unique_movies'] == 0 and report.errors


def test_cache_reuses_local_confirmations_without_storing_key_or_paths(tmp_path, provider):
    path = movie(tmp_path / 'private folder', 'Alpha (2000).mkv')
    cache = tmp_path / 'collections-cache.json'
    first = analyse([path], cache_path=cache)
    assert provider['calls']
    provider['calls'].clear()
    second = analyse([path], cache_path=cache)
    assert not provider['calls']
    assert second.stats == first.stats
    content = cache.read_text(encoding='utf-8')
    assert 'test-private-key' not in content and str(path) not in content and 'private folder' not in content
    assert all(str(path) not in str(call) for call in provider['calls'])


def test_upcoming_membership_cache_expires_sooner_and_manual_refresh_bypasses_it(tmp_path, provider):
    path = movie(tmp_path, 'Alpha (2000).mkv')
    cache = tmp_path / 'cache.json'
    analyse([path], cache_path=cache)
    saved = json.loads(cache.read_text())
    entry = next(iter(saved['collections'].values()))
    assert entry['ttl'] == collections.UPCOMING_TTL
    entry['fetched_at'] = time.time() - 2 * 24 * 60 * 60
    cache.write_text(json.dumps(saved))
    provider['calls'].clear()
    analyse([path], cache_path=cache)
    assert [call for call in provider['calls'] if call[0] == 'get'] == [('get', '/collection/10', {'language': 'en'})]
    provider['calls'].clear()
    analyse([path], cache_path=cache, force_refresh=True)
    assert any(call[0] == 'search' for call in provider['calls'])
    assert any(call[:2] == ('get', '/movie/1') for call in provider['calls'])


def test_cancellation_keeps_valid_cache_without_claiming_a_complete_report(tmp_path, provider):
    path = movie(tmp_path, 'Alpha (2000).mkv')
    event = threading.Event()
    provider['cancel_on_movie'] = 1
    cache = tmp_path / 'cache.json'
    with pytest.raises(Cancelled):
        analyse([path], cancel_event=event, cache_path=cache)
    assert cache.exists() and json.loads(cache.read_text())['movies']
    assert provider['closed'] == 1


def test_json_and_csv_exports_include_uncertainty_errors_and_formula_protection(tmp_path, provider):
    report = analyse([movie(tmp_path, 'Alpha (2000).mkv'), movie(tmp_path, 'Unmatched Movie.mkv')])
    report.collections[0]['name'] = '=unsafe collection formula'
    report.errors.append({'stage': 'example', 'error': '+unsafe error formula'})
    json_path, csv_path = tmp_path / 'report.json', tmp_path / 'report.csv'
    collections.export_collections(report, json_path)
    collections.export_collections(report, csv_path)
    saved = json.loads(json_path.read_text(encoding='utf-8'))
    assert saved['notes'][0] == collections.UNCERTAINTY_NOTE
    assert saved['as_of'] == '2026-11-02'
    with csv_path.open(encoding='utf-8-sig', newline='') as source:
        rows = list(csv.DictReader(source))
    assert any(row['status'] == 'Unmatched' for row in rows)
    assert any(row['status'] == 'Error' and row['reason'].startswith("'+") for row in rows)
    assert all(row['collection'].startswith("'=") for row in rows if row['collection'])
    with pytest.raises(ValueError):
        collections.export_collections(report, tmp_path / 'not-supported.xml')


def test_empty_input_and_missing_key_are_explicit(tmp_path, provider):
    report = analyse([])
    assert report.stats['library_paths'] == 0 and report.stats['unique_movies'] == 0 and not provider['calls']
    with pytest.raises(core.MovieError, match='key is required'):
        collections.analyse_collections([], '')
