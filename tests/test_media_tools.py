from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import wave
import xml.etree.ElementTree as ET

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


def test_native_ffprobe_fallback_reads_actual_streams_and_ignores_filename_hdr(real_mp4, monkeypatch):
    import pymediainfo
    executable = media.find_tool("ffprobe")
    if not executable:
        pytest.skip("An available FFprobe is required for the native fallback check")
    def unavailable(*args, **kwargs):
        raise OSError("synthetic primary-parser unavailability")
    monkeypatch.setattr(pymediainfo.MediaInfo, "parse", unavailable)
    record = media.probe_file(real_mp4, tool_paths={"ffprobe": executable})
    assert record["engine"] == "ffprobe"
    assert "synthetic primary-parser unavailability" in record["parser_warning"]
    assert record["container"] == record["raw"]["ffprobe"]["format"]["format_long_name"]
    assert "mp4" in record["raw"]["ffprobe"]["format"]["format_name"].split(",")
    assert record["title"] == "Old.Release.Group.Title"
    assert record["video"][0]["codec"] == "h264"
    assert (record["video"][0]["width"], record["video"][0]["height"]) == (320, 180)
    assert record["video"][0]["resolution"] == "SD"
    assert record["video"][0]["hdr"] == ["Unknown"]
    assert record["audio"][0]["codec"] == "aac"
    assert record["audio"][0]["sample_rate"] == 48000
    assert record["audio"][0]["channels"] == 1
    assert record["raw"]["ffprobe"]["streams"]


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
    journal = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert "\xa9cmt" not in journal["old_tags"]
    assert "comment" not in journal["new_tags"]
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


@pytest.mark.parametrize("old_comment,requested,expected", [
    ("Old.Release.Group.Comment", "Synthetic Movie (2026)", ["Synthetic Movie (2026)"]),
    ("Old.Release.Group.Comment", "", None),
    (None, "Synthetic Movie (2026)", ["Synthetic Movie (2026)"]),
    (None, "", None),
])
def test_actual_mp4_comment_update_and_undo_preserves_payload_and_other_tags(real_mp4, tmp_path, old_comment, requested, expected):
    from mutagen.mp4 import MP4
    path = tmp_path / "movie.mp4"
    shutil.copy2(real_mp4, path)
    movie = MP4(path)
    if old_comment is None:
        movie.tags.pop("\xa9cmt", None)
    else:
        movie.tags["\xa9cmt"] = [old_comment]
    movie.tags["\xa9gen"] = ["keep the original genre"]
    movie.save()
    old_tags = dict(MP4(path).tags)
    def payloads(file):
        data, offset, payload = file.read_bytes(), 0, []
        while offset + 8 <= len(data):
            length, kind = struct.unpack_from(">I4s", data, offset)
            header = 8
            if length == 1:
                length = struct.unpack_from(">Q", data, offset + 8)[0]
                header = 16
            elif length == 0:
                length = len(data) - offset
            if kind == b"mdat":
                payload.append(data[offset + header:offset + length])
            assert length >= header
            offset += length
        assert payload
        return payload
    original_payload = payloads(path)
    result = media.update_tags(path, "Synthetic Movie (2026)", 2026, journal_dir=tmp_path / "journals", comment=requested)
    changed = MP4(path)
    assert changed.tags.get("\xa9cmt") == expected
    assert media.probe_file(path)["tags"].get("comment") == (expected[0] if expected else None)
    assert changed.tags["\xa9gen"] == old_tags["\xa9gen"]
    assert payloads(path) == original_payload
    journal = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert journal["old_tags"]["\xa9cmt"] == ([old_comment] if old_comment is not None else None)
    assert journal["new_tags"]["comment"] == requested
    assert media.undo_tags(result["manifest"])["status"] == "undone"
    assert dict(MP4(path).tags) == old_tags
    assert payloads(path) == original_payload
    assert media.probe_file(path)["tags"].get("comment") == old_comment


def test_mkv_rich_edit_without_extract_is_refused_before_any_mutation(tmp_path, monkeypatch):
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"unchanged placeholder")
    monkeypatch.setattr(media, "find_tool", lambda name, *args, **kwargs: "mkvpropedit.exe" if name == "mkvpropedit" else None)
    with pytest.raises(media.MediaToolError, match="MKVExtract is required"):
        media.update_tags(path, "Title", comment="Comment", journal_dir=tmp_path / "journals")
    assert path.read_bytes() == b"unchanged placeholder"
    assert not (tmp_path / "journals").exists()


