"""Offline movie safety tests. Only disposable files are ever modified."""
import json
import os
import threading
from pathlib import Path

import pytest

import mmo_core as core


@pytest.mark.parametrize('name,title,year,part', [
    ('A.Haunted.House.2.2014.1080p.BluRay.x264.YIFY.mp4', 'A Haunted House 2', 2014, ''),
    ('1917 (2019).mkv', '1917', 2019, ''),
    ('1917.mkv', '1917', None, ''),
    ('2001.A.Space.Odyssey.1968.1080p.mkv', '2001 A Space Odyssey', 1968, ''),
    ('2012 (2009).mp4', '2012', 2009, ''),
    ('The Twilight Saga - Breaking Dawn - Part 2 (2012).mkv', 'The Twilight Saga - Breaking Dawn - Part 2', 2012, ''),
    ('The Dark Knight (2008) - pt1.mkv', 'The Dark Knight', 2008, 'pt1'),
    ('The.Dark.Knight.2008.1080p.cd2.mkv', 'The Dark Knight', 2008, 'pt2'),
    ('Avatar (2009) {tmdb-19995}.mkv', 'Avatar', 2009, ''),
    ('Batman.Begins.2005.720p.WEB-DL.mp4', 'Batman Begins', 2005, ''),
    ('Example.Movie.2020.1080p.REGROUP.2026.mp4', 'Example Movie', 2020, ''),
])
def test_release_filename_parsing(tmp_path, name, title, year, part):
    guess = core.parse_movie(tmp_path / name)
    assert (guess.title, guess.year, guess.part) == (title, year, part)


def test_folder_supplies_title_year_for_generic_filename(tmp_path):
    guess = core.parse_movie(tmp_path / 'A Haunted House 2 (2014) [1080p]' / 'movie.mp4')
    assert (guess.title, guess.year) == ('A Haunted House 2', 2014)


def film(root, folder='Old Folder', name='Example.Movie.2020.1080p.mp4', payload=b'movie-bytes'):
    parent = root / folder if folder else root
    parent.mkdir(parents=True, exist_ok=True)
    video = parent / name
    video.write_bytes(payload)
    return video


def test_scan_ignores_samples_extras_destination_and_links(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source)
    film(source, 'Extras', 'Extra (2020).mp4')
    film(source, 'sample', 'Sample (2020).mp4')
    film(source, '.grab', 'Hidden (2020).mp4')
    film(source, 'Output', 'Organised (2020).mp4')
    (video.parent / 'Example.Movie.2020.sample.mp4').write_bytes(b'sample')
    assert core.discover_movies(source, exclude=source / 'Output') == [video]
    assert core.discover_movies(source, recursive=False) == []
    if os.name != 'nt':
        (source / 'link').symlink_to(video.parent, target_is_directory=True)
        assert core.discover_movies(source, exclude=source / 'Output') == [video]


