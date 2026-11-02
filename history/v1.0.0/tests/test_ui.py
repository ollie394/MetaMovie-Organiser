"""Real Tk workflow checks with disposable media and no provider/keyring access."""

from pathlib import Path
import sys
import time
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import wave

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mmo
import support


@pytest.fixture(scope='module')
def tk_root():
    """Use one interpreter, matching the application's single-root lifetime."""
    try:
        root = tk.Tk()
    except tk.TclError:
        if sys.platform == 'win32':
            raise
        pytest.skip('A display is required for Tk workflow checks')
    root.withdraw()
    yield root
    for after_id in root.tk.call('after', 'info'):
        root.after_cancel(after_id)
    root.destroy()


@pytest.fixture
def ui(monkeypatch, tmp_path, tk_root):
    monkeypatch.setenv('MMO_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('MMO_PORTABLE', raising=False)
    monkeypatch.setattr(support, '_PORTABLE_OVERRIDE', False)
    monkeypatch.setattr(support, 'app_root', lambda: tmp_path / 'app')
    monkeypatch.setattr(support, 'configure_logging', lambda: None)
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(
        get_password=lambda service, key: None,
        set_password=lambda service, key, value: None,
    ))
    def no_http(*args, **kwargs):
        pytest.fail('GUI tests must not contact a metadata provider')
    monkeypatch.setattr(mmo.core.requests.Session, 'request', no_http)
    warnings, information, errors = [], [], []
    monkeypatch.setattr(mmo.messagebox, 'showwarning', lambda *args, **kwargs: warnings.append(args))
    monkeypatch.setattr(mmo.messagebox, 'showinfo', lambda *args, **kwargs: information.append(args))
    monkeypatch.setattr(mmo.messagebox, 'askyesno', lambda *args, **kwargs: True)
    root = tk_root
    root.report_callback_exception = lambda *args: errors.append(args)
    app = mmo.MovieApp(root)
    root.update()
    yield app, root, tmp_path, information
    app.cancel.set()
    if app.thread:
        app.thread.join(timeout=5)
    for after_id in root.tk.call('after', 'info'):
        root.after_cancel(after_id)
    root.unbind_all('<MouseWheel>')
    root.unbind('<Control-Return>')
    root.unbind('<F1>')
    for child in root.winfo_children():
        child.destroy()
    assert not errors, errors
    assert not warnings, warnings


def settle(app, root, timeout=8):
    limit = time.monotonic() + timeout
    while app.thread is not None and time.monotonic() < limit:
        root.update()
        time.sleep(0.01)
    root.update()
    assert app.thread is None, 'The GUI worker did not finish'
    assert not app.status.get().startswith('Could not finish'), app.status.get()


def test_tabs_and_rename_mode_disable_transfer_destination(ui):
    app, root, tmp_path, information = ui
    assert [app.notebook.tab(tab, 'text') for tab in app.notebook.tabs()] == [
        'Movie library', 'Media analytics', 'Embedded metadata', 'Connections & tools',
    ]
    assert app.mode.get() == 'Rename in place'
    assert str(app.destination_entry.cget('state')) == 'disabled'
    for tab in app.notebook.tabs():
        app.notebook.select(tab)
        root.update()
    app.mode.set('Copy to library')
    assert str(app.destination_entry.cget('state')) == 'normal'
    app.mode.set('Rename in place')
    assert str(app.destination_entry.cget('state')) == 'disabled'
    assert 'no video copying' in app.preview_note.get()


def test_preview_leaves_media_and_subtitles_untouched(ui):
    app, root, tmp_path, information = ui
    source = tmp_path / 'movies'
    source.mkdir()
    video = source / 'Example.Movie.2020.1080p.mkv'
    subtitle = video.with_suffix('.en.forced.srt')
    video.write_bytes(b'disposable movie contents')
    subtitle.write_text('disposable subtitle', encoding='utf-8')
    app.source.set(str(source))
    app.preview()
    settle(app, root)
    assert len(app.plan) == 1
    assert 'no files changed' in app.status.get()
    assert video.read_bytes() == b'disposable movie contents'
    assert subtitle.read_text() == 'disposable subtitle'
    assert list(source.iterdir()) == [video, subtitle] or set(source.iterdir()) == {video, subtitle}
    assert not (support.state_root() / 'manifests').exists()


