"""End-to-end movie workflows using one generated MP4, never user media."""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

import media_tools as media
import mmo_core as core
import workflow


@pytest.fixture(scope='module')
def synthetic_mp4(tmp_path_factory):
    executable = shutil.which('ffmpeg')
    if not executable:
        imageio = pytest.importorskip('imageio_ffmpeg', reason='The dev dependency generates a disposable MP4 fixture')
        executable = imageio.get_ffmpeg_exe()
    path = tmp_path_factory.mktemp('workflow-fixture') / 'Synthetic.Movie.2026.mp4'
    subprocess.run([executable, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=320x180:r=24:d=0.3', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=48000:duration=0.3', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-metadata', 'title=Old.Release.Group.Title',
                    '-metadata', 'date=1997', '-metadata', 'comment=Source release comment',
                    '-metadata', 'genre=Original genre', '-metadata', 'artist=Original artist',
                    '-shortest', str(path)], check=True)
    return path


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    monkeypatch.setattr(workflow, 'state_root', lambda: state)
    monkeypatch.setattr(core, 'state_root', lambda: state)
    yield state


def prepare_movie(tmp_path, synthetic_mp4, canonical=False):
    source = tmp_path / 'Movies'
    parent = source / ('Synthetic Movie (2026)' if canonical else 'Release Folder')
    parent.mkdir(parents=True)
    video = parent / ('Synthetic Movie (2026).mp4' if canonical else 'Synthetic.Movie.2026.1080p.WEBRip.mp4')
    shutil.copy2(synthetic_mp4, video)
    return source, video


def payload(path):
    content = path.read_bytes()
    offset, blocks = 0, []
    while offset + 8 <= len(content):
        length, kind = struct.unpack_from('>I4s', content, offset)
        header = 8
        if length == 1:
            length = struct.unpack_from('>Q', content, offset + 8)[0]
            header = 16
        elif length == 0:
            length = len(content) - offset
        assert length >= header
        if kind == b'mdat':
            blocks.append(content[offset + header:offset + length])
        offset += length
    assert blocks
    return blocks


@pytest.mark.parametrize('mode', ['rename', 'copy', 'move'])
def test_dry_run_never_writes_files_nfo_tags_or_journals(tmp_path, synthetic_mp4, mode):
    source, video = prepare_movie(tmp_path, synthetic_mp4)
    output = tmp_path / 'Library'
    plan = core.build_plan(source, output if mode != 'rename' else None, mode=mode)
    original = video.read_bytes()
    messages = []
    result = workflow.run(plan, mode=mode, write_nfo=True, embedded_tags=True, replace_comments=True,
                          dry_run=True, journal_dir=tmp_path / 'journals', log=messages.append)
    assert result.dry_run and result.manifest is None
    assert video.read_bytes() == original
    assert not output.exists() and not (tmp_path / 'journals').exists() and not (tmp_path / 'state').exists()
    assert not list(source.rglob('*.nfo'))
    assert any('DRY RUN TAGS' in message for message in messages)
    assert any('DRY RUN NFO' in message for message in messages)