def test_find_tool_prefers_explicit_then_bundled_then_path(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    tools = root / "tools" / "mediainfo"
    tools.mkdir(parents=True)
    binary = tools / "MediaInfo.exe"
    binary.write_bytes(b"synthetic path-only fixture")
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    monkeypatch.setattr(media.shutil, "which", lambda name: "from-path.exe")
    assert media.find_tool("mediainfo") == str(binary.resolve())
    explicit = tmp_path / "selected.exe"
    explicit.write_bytes(b"synthetic path-only fixture")
    assert media.find_tool("mediainfo", {"mediainfo": str(explicit)}) == str(explicit.resolve())
    with pytest.raises(media.MediaToolError, match="configured mediainfo"):
        media.find_tool("mediainfo", {"mediainfo": str(tmp_path / "missing.exe")})


def test_find_mkvextract_uses_selected_propedit_sibling(tmp_path):
    propedit = tmp_path / "mkvpropedit.exe"
    extract = tmp_path / "mkvextract.exe"
    propedit.write_bytes(b"synthetic")
    extract.write_bytes(b"synthetic")
    assert media.find_tool("mkvextract", {"mkvpropedit": str(propedit)}) == str(extract.resolve())


def test_validate_tag_update_accepts_uncreated_destinations_and_rejects_unsupported(tmp_path):
    assert media.validate_tag_update(tmp_path / "not-created.mp4", 2026, "Title").startswith("MP4")
    assert not (tmp_path / "not-created.mp4").exists()
    with pytest.raises(media.MediaToolError, match="Unsupported"):
        media.validate_tag_update(tmp_path / "movie.avi")
    with pytest.raises(ValueError, match="four digits"):
        media.validate_tag_update(tmp_path / "movie.mp4", "26")


def test_linked_manifest_is_persisted_before_mutation_and_restore_snapshot_after(real_mp4, tmp_path, monkeypatch):
    path = tmp_path / "movie.mp4"
    shutil.copy2(real_mp4, path)
    journal_dir = tmp_path / "metadata"
    file_manifest = tmp_path / "movie-journal.json"
    original = media._read_mp4_tags
    observed = []
    def instrumented(path):
        movie, old = original(path)
        save = movie.save
        def checked_save():
            record = json.loads(next(journal_dir.glob("metadata-*.json")).read_text(encoding="utf-8"))
            observed.append((record["status"], record["file_manifest"]))
            return save()
        movie.save = checked_save
        return movie, old
    monkeypatch.setattr(media, "_read_mp4_tags", instrumented)
    result = media.update_tags(path, "New Title", 2026, journal_dir=journal_dir, comment="Correct Comment", file_manifest=file_manifest)
    assert observed == [("writing", str(file_manifest))]
    media.undo_tags(result["manifest"])
    restored = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert restored["restored_snapshot"] == media._snapshot(path)
    assert restored["file_manifest"] == str(file_manifest)


def test_edit_mkv_xml_preserves_track_tags_and_untargeted_global_tags():
    old = """<Tags><Tag><Targets/><Simple><Name>TITLE</Name><String>Old global title</String></Simple>
      <Simple><Name>DATE_RELEASED</Name><String>1999</String></Simple>
      <Simple><Name>COMMENT</Name><String>Old global comment</String></Simple>
      <Simple><Name>GENRE</Name><String>Preserve this</String></Simple></Tag>
      <Tag><Targets><TrackUID>123</TrackUID></Targets><Simple><Name>COMMENT</Name>
      <TagLanguage>eng</TagLanguage><String>Keep track comment</String></Simple></Tag></Tags>"""
    new = ET.fromstring(media._edited_mkv_tags(old, "New Title", 2026, "New Comment"))
    global_values = {item.findtext("Name"): item.findtext("String") for item in new[0].findall("Simple")}
    assert global_values == {"TITLE": "New Title", "DATE_RELEASED": "2026", "COMMENT": "New Comment", "GENRE": "Preserve this"}
    assert ET.tostring(new[1]) == ET.tostring(ET.fromstring(old)[1])
    cleared = ET.fromstring(media._edited_mkv_tags(old, "New Title", comment=""))
    assert "COMMENT" not in {item.findtext("Name") for item in cleared[0].findall("Simple")}
    assert cleared[1].find("Simple/String").text == "Keep track comment"


def test_mkv_segment_title_backup_is_distinct_from_global_tags(tmp_path):
    def element(identifier, payload):
        length = next(length for length in range(1, 9) if len(payload) < (1 << (7 * length)) - 1)
        size = ((1 << (7 * length)) | len(payload)).to_bytes(length, "big")
        return identifier.to_bytes((identifier.bit_length() + 7) // 8, "big") + size + payload
    path = tmp_path / "headers-only.mkv"
    title = element(0x7BA9, "Original segment title".encode("utf-8"))
    info = element(0x1549A966, title)
    path.write_bytes(element(0x1A45DFA3, b"") + element(0x18538067, info))
    assert media._read_mkv_segment_title(path) == "Original segment title"


@pytest.fixture(scope="module")
def real_mkv(tmp_path_factory, real_mp4):
    if not media.find_tool("mkvpropedit") or not media.find_tool("mkvextract"):
        pytest.skip("Bundled MKVToolNix is needed for native MKV tag tests")
    import imageio_ffmpeg
    path = tmp_path_factory.mktemp("generated-mkv") / "synthetic.mkv"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(real_mp4),
                    "-map", "0", "-c", "copy", "-metadata", "title=Original segment title", str(path)], check=True)
    original = media._extract_mkv_tags(path)
    root = ET.fromstring(media._edited_mkv_tags(original, "Original global title", 1999, "Original global comment"))
    global_tag = next(tag for tag in root.findall("Tag") if tag.find("Targets/TrackUID") is None)
    extra = ET.SubElement(global_tag, "Simple")
    ET.SubElement(extra, "Name").text = "GENRE"
    ET.SubElement(extra, "String").text = "Preserve genre"
    track_tag = next(tag for tag in root.findall("Tag") if tag.find("Targets/TrackUID") is not None)
    extra = ET.SubElement(track_tag, "Simple")
    ET.SubElement(extra, "Name").text = "COMMENT"
    ET.SubElement(extra, "String").text = "Preserve track comment"
    media._write_mkv_tags_and_title(path, "Original segment title", ET.tostring(root, encoding="unicode"))
    return path


