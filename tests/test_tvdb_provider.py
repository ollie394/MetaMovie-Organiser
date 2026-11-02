"""Official TVDB movie-contract fixtures; no real credentials or HTTP calls."""

from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import sys
import threading

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mmo_core import MovieGuess
from support import Cancelled
from tvdb_provider import TVDBClient, TVDBError


def response(data=None, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result._content = json.dumps(data if data is not None else {'status': 'success'}).encode()
    result._content_consumed = True
    result.encoding = 'utf-8'
    return result


def login(token='fixture-token'):
    return response({'status': 'success', 'data': {'token': token}})


def languages():
    return response({'status': 'success', 'data': [
        {'id': 'eng', 'shortCode': 'en', 'name': 'English'},
        {'id': 'fra', 'shortCode': 'fr', 'name': 'French'},
    ]})


def movies(*items):
    return response({'status': 'success', 'data': list(items)})


def movie(identifier='123', title='Example Movie', year='2020', **extra):
    return {'tvdb_id': identifier, 'id': 'movie-' + identifier, 'type': 'movie',
            'name': title, 'year': year, **extra}


class FakeSession:
    def __init__(self, replies):
        self.replies = deque(replies)
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs, dict(self.headers)))
        assert self.replies, 'Unexpected TVDB request: ' + url
        item = self.replies.popleft()
        if isinstance(item, Exception):
            raise item
        return item


def make_client(monkeypatch, replies, language='en', pin='', event=None):
    session = FakeSession(replies)
    monkeypatch.setattr('tvdb_provider.requests.Session', lambda: session)
    client = TVDBClient('fixture-key', language, event, pin)
    delays = []
    monkeypatch.setattr(client, '_wait', lambda delay: delays.append(delay))
    return client, session, delays


@pytest.fixture(autouse=True)
def forbid_real_requests(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('TVDB tests must not contact a live provider')
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)


@pytest.mark.parametrize('pin,expected', [('', {'apikey': 'fixture-key'}),
                                        ('fixture-pin', {'apikey': 'fixture-key', 'pin': 'fixture-pin'})])
def test_login_omits_unissued_pin_and_uses_bearer_token(monkeypatch, pin, expected):
    client, session, _ = make_client(monkeypatch, [login(), languages()], pin=pin)
    assert not session.calls
    assert client.test() is True
    assert session.calls[0][0:2] == ('POST', client.BASE_URL + '/login')
    assert session.calls[0][2]['json'] == expected
    assert session.calls[1][3]['Authorization'] == 'Bearer fixture-token'
    assert all(call[2]['timeout'] == (7, 25) for call in session.calls)


def test_search_is_movie_only_and_provider_ids_never_become_tmdb_ids(monkeypatch):
    client, session, _ = make_client(monkeypatch, [login(), languages(), movies(
        movie(overview='Movie summary', genres=['Drama']),
        {'type': 'series', 'tvdb_id': '456', 'name': 'Example Movie', 'year': '2020'},
        {'type': 'person', 'tvdb_id': '789', 'name': 'Example Movie', 'year': '2020'},
        movie('not-a-number'),
    )])
    results = client.search('Example Movie', 2020)
    assert len(results) == 1
    assert results[0].provider == 'TVDB'
    assert results[0].provider_id == 123
    assert results[0].tmdb_id is None
    assert results[0].genres == ['Drama']
    params = session.calls[-1][2]['params']
    assert params == {'query': 'Example Movie', 'type': 'movie', 'limit': 50, 'year': 2020}
    assert 'language' not in params


def test_search_maps_preferred_language_to_returned_translations(monkeypatch):
    client, _, _ = make_client(monkeypatch, [login(), languages(), movies(movie(
        title='Original Movie', translations={'fra': 'Film Français', 'eng': 'English Movie'},
        overviews={'fra': 'Résumé français', 'eng': 'English summary'},
    ))], language='fr-FR')
    result = client.search('Film Français', 2020)[0]
    assert result.title == 'Film Français'
    assert result.overview == 'Résumé français'
    assert result.original_title == 'Original Movie'


def test_missing_preferred_translation_falls_back_to_english_then_original(monkeypatch):
    client, _, _ = make_client(monkeypatch, [login(), languages(), movies(
        movie(translations={'eng': 'English Movie'}), movie('124', 'Original Only'),
    )], language='fr')
    results = client.search('Movie', 2020)
    assert {item.title for item in results} == {'English Movie', 'Original Only'}


def test_year_filter_fallback_does_not_make_wrong_year_an_automatic_match(monkeypatch):
    client, session, _ = make_client(monkeypatch, [login(), languages(), movies(),
                                                 movies(movie(year='1990'))])
    chosen, candidates = client.match(MovieGuess('Example Movie', 2020))
    assert chosen is None
    assert candidates[0].year == 1990
    assert 'year' not in session.calls[-1][2]['params']


@pytest.mark.parametrize('items,guess,chosen_id', [
    ([movie()], MovieGuess('Example Movie', 2020), 123),
    ([movie()], MovieGuess('Example Movie'), None),
    ([movie(year='')], MovieGuess('Example Movie', 2020), None),
    ([movie(title='A Different Movie')], MovieGuess('Example Movie', 2020), None),
    ([movie(), movie('124')], MovieGuess('Example Movie', 2020), None),
])
def test_only_unique_exact_title_and_year_matches_are_automatic(monkeypatch, items, guess, chosen_id):
    client, _, _ = make_client(monkeypatch, [login(), languages(), movies(*items)])
    chosen, candidates = client.match(guess)
    assert (chosen.provider_id if chosen else None) == chosen_id
    assert candidates