def test_manual_match_then_rename_and_undo_never_copy_contents(ui, monkeypatch):
    app, root, tmp_path, information = ui
    source = tmp_path / 'movies'
    source.mkdir()
    original = source / 'Unresolved.Movie.mkv'
    original.write_bytes(b'movie data must remain the same file')
    identity = original.stat().st_ino
    app.source.set(str(source))
    app.preview()
    settle(app, root)
    assert not app.plan[0].resolved
    app.plan_tree.selection_set('0')
    app.edit_match()
    root.update()
    dialog = next(child for child in root.winfo_children()
                  if isinstance(child, tk.Toplevel) and child.title() == 'Review movie identity')
    entries = [widget for widget in mmo.descendants(dialog) if isinstance(widget, ttk.Entry)]
    assert len(entries) == 2
    for entry, value in zip(entries, ['Resolved Film', '2020']):
        entry.delete(0, 'end')
        entry.insert(0, value)
    button = next(widget for widget in mmo.descendants(dialog)
                  if isinstance(widget, ttk.Button) and widget.cget('text') == 'Use this title and year')
    button.invoke()
    root.update()
    assert app.plan[0].resolved
    target = next(item.dst for item in app.plan[0].files if item.kind == 'video')
    def no_copy(*args, **kwargs):
        pytest.fail('Rename-in-place must not call the transfer copier')
    monkeypatch.setattr(mmo.core, '_copy_verified', no_copy)
    app.apply_plan()
    settle(app, root)
    assert target.is_file() and not original.exists()
    assert target.stat().st_ino == identity
    app.undo_files()
    settle(app, root)
    assert original.read_bytes() == b'movie data must remain the same file'
    assert original.stat().st_ino == identity


def test_analytics_uses_native_headers_and_keeps_audio_streams(ui):
    app, root, tmp_path, information = ui
    source = tmp_path / 'movies'
    source.mkdir()
    # The extension makes it discoverable; MediaInfo must read the actual WAVE
    # header instead of trusting the filename's invented HDR/8K labels.
    path = source / 'Pretend.8K.HDR.Movie.2020.mp4'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\0' * 3200)
    app.source.set(str(source))
    app.analyse()
    settle(app, root)
    assert app.report is not None
    assert len(app.report.records) == 1
    record = app.report.records[0]
    assert record['container'] == 'Wave'
    assert len(record['audio']) == 1
    assert not record['video']
    assert app.metric_vars['HDR files'].get() == '0'
    assert app.metric_vars['Files'].get() == '1'


def test_embedded_metadata_requires_its_own_consent(ui, monkeypatch):
    app, root, tmp_path, information = ui
    path = tmp_path / 'Movie (2020).mp4'
    path.write_bytes(b'disposable data')
    app.metadata_path = path
    app.metadata_title.set('Movie')
    app.metadata_year.set('2020')
    def no_edit(*args, **kwargs):
        pytest.fail('Unconfirmed embedded metadata must remain untouched')
    monkeypatch.setattr(mmo.media, 'update_tags', no_edit)
    app.update_metadata()
    assert app.thread is None
    assert information
    assert path.read_bytes() == b'disposable data'


def test_source_and_scan_option_changes_invalidate_an_existing_preview(ui):
    app, root, tmp_path, information = ui
    source = tmp_path / 'movies'
    source.mkdir()
    (source / 'Example.Movie.2020.mkv').write_bytes(b'preview-only fixture')
    app.source.set(str(source))
    app.preview()
    settle(app, root)
    assert app.plan
    app.recursive.set(not app.recursive.get())
    assert app.plan == []
    assert app.plan_tree.get_children() == ()
    assert str(app.apply_button.cget('state')) == 'disabled'
    app.preview()
    settle(app, root)
    assert app.plan
    other = tmp_path / 'other movies'
    other.mkdir()
    app.source.set(str(other))
    assert app.plan == []
    assert str(app.apply_button.cget('state')) == 'disabled'


def test_destination_conflict_cannot_enable_apply(ui):
    app, root, tmp_path, information = ui
    source = tmp_path / 'movies'
    source.mkdir()
    original = source / 'Example.Movie.2020.mkv'
    original.write_bytes(b'original fixture')
    occupied = source / 'Example Movie (2020)'
    occupied.mkdir()
    sentinel = occupied / 'keep.txt'
    sentinel.write_text('existing folder contents', encoding='utf-8')
    app.source.set(str(source))
    app.recursive.set(False)
    app.preview()
    settle(app, root)
    assert len(app.plan) == 1
    assert app.plan[0].status == 'Conflict'
    assert str(app.apply_button.cget('state')) == 'disabled'
    app.plan_tree.selection_set('0')
    app.toggle_approval()
    assert str(app.apply_button.cget('state')) == 'disabled'
    assert original.read_bytes() == b'original fixture'
    assert sentinel.read_text() == 'existing folder contents'