@pytest.mark.parametrize("comment", ["Correct Movie Comment", ""])
def test_native_mkv_title_year_comment_update_and_undo_preserve_other_tags_and_media(real_mkv, tmp_path, comment):
    import imageio_ffmpeg
    path = tmp_path / "movie.mkv"
    shutil.copy2(real_mkv, path)
    original_xml = media._extract_mkv_tags(path)
    original_title = media._read_mkv_segment_title(path)
    def packets(file):
        result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(file),
                                 "-map", "0", "-c", "copy", "-f", "framemd5", "-"], check=True, capture_output=True, text=True)
        return [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    original_packets = packets(path)
    result = media.update_tags(path, "Correct Movie Title", 2026, comment=comment, journal_dir=tmp_path / "metadata", file_manifest=tmp_path / "files.json")
    assert media._read_mkv_segment_title(path) == "Correct Movie Title"
    changed = ET.fromstring(media._extract_mkv_tags(path))
    old = ET.fromstring(original_xml)
    global_tag = next(tag for tag in changed.findall("Tag") if tag.find("Targets/TrackUID") is None)
    values = {item.findtext("Name"): item.findtext("String") for item in global_tag.findall("Simple")}
    assert values["TITLE"] == "Correct Movie Title"
    assert values["DATE_RELEASED"] == "2026"
    assert values.get("COMMENT") == (comment or None)
    assert values["GENRE"] == "Preserve genre"
    old_tracks = [ET.canonicalize(ET.tostring(tag, encoding="unicode"), strip_text=True) for tag in old.findall("Tag") if tag.find("Targets/TrackUID") is not None]
    new_tracks = [ET.canonicalize(ET.tostring(tag, encoding="unicode"), strip_text=True) for tag in changed.findall("Tag") if tag.find("Targets/TrackUID") is not None]
    assert new_tracks == old_tracks
    assert packets(path) == original_packets
    media.undo_tags(result["manifest"])
    assert media._read_mkv_segment_title(path) == original_title
    assert ET.canonicalize(media._extract_mkv_tags(path), strip_text=True) == ET.canonicalize(original_xml, strip_text=True)
    assert packets(path) == original_packets
    journal = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert journal["restored_snapshot"] == media._snapshot(path)


def test_external_subtitles_are_parsed_without_inflating_movie_or_embedded_stream_counts(real_mp4, tmp_path):
    subtitle = tmp_path / "Subs" / "movie.en.srt"
    subtitle.parent.mkdir()
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,200\nA synthetic caption.\n\n", encoding="utf-8")
    progress = []
    report = media.scan_library([real_mp4], external_subtitles={real_mp4: [subtitle, subtitle]},
                                progress=lambda current, total, phase: progress.append((current, total)))
    assert report.summary["files_total"] == 1
    assert report.summary["subtitle_streams"] == 0
    assert report.summary["external_subtitles_total"] == 1
    assert report.summary["external_subtitles_ok"] == 1
    assert report.summary["external_subtitles_failed"] == 0
    assert sum(report.summary["external_subtitle_codecs"].values()) == 1
    assert report.records[0]["text"] == []
    external = report.records[0]["external_text"][0]
    assert external["path"] == str(subtitle.resolve())
    assert external["container"] == "SubRip"
    assert external["language"] == "Unknown"
    assert external["raw"]
    assert not report.errors
    assert all(0 <= current <= total == 1 for current, total in progress)


def test_external_subtitle_cache_invalidates_independently_from_movie(real_mp4, tmp_path, monkeypatch):
    subtitle = tmp_path / "movie.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,200\nFirst synthetic caption.\n\n", encoding="utf-8")
    cache = tmp_path / "cache.json"
    original = media.probe_file
    calls = []
    def probe(path, **kwargs):
        calls.append(str(Path(path).resolve()))
        return original(path, **kwargs)
    monkeypatch.setattr(media, "probe_file", probe)
    mapping = {str(real_mp4): [str(subtitle)]}
    first = media.scan_library([real_mp4], cache_path=cache, external_subtitles=mapping)
    second = media.scan_library([real_mp4], cache_path=cache, external_subtitles=mapping)
    assert len(calls) == 2
    assert second.records[0]["cached"] is True
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,250\nSecond longer synthetic caption.\n\n", encoding="utf-8")
    third = media.scan_library([real_mp4], cache_path=cache, external_subtitles=mapping)
    assert calls.count(str(real_mp4.resolve())) == 1
    assert calls.count(str(subtitle.resolve())) == 2
    assert third.summary["external_subtitles_ok"] == 1
    without = media.scan_library([real_mp4], cache_path=cache)
    assert not without.records[0]["external_text"]
    assert without.summary["external_subtitles_total"] == 0