def test_search_deduplicates_the_same_movie_record(monkeypatch):
    client, _, _ = make_client(monkeypatch, [login(), languages(), movies(movie(), movie())])
    chosen, candidates = client.match(MovieGuess('Example Movie', 2020))
    assert len(candidates) == 1 and chosen.provider_id == 123


def test_expired_token_is_renewed_once_and_request_is_retried(monkeypatch):
    client, session, _ = make_client(monkeypatch, [login('old-token'), response(status=401),
                                                 login('new-token'), languages()])
    assert client.test() is True
    assert [call[0] for call in session.calls] == ['POST', 'GET', 'POST', 'GET']
    assert session.calls[-1][3]['Authorization'] == 'Bearer new-token'


def test_repeated_401_stops_after_one_token_renewal(monkeypatch):
    client, session, _ = make_client(monkeypatch, [login(), response(status=401),
                                                 login('new-token'), response(status=401)])
    with pytest.raises(TVDBError, match='rejected'):
        client.test()
    assert len(session.calls) == 4


def test_retry_after_and_transient_server_errors_are_bounded(monkeypatch):
    client, _, delays = make_client(monkeypatch, [login(), response(status=429, headers={'Retry-After': '999'}),
                                                 response(status=503), languages()])
    assert client.test()
    assert delays[0] == 30.0
    assert len(delays) == 2


def test_retry_after_http_date_is_supported(monkeypatch):
    client, _, _ = make_client(monkeypatch, [])
    value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=15), usegmt=True)
    assert 0 < client._retry_delay(response(status=429, headers={'Retry-After': value}), 0) <= 15
    assert client._retry_delay(response(headers={'Retry-After': 'NaN'}), 0) == 0.6


def test_connection_errors_do_not_leak_keys_pins_or_tokens(monkeypatch):
    error = requests.ConnectionError('apikey=fixture-key pin=fixture-pin Bearer fixture-token')
    client, session, delays = make_client(monkeypatch, [error] * 5, pin='fixture-pin')
    with pytest.raises(TVDBError) as caught:
        client.test()
    assert all(secret not in str(caught.value) for secret in ['fixture-key', 'fixture-pin', 'fixture-token'])
    assert len(session.calls) == 5 and len(delays) == 4


def test_retry_wait_is_cancellable_before_any_retry(monkeypatch):
    event = threading.Event()
    client, session, _ = make_client(monkeypatch, [login(), response(status=429)], event=event)
    def cancel_wait(delay):
        event.set()
        TVDBClient._wait(client, 0)
    monkeypatch.setattr(client, '_wait', cancel_wait)
    with pytest.raises(Cancelled):
        client.test()
    assert len(session.calls) == 2


def test_already_cancelled_client_does_not_make_requests(monkeypatch):
    event = threading.Event()
    event.set()
    client, session, _ = make_client(monkeypatch, [], event=event)
    with pytest.raises(Cancelled):
        client.test()
    assert session.calls == []


def test_details_use_movie_extended_translations_and_first_release_year(monkeypatch):
    item = {'id': 123, 'name': 'Original Title', 'first_release': {'date': '2020-06-01'},
            'genres': [{'id': 1, 'name': 'Drama'}], 'translations': {
                'nameTranslations': [{'language': 'fra', 'name': 'Titre Français'}],
                'overviewTranslations': [{'language': 'eng', 'overview': 'English fallback'}],
            }}
    client, session, _ = make_client(monkeypatch, [login(), languages(), response({'data': item})], language='fr')
    result = client.details(123)
    assert result.title == 'Titre Français' and result.year == 2020
    assert result.overview == 'English fallback'
    assert result.genres == ['Drama']
    assert result.provider_id == 123 and result.tmdb_id is None
    assert session.calls[-1][1].endswith('/movies/123/extended')
    assert session.calls[-1][2]['params'] == {'meta': 'translations', 'short': 'true'}
    assert client.details(123) is result  # Details are cached within this client.


def test_dedicated_translation_falls_back_on_404_without_hiding_other_errors(monkeypatch):
    client, session, _ = make_client(monkeypatch, [login(), languages(), response({'data': {
        'id': 123, 'name': 'Original Title', 'year': '2020',
    }}), response(status=404), response({'data': {'name': 'English Title', 'overview': 'Summary'}})], language='fr')
    result = client.details(123)
    assert result.title == 'English Title'
    assert session.calls[-2][1].endswith('/movies/123/translations/fra')
    assert session.calls[-1][1].endswith('/movies/123/translations/eng')


@pytest.mark.parametrize('reply', [response(status=403), response({'status': 'failure', 'message': 'token=fixture-token'}),
                                    response({'data': 'not-a-list'}), response([])])
def test_authentication_and_malformed_data_errors_remain_visible_and_safe(monkeypatch, reply):
    client, _, _ = make_client(monkeypatch, [login(), reply])
    with pytest.raises(TVDBError) as caught:
        client.test()
    assert 'fixture-token' not in str(caught.value)


def test_invalid_json_is_reported_without_response_text(monkeypatch):
    invalid = response()
    invalid._content = b'not-json token=fixture-token'
    client, _, _ = make_client(monkeypatch, [login(), invalid])
    with pytest.raises(TVDBError, match='unreadable JSON'):
        client.test()


def test_connection_test_reaches_provider_even_after_language_cache(monkeypatch):
    client, session, _ = make_client(monkeypatch, [login(), languages(), languages()])
    assert client.test() and client.test()
    assert len(session.calls) == 3


def test_login_without_a_token_is_an_error(monkeypatch):
    client, _, _ = make_client(monkeypatch, [response({'data': {}})])
    with pytest.raises(TVDBError, match='no session token'):
        client.test()
