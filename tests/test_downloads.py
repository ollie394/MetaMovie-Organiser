"""Download failures must never become trusted cached archives."""
import hashlib
import importlib.util
import io
from urllib.error import URLError
from pathlib import Path

import pytest


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'prepare_tools', Path(__file__).parents[1] / 'scripts' / 'prepare_tools.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'CACHE', tmp_path / 'cache')
    monkeypatch.setattr(module, 'SOURCES', tmp_path / 'sources')
    return module


def test_bad_download_is_rejected_without_poisoning_cache(downloader, monkeypatch):
    artifact = {'url': 'https://example.invalid/archive.xz',
                'sha256': hashlib.sha256(b'archive').hexdigest()}
    monkeypatch.setattr(downloader, 'urlopen', lambda *a, **k: io.BytesIO(b'<html>error</html>'))
    with pytest.raises(RuntimeError, match='SHA-256 mismatch'):
        downloader.download_artifact(artifact)
    assert not (downloader.CACHE / 'archive.xz').exists()
    assert not (downloader.CACHE / 'archive.xz.part').exists()
    monkeypatch.setattr(downloader, 'urlopen', lambda *a, **k: io.BytesIO(b'archive'))
    assert downloader.download_artifact(artifact).read_bytes() == b'archive'


def test_invalid_existing_cache_is_replaced(downloader, monkeypatch):
    downloader.CACHE.mkdir()
    (downloader.CACHE / 'archive.xz').write_bytes(b'broken')
    artifact = {'url': 'https://example.invalid/archive.xz',
                'sha256': hashlib.sha256(b'archive').hexdigest()}
    monkeypatch.setattr(downloader, 'urlopen', lambda *a, **k: io.BytesIO(b'archive'))
    assert downloader.download_artifact(artifact).read_bytes() == b'archive'


def test_interrupted_download_retries_from_the_beginning(downloader, monkeypatch):
    artifact = {'url': 'https://example.invalid/archive.xz',
                'sha256': hashlib.sha256(b'archive').hexdigest()}
    calls = []
    def fetch(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise URLError('connection interrupted')
        return io.BytesIO(b'archive')
    monkeypatch.setattr(downloader, 'urlopen', fetch)
    monkeypatch.setattr(downloader.time, 'sleep', lambda seconds: None)
    assert downloader.download_artifact(artifact).read_bytes() == b'archive'
    assert len(calls) == 2


def test_connection_retries_are_bounded_and_partial_files_are_removed(downloader, monkeypatch):
    artifact = {'url': 'https://example.invalid/archive.xz',
                'sha256': hashlib.sha256(b'archive').hexdigest()}
    calls = []
    def fetch(*args, **kwargs):
        calls.append(args)
        raise URLError('server unavailable')
    monkeypatch.setattr(downloader, 'urlopen', fetch)
    monkeypatch.setattr(downloader.time, 'sleep', lambda seconds: None)
    with pytest.raises(URLError):
        downloader.download_artifact(artifact)
    assert len(calls) == 3
    assert not (downloader.CACHE / 'archive.xz.part').exists()
