from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import wave

import pytest

import media_tools as media
from support import Cancelled


@pytest.mark.parametrize("width,height,label", [
    (1920, 800, "1080p"), (1920, 1080, "1080p"), (3840, 1600, "4K"),
    (4096, 2160, "4K"), (7680, 3200, "8K"), (1280, 534, "720p"),
    (2560, 1440, "1440p"), (720, 576, "SD"), (None, None, "Unknown"),
    (2160, 3840, "4K"), (320, 180, "SD"),
])
def test_resolution_uses_real_dimensions_including_cropped_films(width, height, label):
    assert media.resolution_label(width, height) == label


@pytest.mark.parametrize("track,expected", [
    ({"hdr_format": "Dolby Vision / SMPTE ST 2086", "hdr_format_compatibility": "HDR10"}, {"Dolby Vision", "HDR10"}),
    ({"hdr_format": "SMPTE ST 2094-40", "hdr_format_commercial": "HDR10+"}, {"HDR10+"}),
    ({"transfer_characteristics": "HLG"}, {"HLG"}),
    ({"transfer_characteristics": "ARIB STD-B67"}, {"HLG"}),
    ({"transfer_characteristics": "PQ"}, {"HDR (PQ)"}),
    ({"transfer_characteristics": "BT.709"}, {"SDR"}),
    ({"bit_depth": 10, "colour_primaries": "BT.2020"}, {"Unknown"}),
    ({}, {"Unknown"}),
])
def test_hdr_requires_explicit_evidence(track, expected):
    assert set(media.hdr_labels(track)) == expected


def parsed_record(tmp_path):
    path = tmp_path / "Pretend.8K.HDR10+.mkv"
    path.write_bytes(b"test")
    return media.normalise_mediainfo({"tracks": [
        {"track_type": "General", "format": "Matroska", "duration": 120000,
         "format_profile": "Version 4", "commercial_name": "Matroska", "title": "Original Title"},
        {"track_type": "Video", "format": "HEVC", "codec_id": "V_MPEGH/ISO/HEVC", "width": 3840, "height": 1600,
         "bit_depth": 10, "color_space": "YUV", "chroma_subsampling": "4:2:0", "frame_rate": "23.976",
         "bit_rate": 42000000, "hdr_format": "Dolby Vision", "hdr_format_compatibility": "HDR10"},
        {"track_type": "Audio", "format": "AC-3", "channel_s": 6, "sampling_rate": 48000,
         "language": "en", "title": "Main English", "bit_rate": 640000},
        {"track_type": "Audio", "format": "AC-3", "channel_s": 2, "sampling_rate": 48000,
         "language": "en", "title": "Commentary", "bit_rate": 224000},
        {"track_type": "Audio", "format": "DTS", "channel_s": 6, "language": "fr"},
        {"track_type": "Text", "format": "PGS", "codec_id": "S_HDMV/PGS", "language": "en", "forced": "Yes"},
        {"track_type": "Menu", "00_00_00000": "Opening"},
    ]}, path)


def test_per_file_details_preserve_streams_and_raw_fields(tmp_path):
    record = parsed_record(tmp_path)
    video = record["video"][0]
    assert video["resolution"] == "4K"
    assert video["frame_rate"] == 23.976
    assert video["bitrate"] == 42000000
    assert video["color_space"] == "YUV"
    assert video["chroma_subsampling"] == "4:2:0"
    assert record["duration"] == 120
    assert record["audio"][0]["channels"] == 6
    assert record["audio"][0]["sample_rate"] == 48000
    assert record["text"][0]["codec_id"] == "S_HDMV/PGS"
    assert record["other_tracks"][0]["track_type"] == "Menu"
    assert len(record["raw"]["tracks"]) == 7


def test_summary_counts_distinct_files_and_all_streams_separately(tmp_path):
    record = parsed_record(tmp_path)
    summary = media.summarise([record, {"path": "bad", "size": 9, "error": "unreadable"}])
    assert summary["files_total"] == 2
    assert summary["files_ok"] == 1
    assert summary["files_failed"] == 1
    assert summary["audio_codecs"]["AC-3"] == 1
    assert summary["stream_counts"]["audio_codecs"]["AC-3"] == 2
    assert summary["audio_languages"]["en"] == 1
    assert summary["stream_counts"]["audio_languages"]["en"] == 2
    assert summary["hdr"] == {"Dolby Vision": 1, "HDR10": 1}
    assert summary["audio_streams"] == 3
    assert summary["total_duration"] == 120


def test_cache_reuses_unchanged_files_and_invalidates_after_write(tmp_path, monkeypatch):
    record = parsed_record(tmp_path)
    path = Path(record["path"])
    calls = []
    def probe(path, **kwargs):
        calls.append(path)
        return dict(record)
    monkeypatch.setattr(media, "probe_file", probe)
    cache = tmp_path / "cache.json"
    first = media.scan_library([path, path], cache_path=cache)
    second = media.scan_library([path], cache_path=cache)
    assert len(calls) == 1
    assert first.summary["files_total"] == 1
    assert second.records[0]["cached"] is True
    path.write_bytes(b"changed and larger")
    third = media.scan_library([path], cache_path=cache)
    assert len(calls) == 2
    assert third.records[0]["cached"] is False