@pytest.mark.parametrize('mode', ['rename', 'copy', 'move'])
def test_real_mp4_tags_comment_nfo_and_undo_in_every_file_mode(tmp_path, synthetic_mp4, mode):
    source, video = prepare_movie(tmp_path, synthetic_mp4)
    original_bytes = video.read_bytes()
    original_tags = dict(MP4(video).tags)
    original_payload = payload(video)
    output = tmp_path / 'Library'
    plan = core.build_plan(source, output if mode != 'rename' else None, mode=mode)
    plan[0].overview = 'Plot with <markup> & punctuation.'
    plan[0].genres = ['Drama', 'Science Fiction']
    plan[0].provider_id = 12345
    result = workflow.run(plan, mode=mode, write_nfo=True, embedded_tags=True, replace_comments=True,
                          dry_run=False, journal_dir=tmp_path / 'journals')
    assert result.failures == 0 and result.completed == 1
    assert result.tags_updated == 1 and result.nfo_created == 1
    destination = (source if mode == 'rename' else output) / 'Synthetic Movie (2026)' / 'Synthetic Movie (2026).mp4'
    assert result.paths == [destination]
    changed = dict(MP4(destination).tags)
    assert changed['\xa9nam'] == ['Synthetic Movie']
    assert changed['\xa9day'] == ['2026']
    assert changed['\xa9cmt'] == ['Synthetic Movie (2026)']
    assert changed['\xa9gen'] == original_tags['\xa9gen']
    assert changed['\xa9ART'] == original_tags['\xa9ART']
    assert payload(destination) == original_payload
    import xml.etree.ElementTree as ET
    nfo = ET.parse(destination.with_suffix('.nfo')).getroot()
    assert nfo.findtext('title') == 'Synthetic Movie'
    assert nfo.findtext('plot') == plan[0].overview
    assert nfo.find('uniqueid').attrib['type'] == 'tmdb'
    assert nfo.find('uniqueid').text == '12345'
    if mode == 'copy':
        assert video.read_bytes() == original_bytes
        assert dict(MP4(video).tags) == original_tags
    else:
        assert not video.exists()
    undo = workflow.undo_run(result.manifest)
    assert undo.failures == 0
    assert video.exists() and dict(MP4(video).tags) == original_tags
    assert payload(video) == original_payload
    assert not destination.with_suffix('.nfo').exists()
    if mode == 'copy':
        assert video.read_bytes() == original_bytes
    else:
        assert video.name == 'Synthetic.Movie.2026.1080p.WEBRip.mp4'
    if mode != 'rename':
        assert not output.exists()
    assert json.loads(result.manifest.read_text())['undone']


def test_already_correct_movie_can_update_tags_and_nfo_without_moving_it(tmp_path, synthetic_mp4):
    source, video = prepare_movie(tmp_path, synthetic_mp4, canonical=True)
    inode = video.stat().st_ino
    old_tags = dict(MP4(video).tags)
    original_payload = payload(video)
    plan = core.build_plan(source)
    assert plan[0].status == 'Already correct'
    result = workflow.run(plan, write_nfo=True, embedded_tags=True, replace_comments=True,
                          dry_run=False, journal_dir=tmp_path / 'journals')
    assert result.failures == 0 and result.completed == 0
    assert result.tags_updated == 1 and result.nfo_created == 1
    assert video.stat().st_ino == inode and result.paths == [video]
    assert payload(video) == original_payload
    assert dict(MP4(video).tags)['\xa9cmt'] == ['Synthetic Movie (2026)']
    assert workflow.undo_run(result.manifest).failures == 0
    assert dict(MP4(video).tags) == old_tags
    assert payload(video) == original_payload and video.stat().st_ino == inode
    assert not video.with_suffix('.nfo').exists()


def test_foreign_nfo_is_never_overwritten_or_deleted(tmp_path, synthetic_mp4):
    source, video = prepare_movie(tmp_path, synthetic_mp4, canonical=True)
    nfo = video.with_suffix('.nfo')
    nfo.write_text('<movie><title>My own metadata</title></movie>', encoding='utf-8')
    original = nfo.read_bytes()
    result = workflow.run(core.build_plan(source), write_nfo=True, embedded_tags=True,
                          dry_run=False, journal_dir=tmp_path / 'journals')
    assert result.failures == 0 and result.tags_updated == 1 and result.nfo_created == 0
    assert nfo.read_bytes() == original
    assert workflow.undo_run(result.manifest).failures == 0
    assert nfo.read_bytes() == original