def test_external_subtitle_failures_report_separately_from_movies(real_mp4, tmp_path):
    subtitle = tmp_path / "unreadable.srt"
    subtitle.write_bytes(b"not subtitle data")
    report = media.scan_library([real_mp4], external_subtitles={real_mp4: [subtitle]})
    assert report.summary["files_total"] == report.summary["files_ok"] == 1
    assert report.summary["files_failed"] == 0
    assert report.summary["external_subtitles_failed"] == 1
    assert report.errors[0]["kind"] == "external-subtitle"
    assert report.errors[0]["path"] == str(subtitle.resolve())


def test_external_subtitle_exports_keep_companion_paths_and_raw_details(real_mp4, tmp_path):
    subtitle = tmp_path / "movie.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,200\nA synthetic caption.\n\n", encoding="utf-8")
    report = media.scan_library([real_mp4], external_subtitles={real_mp4: [subtitle]})
    json_file, csv_file = tmp_path / "report.json", tmp_path / "report.csv"
    media.export_report(report, json_file)
    media.export_report(report, csv_file)
    assert json.loads(json_file.read_text(encoding="utf-8"))["records"][0]["external_text"][0]["raw"]
    rows = list(csv.DictReader(csv_file.open(encoding="utf-8-sig", newline="")))
    assert len(rows) == 1
    assert rows[0]["external_subtitle_paths"] == str(subtitle.resolve())


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