def test_cancelled_scan_never_claims_success(tmp_path):
    path = tmp_path / "one.mkv"
    path.write_bytes(b"x")
    event = threading.Event()
    event.set()
    with pytest.raises(Cancelled):
        media.scan_library([path], cancel_event=event)


def test_scan_reports_failures_with_the_original_path(tmp_path, monkeypatch):
    path = tmp_path / "unreadable.mkv"
    path.write_bytes(b"bad")
    def fail(*args, **kwargs):
        raise media.MediaToolError("unreadable media")
    monkeypatch.setattr(media, "probe_file", fail)
    report = media.scan_library([path])
    assert report.summary["files_failed"] == 1
    assert report.summary["files_ok"] == 0
    assert report.errors == [{"path": str(path.resolve()), "error": "unreadable media"}]


def test_json_export_retains_every_track_and_csv_is_formula_safe(tmp_path):
    record = parsed_record(tmp_path)
    record["title"] = '=HYPERLINK("bad")'
    report = media.MediaReport(records=[record], summary=media.summarise([record]))
    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"
    media.export_report(report, json_path)
    media.export_report(report, csv_path)
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(saved["records"][0]["raw"]["tracks"]) == 7
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig", newline="")))
    assert rows[0]["title"].startswith("'=")
    assert rows[0]["hdr"] == "Dolby Vision + HDR10"
    assert rows[0]["dimensions"] == "3840x1600"


def test_native_mediainfo_reads_actual_wave_without_trusting_extension(tmp_path):
    pytest.importorskip("pymediainfo")
    path = tmp_path / "not-really-4K.mkv"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\0" * 48000 * 4)
    record = media.probe_file(path)
    assert record["container"] == "Wave"
    assert not record["video"]
    assert record["audio"][0]["sample_rate"] == 48000
    assert record["audio"][0]["channels"] == 2
    assert record["duration"] == pytest.approx(1)


@pytest.fixture(scope="module")
def real_mp4(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        imageio = pytest.importorskip("imageio_ffmpeg", reason="Install dev dependencies to generate the synthetic MP4 test fixture")
        ffmpeg = imageio.get_ffmpeg_exe()
    path = tmp_path_factory.mktemp("generated-video") / "Synthetic.Movie.2026.4K.HDR.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24:d=0.3",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-metadata", "title=Old.Release.Group.Title", "-metadata", "comment=keep this comment", "-shortest", str(path)], check=True)
    return path


def test_native_video_probe_ignores_misleading_filename(real_mp4):
    record = media.probe_file(real_mp4)
    assert record["video"][0]["width"] == 320
    assert record["video"][0]["height"] == 180
    assert record["video"][0]["resolution"] == "SD"
    assert "HDR10" not in record["video"][0]["hdr"]
    assert record["audio"][0]["codec"] == "AAC"
    assert record["title"] == "Old.Release.Group.Title"


def test_actual_mp4_title_and_year_update_undo_preserves_streams_and_other_tags(real_mp4, tmp_path):
    from mutagen.mp4 import MP4
    path = tmp_path / "movie.mp4"
    shutil.copy2(real_mp4, path)
    old_tags = dict(MP4(path).tags)
    def media_payload(file):
        content = file.read_bytes()
        offset, payloads = 0, []
        while offset + 8 <= len(content):
            length, kind = struct.unpack_from(">I4s", content, offset)
            header = 8
            if length == 1:
                length = struct.unpack_from(">Q", content, offset + 8)[0]
                header = 16
            elif length == 0:
                length = len(content) - offset
            if kind == b"mdat":
                payloads.append(content[offset + header:offset + length])
            assert length >= header
            offset += length
        assert payloads
        return payloads
    original_payload = media_payload(path)
    before = media.probe_file(path)
    result = media.update_tags(path, "Synthetic Movie (2026)", 2026, journal_dir=tmp_path / "journals")
    assert result["status"] == "done"
    changed = MP4(path)
    assert changed.tags["\xa9nam"] == ["Synthetic Movie (2026)"]
    assert changed.tags["\xa9day"] == ["2026"]
    assert changed.tags["\xa9cmt"] == old_tags["\xa9cmt"]
    after = media.probe_file(path)
    assert after["video"][0]["width"] == before["video"][0]["width"]
    assert after["video"][0]["codec"] == before["video"][0]["codec"]
    assert after["audio"][0]["codec"] == before["audio"][0]["codec"]
    assert after["duration"] == before["duration"]
    assert media_payload(path) == original_payload
    restored = media.undo_tags(result["manifest"])
    assert restored["status"] == "undone"
    assert dict(MP4(path).tags) == old_tags
    assert media_payload(path) == original_payload
    assert media.undo_tags(result["manifest"])["status"] == "already-undone"


def test_metadata_undo_refuses_files_changed_after_update(real_mp4, tmp_path):
    path = tmp_path / "movie.mp4"
    shutil.copy2(real_mp4, path)
    result = media.update_tags(path, "New Title", journal_dir=tmp_path / "journals")
    with path.open("ab") as output:
        output.write(b"external edit")
    data = path.read_bytes()
    with pytest.raises(media.MediaToolError, match="changed"):
        media.undo_tags(result["manifest"])
    assert path.read_bytes() == data


def test_metadata_changes_require_a_journal_and_supported_container(tmp_path):
    path = tmp_path / "movie.avi"
    path.write_bytes(b"not a real file")
    with pytest.raises(ValueError, match="journal"):
        media.update_tags(path, "Title")
    with pytest.raises(media.MediaToolError, match="Unsupported"):
        media.update_tags(path, "Title", journal_dir=tmp_path / "journals")
    assert not (tmp_path / "journals").exists()


def test_mkv_capability_is_explicit_when_external_tool_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "find_tool", lambda *args, **kwargs: None)
    assert media.metadata_capability(tmp_path / "movie.mkv").startswith("Unavailable")