def test_changed_generated_nfo_prevents_file_undo(tmp_path, synthetic_mp4):
    source, video = prepare_movie(tmp_path, synthetic_mp4)
    result = workflow.run(core.build_plan(source), write_nfo=True, embedded_tags=True,
                          dry_run=False, journal_dir=tmp_path / 'journals')
    assert result.failures == 0
    destination = result.paths[0]
    nfo = destination.with_suffix('.nfo')
    nfo.write_text('My edits after the run', encoding='utf-8')
    with pytest.raises(OSError, match='NFO changed'):
        workflow.undo_run(result.manifest)
    assert nfo.read_text(encoding='utf-8') == 'My edits after the run'
    assert destination.exists() and not video.exists()
    assert not json.loads(result.manifest.read_text())['undone']


def test_changed_tagged_video_prevents_file_undo(tmp_path, synthetic_mp4):
    source, video = prepare_movie(tmp_path, synthetic_mp4)
    result = workflow.run(core.build_plan(source), write_nfo=True, embedded_tags=True,
                          dry_run=False, journal_dir=tmp_path / 'journals')
    assert result.failures == 0
    destination = result.paths[0]
    with destination.open('ab') as stream:
        stream.write(b'changed externally after tag update')
    changed = destination.read_bytes()
    with pytest.raises((OSError, media.MediaToolError), match='changed'):
        workflow.undo_run(result.manifest)
    assert destination.read_bytes() == changed
    assert destination.with_suffix('.nfo').exists() and not video.exists()


def test_metadata_done_before_reverse_link_is_recovered_from_metadata_journal(tmp_path, synthetic_mp4, monkeypatch):
    source, video = prepare_movie(tmp_path, synthetic_mp4)
    old_tags = dict(MP4(video).tags)
    original_payload = payload(video)
    original = workflow._rebase_edit
    monkeypatch.setattr(workflow, '_rebase_edit', lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt('simulated exit before reverse link')))
    with pytest.raises(KeyboardInterrupt):
        workflow.run(core.build_plan(source), write_nfo=True, embedded_tags=True,
                     dry_run=False, journal_dir=tmp_path / 'journals')
    manifest = next((tmp_path / 'journals').glob('movie-*.json'))
    assert 'metadata_edits' not in json.loads(manifest.read_text())
    metadata_manifest = next((tmp_path / 'state' / 'metadata').glob('metadata-*.json'))
    metadata = json.loads(metadata_manifest.read_text())
    assert Path(metadata['file_manifest']) == manifest
    assert metadata['status'] == 'done'
    monkeypatch.setattr(workflow, '_rebase_edit', original)
    assert workflow.undo_run(manifest).failures == 0
    assert video.exists() and dict(MP4(video).tags) == old_tags
    assert payload(video) == original_payload


