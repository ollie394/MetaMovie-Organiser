"""State and credential checks using disposable files and a fake keyring."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import support


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv('MMO_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('MMO_PORTABLE', raising=False)
    monkeypatch.setattr(support, '_PORTABLE_OVERRIDE', False)
    monkeypatch.setattr(support, 'app_root', lambda: tmp_path / 'app')
    fake = SimpleNamespace(get_password=lambda service, key: None,
                           set_password=lambda service, key, value: None)
    monkeypatch.setitem(sys.modules, 'keyring', fake)


def test_state_override_is_used_even_for_portable_mode(tmp_path):
    expected = tmp_path / 'state'
    assert support.user_state_root() == expected
    assert support.state_root(False) == expected
    assert support.state_root(True) == expected
    support.check_state_writable(True)
    assert expected.is_dir()
    assert not (tmp_path / 'app' / 'data').exists()


def test_keyring_uses_mmo_service_and_never_mso(monkeypatch):
    saved = {}
    calls = []
    def write(service, key, value):
        calls.append(service)
        saved[service, key] = value
    def read(service, key):
        calls.append(service)
        return saved.get((service, key))
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(set_password=write, get_password=read))
    store = support.CredentialsStore(False)
    assert store.set('tmdb_key', 'disposable-key')
    assert store.get('tmdb_key') == 'disposable-key'
    assert calls and set(calls) == {'MetaMovie-Organiser'}


def test_settings_never_persist_keys_or_unknown_fields():
    support.save_settings({'language': 'fr', 'mode': 'copy', 'tmdb_key': 'private-fixture',
                           'cred_tmdb_key': 'legacy-fixture', 'unexpected': 123})
    saved = json.loads(support.settings_path().read_text())
    assert saved == {'language': 'fr', 'mode': 'copy'}
    assert 'private-fixture' not in support.settings_path().read_text()


def test_legacy_credentials_are_ignored_when_loading_settings():
    path = support.settings_path()
    support.atomic_json(path, {'language': 'de', 'cred_tmdb_key': 'private-fixture', 'tmdb_key': 'private-fixture'})
    settings = support.load_settings()
    assert settings['language'] == 'de'
    assert 'cred_tmdb_key' not in settings
    assert 'tmdb_key' not in settings


def test_damaged_json_falls_back_to_defaults():
    path = support.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text('{ invalid JSON', encoding='utf-8')
    settings = support.load_settings()
    assert settings['language'] == support.DEFAULT_SETTINGS['language']
    assert settings['mode'] == 'rename'


def test_keyring_failure_keeps_only_a_session_copy(monkeypatch):
    def unavailable(*args):
        raise RuntimeError('Test credential backend unavailable')
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(set_password=unavailable, get_password=unavailable))
    store = support.CredentialsStore(False)
    assert store.set('tmdb_key', 'session-fixture') is False
    assert store.get('tmdb_key') == 'session-fixture'
    assert support.CredentialsStore(False).get('tmdb_key') is None
    assert not store.vault_path.exists()


def test_portable_vault_encrypts_and_roundtrips():
    store = support.CredentialsStore(True)
    store.set_session_passphrase('disposable passphrase')
    assert store.set('tmdb_key', 'private-fixture-key')
    assert b'private-fixture-key' not in store.vault_path.read_bytes()
    reopened = support.CredentialsStore(True)
    reopened.set_session_passphrase('disposable passphrase')
    assert reopened.get('tmdb_key') == 'private-fixture-key'


def test_portable_vault_rejects_wrong_passphrase():
    store = support.CredentialsStore(True)
    store.set_session_passphrase('correct fixture')
    store.set('tmdb_key', 'fixture-value')
    reopened = support.CredentialsStore(True)
    reopened.set_session_passphrase('wrong fixture')
    with pytest.raises(RuntimeError, match='incorrect|damaged'):
        reopened.get('tmdb_key')


def test_portable_vault_rejects_damaged_header():
    store = support.CredentialsStore(True)
    store.vault_path.write_bytes(b'ZGFtYWdlZA==')
    store.set_session_passphrase('fixture passphrase')
    with pytest.raises(RuntimeError, match='Unsupported|damaged'):
        store.get('tmdb_key')


def test_failed_atomic_write_preserves_previous_settings(monkeypatch):
    path = support.settings_path()
    support.atomic_json(path, {'language': 'en'})
    def fail_replace(*args):
        raise OSError('Simulated replacement failure')
    monkeypatch.setattr(support.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='replacement failure'):
        support.atomic_json(path, {'language': 'fr'})
    assert json.loads(path.read_text()) == {'language': 'en'}
    assert list(path.parent.glob('*.tmp')) == []