def test_ffprobe_fallback_preserves_dolby_vision_and_audio_details(tmp_path, monkeypatch):
    import pymediainfo
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"synthetic bytes for mocked tool response")
    def native_failure(*args, **kwargs):
        raise OSError("native parser unavailable")
    monkeypatch.setattr(pymediainfo.MediaInfo, "parse", native_failure)
    monkeypatch.setattr(media, "find_tool", lambda name, tools=None: "ffprobe.exe" if name == "ffprobe" else None)
    source = {"format": {"format_long_name": "Matroska / WebM", "duration": "2.0", "tags": {"title": "Title"}},
              "streams": [{"codec_type": "video", "codec_name": "hevc", "codec_tag_string": "hvc1", "profile": "Main 10",
                           "width": 3840, "height": 1600, "avg_frame_rate": "24000/1001", "color_transfer": "smpte2084",
                           "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 7}]},
                          {"codec_type": "audio", "codec_name": "truehd", "sample_rate": "48000", "channels": 8,
                           "channel_layout": "7.1", "tags": {"language": "eng"}},
                          {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}}]}
    monkeypatch.setattr(media, "_run", lambda *args, **kwargs: (0, json.dumps(source), ""))
    result = media.probe_file(path)
    assert result["engine"] == "ffprobe"
    assert result["duration"] == 2
    assert result["video"][0]["resolution"] == "4K"
    assert result["video"][0]["hdr"] == ["Dolby Vision"]
    assert result["audio"][0]["channels"] == 8
    assert result["audio"][0]["sample_rate"] == 48000
    assert result["text"][0]["language"] == "eng"
    assert "native parser unavailable" in result["parser_warning"]


def test_mediainfo_cli_maps_acronym_and_channel_fields(tmp_path, monkeypatch):
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"synthetic tool-response fixture")
    source = {"media": {"track": [
        {"@type": "General", "Format": "Matroska", "Duration": "2.5", "Title": "Film"},
        {"@type": "Video", "Format": "HEVC", "CodecID": "V_MPEGH/ISO/HEVC", "Width": "3840", "Height": "1600",
         "HDR_Format": "Dolby Vision", "HDR_Format_Compatibility": "HDR10", "BitDepth": "10"},
        {"@type": "Audio", "Format": "AAC", "CodecID": "A_AAC", "SamplingRate": "48000", "Channels": "6"},
    ]}}
    monkeypatch.setattr(media, "find_tool", lambda *args: "mediainfo.exe")
    monkeypatch.setattr(media, "_run", lambda *args, **kwargs: (0, json.dumps(source), ""))
    result = media.probe_file(path, {"mediainfo": "mediainfo.exe"})
    assert result["engine"] == "MediaInfo CLI"
    assert result["duration"] == 2.5
    assert result["video"][0]["codec_id"] == "V_MPEGH/ISO/HEVC"
    assert result["video"][0]["bit_depth"] == 10
    assert set(result["video"][0]["hdr"]) == {"Dolby Vision", "HDR10"}
    assert result["audio"][0]["channels"] == 6


def test_failed_metadata_edit_leaves_a_recovery_record(real_mp4, tmp_path, monkeypatch):
    path = tmp_path / "movie.mp4"
    shutil.copy2(real_mp4, path)
    original = media._read_mp4_tags
    def broken_read(path):
        movie, tags = original(path)
        def fail_save():
            raise OSError("simulated storage failure")
        movie.save = fail_save
        return movie, tags
    monkeypatch.setattr(media, "_read_mp4_tags", broken_read)
    with pytest.raises(media.MediaToolError, match="recovery record"):
        media.update_tags(path, "Title", journal_dir=tmp_path / "journals")
    records = list((tmp_path / "journals").glob("metadata-*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["status"] == "failed"
    assert media.probe_file(path)["title"] == "Old.Release.Group.Title"