def test_semantic_tag_restore_crash_can_finish_file_undo_on_retry(tmp_path, synthetic_mp4, monkeypatch):
    source, video = prepare_movie(tmp_path, synthetic_mp4)
    old_tags = dict(MP4(video).tags)
    result = workflow.run(core.build_plan(source), embedded_tags=True, dry_run=False, journal_dir=tmp_path / 'journals')
    assert result.failures == 0
    original = workflow._rebase_edit
    calls = [0]
    def crash_after_restore(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            raise KeyboardInterrupt('simulated exit after tags restored but before file guard rebased')
        return original(*args, **kwargs)
    monkeypatch.setattr(workflow, '_rebase_edit', crash_after_restore)
    with pytest.raises(KeyboardInterrupt):
        workflow.undo_run(result.manifest)
    monkeypatch.setattr(workflow, '_rebase_edit', original)
    undo = workflow.undo_run(result.manifest)
    assert undo.failures == 0
    assert video.exists() and dict(MP4(video).tags) == old_tags


@pytest.fixture(scope='module')
def synthetic_mkv(tmp_path_factory, synthetic_mp4):
    tools = {name: media.find_tool(name) for name in ('mkvpropedit', 'mkvextract', 'ffprobe')}
    if not all(tools.values()):
        pytest.skip('Native MKV workflow tests require MKVPropEdit, MKVExtract and FFprobe; source-only CI may omit these tools')
    imageio = pytest.importorskip('imageio_ffmpeg', reason='The dev dependency generates a disposable MKV fixture')
    path = tmp_path_factory.mktemp('workflow-mkv-fixture') / 'Synthetic.Movie.2026.mkv'
    subprocess.run([imageio.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-i', str(synthetic_mp4),
                    '-map', '0', '-c', 'copy', '-metadata', 'title=Original segment title', str(path)], check=True)
    original = media._extract_mkv_tags(path, tools)
    tags = media._edited_mkv_tags(original, 'Original global title', 1999, 'Original MKV comment')
    media._write_mkv_tags_and_title(path, 'Original segment title', tags, tools)
    return path, tools


def mkv_values(path, tools):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(media._extract_mkv_tags(path, tools))
    global_tags = [tag for tag in root.findall('Tag') if tag.find('Targets/TrackUID') is None]
    return {item.findtext('Name'): item.findtext('String') for tag in global_tags for item in tag.findall('Simple')}


def packet_hashes(path, tools):
    response = subprocess.run([tools['ffprobe'], '-v', 'error', '-show_packets', '-show_data_hash', 'sha256',
                               '-show_entries', 'packet=stream_index,pts,dts,duration,size,data_hash',
                               '-of', 'json', str(path)], check=True, capture_output=True, text=True, encoding='utf-8')
    packets = json.loads(response.stdout)['packets']
    assert packets and all(packet.get('data_hash', '').startswith('SHA256:') for packet in packets)
    return packets


@pytest.mark.parametrize('mode', ['rename', 'copy', 'move'])
def test_native_mkv_tags_comment_nfo_and_undo_preserve_packet_payloads(tmp_path, synthetic_mkv, mode):
    fixture, tools = synthetic_mkv
    source = tmp_path / 'Movies'
    parent = source / 'Release Folder'
    parent.mkdir(parents=True)
    video = parent / 'Synthetic.Movie.2026.1080p.WEBRip.mkv'
    shutil.copy2(fixture, video)
    original_bytes = video.read_bytes()
    original_values = mkv_values(video, tools)
    original_title = media._read_mkv_segment_title(video)
    original_packets = packet_hashes(video, tools)
    output = tmp_path / 'Library'
    plan = core.build_plan(source, output if mode != 'rename' else None, mode=mode)
    result = workflow.run(plan, mode=mode, write_nfo=True, embedded_tags=True, replace_comments=True,
                          dry_run=False, journal_dir=tmp_path / 'journals', tool_paths=tools)
    assert result.failures == 0 and result.completed == 1
    assert result.tags_updated == 1 and result.nfo_created == 1
    destination = (source if mode == 'rename' else output) / 'Synthetic Movie (2026)' / 'Synthetic Movie (2026).mkv'
    assert result.paths == [destination]
    assert media._read_mkv_segment_title(destination) == 'Synthetic Movie'
    values = mkv_values(destination, tools)
    assert values['TITLE'] == 'Synthetic Movie'
    assert values['DATE_RELEASED'] == '2026'
    assert values['COMMENT'] == 'Synthetic Movie (2026)'
    assert destination.with_suffix('.nfo').exists()
    assert packet_hashes(destination, tools) == original_packets
    if mode == 'copy':
        assert video.read_bytes() == original_bytes
        assert mkv_values(video, tools) == original_values
    else:
        assert not video.exists()
    undo = workflow.undo_run(result.manifest, tool_paths=tools)
    assert undo.failures == 0 and video.exists()
    assert media._read_mkv_segment_title(video) == original_title
    restored = mkv_values(video, tools)
    assert restored['TITLE'] == original_values['TITLE']
    assert restored['DATE_RELEASED'] == original_values['DATE_RELEASED']
    assert restored['COMMENT'] == original_values['COMMENT']
    assert packet_hashes(video, tools) == original_packets
    assert not destination.with_suffix('.nfo').exists()
    if mode == 'copy':
        assert video.read_bytes() == original_bytes
    if mode != 'rename':
        assert not output.exists()