def test_existing_movie_folder_and_movie_rename_without_content_read(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    video = film(source, 'A Haunted House 2 (2014) [1080p]', 'A.Haunted.House.2.2014.1080p.BluRay.x264.YIFY.mp4')
    old_folder = video.parent
    inode = video.stat().st_ino
    subtitle = old_folder / (video.stem + '.en.forced.srt')
    subtitle.write_text('subtitle')
    (old_folder / 'poster.jpg').write_bytes(b'poster')
    (old_folder / 'notes.txt').write_text('keep this')
    plan = core.build_plan(source)
    assert len(plan) == 1 and plan[0].resolved
    monkeypatch.setattr(core, '_hash_file', lambda *args, **kwargs: pytest.fail('Rename must never hash movie bytes'))
    monkeypatch.setattr(core, '_copy_verified', lambda *args, **kwargs: pytest.fail('Rename must never copy movie bytes'))
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    destination = source / 'A Haunted House 2 (2014)' / 'A Haunted House 2 (2014).mp4'
    assert result.completed == 1 and result.failures == 0
    assert not old_folder.exists()
    assert destination.read_bytes() == b'movie-bytes'
    assert destination.stat().st_ino == inode
    assert destination.with_suffix('.en.forced.srt').read_text() == 'subtitle'
    assert (destination.parent / 'poster.jpg').read_bytes() == b'poster'
    assert (destination.parent / 'notes.txt').read_text() == 'keep this'
    restored = core.undo_manifest(result.manifest)
    assert restored.failures == 0
    assert video.read_bytes() == b'movie-bytes' and video.stat().st_ino == inode
    assert subtitle.read_text() == 'subtitle'
    assert (old_folder / 'notes.txt').exists()


def test_flat_folder_gets_separate_directories_without_root_rename(tmp_path):
    source = tmp_path / 'Movies'
    first = film(source, '', 'Avatar (2009).mkv')
    second = film(source, '', 'Batman.Begins.2005.mp4')
    (source / 'poster.jpg').write_bytes(b'ambiguous')
    plan = core.build_plan(source)
    assert all(group.folder_source is None for group in plan)
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 2
    assert (source / 'Avatar (2009)' / 'Avatar (2009).mkv').exists()
    assert (source / 'Batman Begins (2005)' / 'Batman Begins (2005).mp4').exists()
    assert (source / 'poster.jpg').read_bytes() == b'ambiguous'
    assert core.undo_manifest(result.manifest).failures == 0
    assert first.exists() and second.exists()
    assert sorted(path.name for path in source.iterdir()) == [first.name, second.name, 'poster.jpg']


def test_shared_folder_is_never_renamed(tmp_path):
    source = tmp_path / 'Movies'
    film(source, 'Mixed', 'First (2001).mp4')
    film(source, 'Mixed', 'Second (2002).mp4')
    plan = core.build_plan(source)
    assert all(group.folder_source is None for group in plan)
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 2 and (source / 'Mixed').is_dir()
    assert (source / 'Mixed' / 'First (2001)' / 'First (2001).mp4').exists()


def test_split_parts_keep_sequel_title_and_subtitles(tmp_path):
    source = tmp_path / 'Movies'
    first = film(source, 'Old', 'Example (2020) - pt1.mp4')
    second = film(source, 'Old', 'Example (2020) - pt2.mp4')
    (first.parent / (first.stem + '.en.srt')).write_text('part one')
    plan = core.build_plan(source)
    assert len(plan) == 1 and len(plan[0].video_paths) == 2
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 1
    assert (source / 'Example (2020)' / first.name).exists()
    assert (source / 'Example (2020)' / second.name).exists()
    assert (source / 'Example (2020)' / 'Example (2020) - pt1.en.srt').exists()


def test_language_subtitles_and_vobsub_pair(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source)
    for name in ['en.srt', video.stem + '.en.idx', video.stem + '.en.sub']:
        (video.parent / name).write_text('subtitle')
    plan = core.build_plan(source)
    assert sum(item.kind == 'subtitle' for item in plan[0].files) == 3
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 1 and result.failures == 0
    destination = source / 'Example Movie (2020)'
    assert all((destination / ('Example Movie (2020).en' + extension)).exists() for extension in ['.srt', '.idx', '.sub'])


def test_unknown_year_requires_review(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source, '', 'Unknown.Movie.mp4')
    plan = core.build_plan(source)
    assert not plan[0].resolved and not plan[0].approved
    assert core.execute_plan(plan, journal_dir=tmp_path / 'journals').completed == 0
    core.revise_group(plan[0], 'Known Movie', 2022)
    assert plan[0].resolved
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 1 and not video.exists()


def test_ambiguous_match_never_auto_approved(tmp_path):
    video = film(tmp_path / 'Movies')
    candidates = [core.MovieMatch('Example Movie', 2020, 1), core.MovieMatch('Example Movie', 2020, 2)]
    plan = core.build_plan(video.parent.parent, matcher=lambda guess: (None, candidates))
    assert plan[0].status == 'Choose movie' and not plan[0].approved
    core.revise_group(plan[0], candidates[1].title, candidates[1].year, candidates[1].tmdb_id)
    assert plan[0].tmdb_id == 2 and plan[0].approved


def test_destination_and_duplicate_movies_are_blocked(tmp_path):
    source = tmp_path / 'Movies'
    film(source, 'One', 'Example.Movie.2020.mp4')
    film(source, 'Two', 'Example.Movie.2020.mkv')
    output = tmp_path / 'Output'
    plan = core.build_plan(source, output, mode='copy')
    assert all(group.status == 'Conflict' for group in plan)
    result = core.execute_plan(plan, mode='copy', journal_dir=tmp_path / 'journals')
    assert result.completed == 0 and not output.exists()


def test_destination_appearing_after_preview_is_never_overwritten(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source)
    plan = core.build_plan(source)
    occupied = source / 'Example Movie (2020)'
    occupied.mkdir()
    (occupied / 'existing.txt').write_text('keep')
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 0 and video.exists()
    assert (occupied / 'existing.txt').read_text() == 'keep'


def test_changed_source_or_folder_blocks_rename(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source)
    plan = core.build_plan(source)
    (video.parent / 'new.txt').write_text('appeared after preview')
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.failures == 1 and video.exists()


def test_undo_does_not_rename_folder_with_new_unknown_child(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source)
    result = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals')
    destination = source / 'Example Movie (2020)'
    (destination / 'new.txt').write_text('unrelated')
    undo = core.undo_manifest(result.manifest)
    assert undo.failures == 1
    assert destination.is_dir() and (destination / 'new.txt').read_text() == 'unrelated'
    assert (destination / video.name).exists()  # Known video was restored before protecting the folder.
    assert not json.loads(result.manifest.read_text())['undone']


def test_undo_leaves_modified_video_and_occupied_original(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source)
    result = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals')
    destination = result.paths[0]
    destination.write_bytes(b'modified-longer')
    assert core.undo_manifest(result.manifest).failures >= 1
    assert destination.read_bytes() == b'modified-longer'


def test_case_only_rename_roundtrip(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source, 'example movie (2020)', 'example movie (2020).mp4')
    plan = core.build_plan(source)
    core.revise_group(plan[0], 'Example Movie', 2020)
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.failures == 0
    assert (source / 'Example Movie (2020)' / 'Example Movie (2020).mp4').exists()
    assert core.undo_manifest(result.manifest).failures == 0
    assert video.exists()


@pytest.mark.parametrize('mode', ['copy', 'move'])
def test_verified_transfer_and_undo(tmp_path, mode):
    source = tmp_path / 'Movies'
    video = film(source, name='Example.Movie.2020.mkv', payload=b'\x00\xffmedia' * 100)
    subtitle = video.parent / (video.stem + '.en.srt')
    subtitle.write_text('subtitle')
    output = tmp_path / 'Output'
    plan = core.build_plan(source, output, mode=mode)
    result = core.execute_plan(plan, mode=mode, journal_dir=tmp_path / 'journals')
    assert result.completed == 1 and result.failures == 0
    dst = output / 'Example Movie (2020)' / 'Example Movie (2020).mkv'
    assert dst.read_bytes() == b'\x00\xffmedia' * 100
    assert video.exists() == (mode == 'copy')
    if mode == 'copy':
        assert video.stat().st_ino != dst.stat().st_ino
    undo = core.undo_manifest(result.manifest)
    assert undo.failures == 0
    assert video.read_bytes() == b'\x00\xffmedia' * 100
    assert subtitle.read_text() == 'subtitle'
    assert not output.exists()
    assert json.loads(result.manifest.read_text())['undone']


def test_transfer_cancel_retains_source_and_journalled_partial(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source, payload=b'x' * (8 * 1024 * 1024 + 1))
    output = tmp_path / 'Output'
    event = threading.Event()
    def progress(number, total, phase):
        if phase.startswith('Copying'):
            event.set()
    result = core.execute_plan(core.build_plan(source, output, mode='move'), mode='move',
                               journal_dir=tmp_path / 'journals', cancel_event=event, progress=progress)
    assert result.cancelled and video.exists()
    assert any(item['status'] == 'partial' for item in json.loads(result.manifest.read_text())['records'])
    assert core.undo_manifest(result.manifest).failures == 0
    assert video.exists() and not output.exists()


def test_cancel_after_folder_rename_can_undo(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    video = film(source)
    event = threading.Event()
    original = core._journal_rename
    def rename(*args, **kwargs):
        original(*args, **kwargs)
        event.set()
    monkeypatch.setattr(core, '_journal_rename', rename)
    result = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals', cancel_event=event)
    assert result.cancelled and not video.exists()
    assert core.undo_manifest(result.manifest).failures == 0
    assert video.exists()


def test_pre_cancel_does_not_touch_media(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source)
    event = threading.Event()
    event.set()
    result = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals', cancel_event=event)
    assert result.cancelled and video.exists()


def test_already_correct_is_skipped(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source, 'Example Movie (2020)', 'Example Movie (2020).mp4')
    plan = core.build_plan(source)
    assert plan[0].status == 'Already correct'
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 0 and video.exists()


def test_numeric_year_validation_and_windows_names():
    assert core.movie_stem('The Movie: A / Story?', 2020) == 'The Movie- A - Story- (2020)'
    assert len(core.movie_stem('長' * 500, 2020).encode('utf-16-le')) // 2 <= 180
    for year in [None, True, 'wrong', 123, 3000]:
        with pytest.raises(ValueError):
            core.movie_stem('Title', year)


def test_tmdb_only_unique_exact_title_and_year_auto_matches(monkeypatch):
    client = core.TMDBClient('fake-test-key')
    first = core.MovieMatch('Example Movie', 2020, 1)
    monkeypatch.setattr(client, 'search', lambda title, year: [first])
    assert client.match(core.MovieGuess('Example Movie', 2020))[0] is first
    assert client.match(core.MovieGuess('Example Movie', None))[0] is None
    assert client.match(core.MovieGuess('Example', 2020))[0] is None
    monkeypatch.setattr(client, 'search', lambda title, year: [first, core.MovieMatch('Example Movie', 2020, 2)])
    assert client.match(core.MovieGuess('Example Movie', 2020))[0] is None


def test_last_manifest_ignores_fully_undone(tmp_path):
    source = tmp_path / 'Movies'
    film(source)
    directory = tmp_path / 'journals'
    result = core.execute_plan(core.build_plan(source), journal_dir=directory)
    assert core.latest_manifest(directory) == result.manifest
    core.undo_manifest(result.manifest)
    assert core.latest_manifest(directory) is None


def test_crash_after_directory_rename_recovers_planned_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    video = film(source)
    plan = core.build_plan(source)
    original = core._rename_noreplace
    def crash(src, dst):
        original(src, dst)
        raise KeyboardInterrupt('simulated process exit after directory entry changed')
    monkeypatch.setattr(core, '_rename_noreplace', crash)
    directory = tmp_path / 'journals'
    with pytest.raises(KeyboardInterrupt):
        core.execute_plan(plan, journal_dir=directory)
    monkeypatch.setattr(core, '_rename_noreplace', original)
    journal = next(directory.glob('movie-*.json'))
    assert core.latest_manifest(directory) == journal
    assert core.undo_manifest(journal).failures == 0
    assert video.read_bytes() == b'movie-bytes'


def test_crash_after_move_source_unlink_restores_verified_destination(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    video = film(source)
    output = tmp_path / 'Output'
    plan = core.build_plan(source, output, mode='move')
    original = Path.unlink
    def crash(path, *args, **kwargs):
        original(path, *args, **kwargs)
        if path == video:
            raise KeyboardInterrupt('simulated process exit after source removal')
    monkeypatch.setattr(Path, 'unlink', crash)
    directory = tmp_path / 'journals'
    with pytest.raises(KeyboardInterrupt):
        core.execute_plan(plan, mode='move', journal_dir=directory)
    monkeypatch.setattr(Path, 'unlink', original)
    journal = next(directory.glob('movie-*.json'))
    assert not video.exists()
    assert core.undo_manifest(journal).failures == 0
    assert video.read_bytes() == b'movie-bytes'
    assert not output.exists()


def test_undo_of_cancelled_restore_can_retry(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    video = film(source, payload=b'x' * (8 * 1024 * 1024 + 1))
    output = tmp_path / 'Output'
    run = core.execute_plan(core.build_plan(source, output, mode='move'), mode='move', journal_dir=tmp_path / 'journals')
    event = threading.Event()
    original = core.check_cancel
    calls = [0]
    def cancel_after_one_block(token):
        if token is event:
            calls[0] += 1
            if calls[0] == 6:
                event.set()
        original(token)
    monkeypatch.setattr(core, 'check_cancel', cancel_after_one_block)
    first = core.undo_manifest(run.manifest, cancel_event=event)
    assert first.cancelled
    monkeypatch.setattr(core, 'check_cancel', original)
    assert core.undo_manifest(run.manifest).failures == 0
    assert video.read_bytes() == b'x' * (8 * 1024 * 1024 + 1)
    assert not output.exists()


def test_unowned_planned_destination_is_not_removed_or_marked_undone(tmp_path):
    directory = tmp_path / 'unexpected'
    directory.mkdir()
    journal_path = tmp_path / 'movie-manual.json'
    core.atomic_json(journal_path, {'version': 1, 'undone': False,
                                  'records': [{'action': 'mkdir', 'dst': str(directory), 'status': 'planned'}]})
    result = core.undo_manifest(journal_path)
    assert result.failures == 1 and directory.exists()
    assert not json.loads(journal_path.read_text())['undone']


def test_case_insensitive_duplicate_destinations_are_blocked(tmp_path):
    source = tmp_path / 'Movies'
    film(source, 'One', 'EXAMPLE (2020).mp4')
    film(source, 'Two', 'Example (2020).mp4')
    plan = core.build_plan(source, tmp_path / 'Output', mode='copy')
    assert all(group.status == 'Conflict' for group in plan)


def test_nested_subtitle_and_named_poster_follow_movie(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source)
    subtitles = video.parent / 'Subs'
    subtitles.mkdir()
    (subtitles / 'en.srt').write_text('nested subtitle')
    poster = video.parent / (video.stem + '-poster.jpg')
    poster.write_bytes(b'poster')
    result = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals')
    assert result.failures == 0
    destination = source / 'Example Movie (2020)'
    assert (destination / 'Example Movie (2020).en.srt').read_text() == 'nested subtitle'
    assert (destination / 'Example Movie (2020)-poster.jpg').read_bytes() == b'poster'
    assert core.undo_manifest(result.manifest).failures == 0
    assert (subtitles / 'en.srt').read_text() == 'nested subtitle'
    assert poster.read_bytes() == b'poster'


def test_rarbg_numbered_english_subtitle_on_already_named_selected_movie(tmp_path):
    movie_folder = tmp_path / 'Cruel Instruction (2022)'
    video = film(movie_folder, '', 'Cruel Instruction (2022).mp4')
    subs = movie_folder / 'Subs'
    subs.mkdir()
    subtitle = subs / '2_English.srt'
    subtitle.write_text('example subtitle, not a real user file')
    plan = core.build_plan(movie_folder)
    assert plan[0].destination == movie_folder
    assert plan[0].folder_source == movie_folder
    associated = core.discover_subtitles([video])
    assert associated[str(video)] == [subtitle]
    assert any(change.src == subtitle and change.dst == movie_folder / 'Cruel Instruction (2022).en.srt' for change in plan[0].files)
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 1 and result.failures == 0
    assert video.exists() and (movie_folder / 'Cruel Instruction (2022).en.srt').exists()
    assert not (movie_folder / 'Cruel Instruction (2022)').exists()
    assert core.undo_manifest(result.manifest).failures == 0
    assert subtitle.exists()


def test_recursive_subs_preserves_duplicate_languages_flags_unknown_and_pairs(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source)
    subs = video.parent / 'Subtitles' / 'Nested'
    subs.mkdir(parents=True)
    for name in ['2_English.srt', '3_English.srt', 'English [SDH].srt', 'Track UNKNOWN.srt', '4_French.idx', '4_French.sub']:
        (subs / name).write_text(name)
    plan = core.build_plan(source)
    changes = [change for change in plan[0].files if change.kind == 'subtitle']
    assert len(changes) == 6
    assert len({str(change.dst).casefold() for change in changes}) == 6
    assert {change.dst.suffix for change in changes} >= {'.srt', '.idx', '.sub'}
    assert any(change.dst.name.endswith('.en.srt') for change in changes)
    assert any('.track-3.en.srt' in change.dst.name for change in changes)
    assert any(change.dst.name.endswith('.en.sdh.srt') for change in changes)
    assert any('.und.Track-UNKNOWN.srt' in change.dst.name for change in changes)
    assert any('language is unknown' in warning for warning in plan[0].warnings)
    assert next(change.dst.stem for change in changes if change.src.suffix == '.idx') == next(change.dst.stem for change in changes if change.src.suffix == '.sub')
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 1 and result.failures == 0
    assert core.undo_manifest(result.manifest).failures == 0
    assert all(change.src.exists() for change in changes)


def test_shared_subtitle_directory_requires_specific_movie_prefix(tmp_path):
    source = tmp_path / 'Movies'
    first = film(source, '', 'First (2020).mp4')
    second = film(source, '', 'Second (2021).mp4')
    subs = source / 'Subs' / 'Nested'
    subs.mkdir(parents=True)
    unknown = subs / '2_English.srt'
    unknown.write_text('ambiguous')
    known = subs / 'First (2020).en.srt'
    known.write_text('first only')
    associations = core.discover_subtitles([first, second])
    assert associations[str(first)] == [known]
    assert associations[str(second)] == []
    assert core.discover_subtitles([second])[str(second)] == []  # Unselected sibling still makes generic subtitles ambiguous.
    plan = core.build_plan(source)
    assert all(any('unambiguously' in warning for warning in group.warnings) for group in plan)
    result = core.execute_plan(plan, journal_dir=tmp_path / 'journals')
    assert result.completed == 2 and unknown.exists()
    assert (source / 'First (2020)' / 'First (2020).en.srt').exists()


def test_subtitle_language_is_not_guessed_from_movie_title(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source, 'English Movie (2020)', 'English Movie (2020).mp4')
    unknown = video.parent / '2_Unlabelled.srt'
    unknown.write_text('unknown language')
    plan = core.build_plan(source)
    change = next(change for change in plan[0].files if change.kind == 'subtitle')
    assert '.und.' in change.dst.name


@pytest.mark.parametrize('verification', [*core.VERIFICATION_DESCRIPTIONS, 'quick-sampled', 'quick-identity'])
@pytest.mark.parametrize('mode', ['copy', 'move'])
def test_every_transfer_policy_is_recorded_and_undoable(tmp_path, verification, mode):
    source = tmp_path / 'Movies'
    video = film(source, payload=b'test media\x00\xff' * 100)
    output = tmp_path / 'Output'
    result = core.execute_plan(core.build_plan(source, output, mode=mode), mode=mode,
                               journal_dir=tmp_path / 'journals', verification=verification)
    assert result.completed == 1 and result.failures == 0
    record = next(record for record in json.loads(result.manifest.read_text())['records'] if record['action'] == mode)
    assert record['verification'] == verification
    expected = 'Size and file identity' if verification == 'quick-identity' or verification.startswith('quick-') and mode == 'move' else 'Sampled fingerprint' if verification == 'quick-sampled' else verification
    assert record['method'] == expected
    assert core.undo_manifest(result.manifest).failures == 0
    assert video.read_bytes() == b'test media\x00\xff' * 100 and not output.exists()


@pytest.mark.parametrize('verification', ['Size and file identity', 'Skip verification', 'quick-identity'])
def test_no_content_verification_never_calls_hash_helper(tmp_path, monkeypatch, verification):
    source = tmp_path / 'Movies'
    video = film(source)
    monkeypatch.setattr(core, '_hash_file', lambda *args, **kwargs: pytest.fail('No-content policy must not hash'))
    run = core.execute_plan(core.build_plan(source, tmp_path / 'Output', mode='move'), mode='move',
                            journal_dir=tmp_path / 'journals', verification=verification)
    assert run.completed == 1 and run.failures == 0
    assert core.undo_manifest(run.manifest).failures == 0 and video.exists()


def test_quick_same_volume_move_changes_entries_without_opening_video(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    video = film(source)
    inode = video.stat().st_ino
    original_open = Path.open
    def guarded_open(path, *args, **kwargs):
        if path.suffix == '.mp4':
            pytest.fail('Quick same-volume move must not read or copy the video')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded_open)
    run = core.execute_plan(core.build_plan(source, tmp_path / 'Output', mode='move'), mode='move',
                            journal_dir=tmp_path / 'journals', verification='quick-sampled')
    assert run.completed == 1 and run.paths[0].stat().st_ino == inode
    assert core.undo_manifest(run.manifest).failures == 0 and video.stat().st_ino == inode


def test_missing_selected_hash_fails_before_mutation_but_rename_needs_no_hasher(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    video = film(source)
    copy_plan = core.build_plan(source, tmp_path / 'Output', mode='copy')
    monkeypatch.setattr(core, 'new_hasher', lambda method: (_ for _ in ()).throw(ValueError('missing package')))
    with pytest.raises(ValueError, match='missing package'):
        core.execute_plan(copy_plan, mode='copy', journal_dir=tmp_path / 'journals', verification='XXH3-128')
    assert video.exists() and not (tmp_path / 'Output').exists() and not (tmp_path / 'journals').exists()
    run = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals', verification='XXH3-128')
    assert run.completed == 1 and core.undo_manifest(run.manifest).failures == 0


@pytest.mark.parametrize('verification', ['Sampled fingerprint', 'quick-sampled'])
def test_sampled_partial_copy_can_be_undone_without_full_fingerprint(tmp_path, verification):
    source = tmp_path / 'Movies'
    video = film(source, payload=b'x' * (8 * 1024 * 1024 + 1))
    event = threading.Event()
    def progress(done, total, phase):
        if phase.startswith('Copying'):
            event.set()
    run = core.execute_plan(core.build_plan(source, tmp_path / 'Output', mode='copy'), mode='copy', journal_dir=tmp_path / 'journals',
                            verification=verification, progress=progress, cancel_event=event)
    assert run.cancelled and video.exists()
    assert core.undo_manifest(run.manifest).failures == 0 and not (tmp_path / 'Output').exists()


def media_snapshot(path):
    value = core._snapshot(path)
    return {'device': value['dev'], 'inode': value['ino'], 'size': value['size'], 'mtime_ns': value['mtime_ns']}


def test_metadata_rebase_links_semantic_restore_to_rename_without_hashing(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    video = film(source)
    run = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals')
    destination = run.paths[0]
    monkeypatch.setattr(core, '_hash_file', lambda *args, **kwargs: pytest.fail('Rename rebase must not hash video'))
    before = media_snapshot(destination)
    destination.write_bytes(b'movie-bytes plus descriptive tags')
    tagged = media_snapshot(destination)
    assert core.rebase_after_metadata(run.manifest, destination, before, tagged) >= 2
    destination.write_bytes(b'movie-bytes\x00padding retained after semantic tag restore')
    restored = media_snapshot(destination)
    assert core.rebase_after_metadata(run.manifest, destination, tagged, restored) >= 2
    assert core.undo_manifest(run.manifest).failures == 0
    assert video.read_bytes().startswith(b'movie-bytes')


@pytest.mark.parametrize('verification', ['BLAKE3', 'SHA-256', 'XXH3-128', 'Sampled fingerprint', 'Size and file identity', 'Skip verification'])
def test_metadata_rebase_retains_selected_transfer_policy(tmp_path, verification):
    source = tmp_path / 'Movies'
    video = film(source)
    run = core.execute_plan(core.build_plan(source, tmp_path / 'Output', mode='copy'), mode='copy',
                            journal_dir=tmp_path / 'journals', verification=verification)
    destination = run.paths[0]
    before = media_snapshot(destination)
    destination.write_bytes(b'movie-bytes plus tags')
    assert core.rebase_after_metadata(run.manifest, destination, before, media_snapshot(destination)) >= 2
    assert core.undo_manifest(run.manifest).failures == 0
    assert video.read_bytes() == b'movie-bytes'


def test_metadata_rebase_rejects_unknown_file_bad_before_and_later_changes(tmp_path):
    source = tmp_path / 'Movies'
    film(source)
    run = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals')
    destination = run.paths[0]
    before = media_snapshot(destination)
    unrelated = tmp_path / 'unrelated.mp4'
    unrelated.write_bytes(b'other data')
    with pytest.raises(ValueError, match='not owned'):
        core.rebase_after_metadata(run.manifest, unrelated, before, media_snapshot(unrelated))
    bad_before = {**before, 'size': before['size'] + 1}
    destination.write_bytes(b'new tags')
    after = media_snapshot(destination)
    with pytest.raises(ValueError, match='not owned'):
        core.rebase_after_metadata(run.manifest, destination, bad_before, after)
    destination.write_bytes(b'changed again after tag operation')
    with pytest.raises(OSError, match='changed'):
        core.rebase_after_metadata(run.manifest, destination, before, after)


def test_already_correct_movie_has_explicit_ownership_proof_for_metadata(tmp_path):
    source = tmp_path / 'Movies'
    video = film(source, 'Example Movie (2020)', 'Example Movie (2020).mp4')
    run = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals')
    assert run.completed == 0 and run.paths == [video]
    before = media_snapshot(video)
    video.write_bytes(b'tagged movie-bytes')
    assert core.rebase_after_metadata(run.manifest, video, before, media_snapshot(video)) == 1
    assert core.undo_manifest(run.manifest).failures == 0
    assert video.exists()


def test_provider_identity_never_places_tvdb_id_in_tmdb_field(tmp_path):
    source = tmp_path / 'Movies'
    film(source)
    match = core.MovieMatch('Example Movie', 2020, provider='TVDB', provider_id='tvdb-123')
    plan = core.build_plan(source, matcher=lambda guess: match)
    assert plan[0].provider == 'TVDB' and plan[0].provider_id == 'tvdb-123' and plan[0].tmdb_id is None
    core.revise_group(plan[0], 'Example Movie', 2020, provider='TVDB', provider_id='tvdb-456')
    assert plan[0].provider_id == 'tvdb-456' and plan[0].tmdb_id is None


@pytest.mark.parametrize('verification,effective', [('quick-sampled', 'Sampled fingerprint'), ('quick-identity', 'Size and file identity')])
def test_quick_move_cross_volume_fallback_retains_policy(tmp_path, monkeypatch, verification, effective):
    import errno
    source = tmp_path / 'Movies'
    video = film(source)
    monkeypatch.setattr(core.os, 'link', lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.EXDEV, 'different volume')))
    run = core.execute_plan(core.build_plan(source, tmp_path / 'Output', mode='move'), mode='move',
                            journal_dir=tmp_path / 'journals', verification=verification)
    assert run.completed == 1 and run.failures == 0
    record = next(record for record in json.loads(run.manifest.read_text())['records'] if record['action'] == 'move')
    assert record['method'] == effective and not record.get('linked')
    assert core.undo_manifest(run.manifest).failures == 0 and video.exists()


def test_replaced_destination_is_never_adopted_by_error_checkpoint_or_undo(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    video = film(source)
    output = tmp_path / 'Output'
    plan = core.build_plan(source, output, mode='move')
    destination = plan[0].files[0].dst
    original = core._fingerprint_matches
    replaced = [False]
    def replace_before_verification(path, record, cancel_event=None):
        if path == destination and not replaced[0]:
            replaced[0] = True
            path.unlink()
            path.write_bytes(b'belongs to another application')
        return original(path, record, cancel_event)
    monkeypatch.setattr(core, '_fingerprint_matches', replace_before_verification)
    run = core.execute_plan(plan, mode='move', journal_dir=tmp_path / 'journals')
    assert run.failures == 1 and video.exists()
    record = next(record for record in json.loads(run.manifest.read_text())['records'] if record['action'] == 'move')
    assert record.get('destination_replaced')
    assert core.undo_manifest(run.manifest).failures >= 1
    assert destination.read_bytes() == b'belongs to another application'


@pytest.mark.parametrize('verification', [*core.VERIFICATION_DESCRIPTIONS, 'quick-sampled', 'quick-identity'])
def test_modified_destination_remains_protected_for_every_policy(tmp_path, verification):
    source = tmp_path / 'Movies'
    film(source)
    run = core.execute_plan(core.build_plan(source, tmp_path / 'Output', mode='copy'), mode='copy',
                            journal_dir=tmp_path / 'journals', verification=verification)
    destination = run.paths[0]
    destination.write_bytes(b'changed by someone else after the run')
    assert core.undo_manifest(run.manifest).failures >= 1
    assert destination.read_bytes() == b'changed by someone else after the run'


def test_already_correct_cancellation_does_not_authorise_metadata_paths(tmp_path):
    source = tmp_path / 'Movies'
    film(source, 'Example Movie (2020)', 'Example Movie (2020).mp4')
    event = threading.Event()
    event.set()
    run = core.execute_plan(core.build_plan(source), journal_dir=tmp_path / 'journals', cancel_event=event)
    assert run.cancelled and run.paths == []


def test_metadata_rebase_only_repeats_a_proved_transition_without_hashing(tmp_path, monkeypatch):
    source = tmp_path / 'Movies'
    film(source)
    run = core.execute_plan(core.build_plan(source, tmp_path / 'Output', mode='copy'), mode='copy', journal_dir=tmp_path / 'journals')
    destination = run.paths[0]
    before = media_snapshot(destination)
    destination.write_bytes(b'approved tags changed the container')
    after = media_snapshot(destination)
    assert core.rebase_after_metadata(run.manifest, destination, before, after) > 0
    journal_before = run.manifest.read_bytes()
    monkeypatch.setattr(core, '_hash_file', lambda *args, **kwargs: pytest.fail('An idempotent rebase must not read content'))
    assert core.rebase_after_metadata(run.manifest, destination, before, after) == 0
    assert run.manifest.read_bytes() == journal_before
    with pytest.raises(ValueError, match='not owned'):
        core.rebase_after_metadata(run.manifest, destination, {**before, 'size': before['size'] + 2}, after)
