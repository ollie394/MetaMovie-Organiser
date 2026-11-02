"""Read media streams, summarise a library and explicitly edit descriptive tags.

Technical values always come from the file parser, never its filename. Embedded
tag updates are a separate opt-in operation and do not transcode media streams.
Tag undo restores the edited tag values, not an identical binary file layout.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from support import Cancelled, check_cancel


CACHE_VERSION = 1


class MediaToolError(RuntimeError):
    pass


@dataclass
class MediaReport:
    records: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    engine: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cancelled: bool = False

    def to_dict(self):
        return asdict(self)


def _number(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return value
    match = re.match(r"^\s*(-?\d+(?:\.\d+)?)", str(value).replace(" ", ""))
    if not match:
        return default
    result = float(match.group(1))
    return int(result) if result.is_integer() else result


def _text(value, default=""):
    if value is None:
        return default
    return " / ".join(map(str, value)) if isinstance(value, list) else str(value)


def _get(track, *names, default=""):
    for name in names:
        value = track.get(name)
        if value is not None and value != "":
            return value
    return default


def resolution_label(width, height):
    """Classify by the encoded canvas width as well as height (including crops)."""
    width, height = _number(width, 0), _number(height, 0)
    if not width or not height:
        return "Unknown"
    long, short = max(width, height), min(width, height)
    for threshold, short_threshold, label in ((7680, 4320, "8K"), (3840, 2160, "4K"),
                                             (2560, 1440, "1440p"), (1920, 1080, "1080p"),
                                             (1280, 720, "720p")):
        if long >= threshold or short >= short_threshold:
            return label
    return "SD"


def hdr_labels(track):
    """Only identify HDR when the parser supplies HDR/transfer evidence."""
    evidence = " ".join(_text(track.get(name)) for name in (
        "hdr_format", "hdr_format_string", "hdr_format_commercial", "hdr_format_commercial_ifany",
        "hdr_format_compatibility", "hdr_format_profile", "transfer_characteristics",
        "transfer_characteristics_original", "color_transfer", "side_data_text"))
    lower = evidence.casefold()
    labels = []
    if "dolby vision" in lower or "dovi" in lower or "dvhe." in lower or "dvh1." in lower:
        labels.append("Dolby Vision")
    if "hdr10+" in lower or "2094-40" in lower or "2094 app 4" in lower:
        labels.append("HDR10+")
    # The + variant alone is not evidence that a parser confirmed the base layer.
    without_plus = lower.replace("hdr10+", "")
    if "hdr10" in without_plus:
        labels.append("HDR10")
    if "hlg" in lower or "arib-std-b67" in lower or "arib std-b67" in lower:
        labels.append("HLG")
    pq = any(value in lower for value in ("smpte2084", "smpte st 2084", "smpte st2084", "pq"))
    if not labels and pq:
        labels.append("HDR (PQ)")
    if not labels and any(value in lower for value in ("bt.709", "bt709", "srgb", "iec 61966", "gamma 2.")):
        labels.append("SDR")
    return labels or ["Unknown"]


def _codec(track):
    return _text(_get(track, "commercial_name", "format_commercial_ifany", "format", "codec_name"), "Unknown") or "Unknown"


def normalise_mediainfo(data, path, engine="MediaInfo"):
    tracks = data.get("tracks", [])
    general = next((item for item in tracks if str(item.get("track_type", "")).lower() == "general"), {})
    path = Path(path)
    record = {
        "path": str(path), "size": int(_number(general.get("file_size"), path.stat().st_size)),
        "duration": float(_number(general.get("duration"), 0)) / 1000,
        "container": _text(_get(general, "format", "codec_id"), "Unknown") or "Unknown",
        "format_profile": _text(general.get("format_profile")),
        "commercial_name": _text(_get(general, "commercial_name", "format_commercial_ifany")),
        "title": _text(_get(general, "title", "movie_name")),
        "overall_bitrate": _number(general.get("overall_bit_rate")),
        "tags": {name: value for name, value in general.items() if name not in {"track_type", "complete_name"}},
        "video": [], "audio": [], "text": [], "other_tracks": [],
        "raw": data, "engine": engine, "error": "",
    }
    for track in tracks:
        kind = str(track.get("track_type", "")).casefold()
        if kind == "video":
            video = {
                "format": _text(track.get("format")), "codec": _codec(track),
                "codec_id": _text(track.get("codec_id")), "profile": _text(track.get("format_profile")),
                "width": _number(track.get("width")), "height": _number(track.get("height")),
                "aspect_ratio": _text(_get(track, "display_aspect_ratio", "display_aspect_ratio_string")),
                "frame_rate": _number(track.get("frame_rate")), "bitrate": _number(track.get("bit_rate")),
                "color_space": _text(track.get("color_space")),
                "color_primaries": _text(track.get("colour_primaries", track.get("color_primaries"))),
                "transfer_characteristics": _text(track.get("transfer_characteristics")),
                "matrix_coefficients": _text(track.get("matrix_coefficients")),
                "chroma_subsampling": _text(track.get("chroma_subsampling")),
                "bit_depth": _number(track.get("bit_depth")),
                "hdr": hdr_labels(track), "hdr_details": _text(_get(track, "hdr_format_string", "hdr_format")),
                "scan_type": _text(track.get("scan_type")), "raw": track,
            }
            video["resolution"] = resolution_label(video["width"], video["height"])
            record["video"].append(video)
        elif kind == "audio":
            record["audio"].append({
                "format": _text(track.get("format")), "codec": _codec(track),
                "codec_id": _text(track.get("codec_id")), "profile": _text(track.get("format_profile")),
                "sample_rate": _number(track.get("sampling_rate")), "channels": _number(track.get("channel_s")),
                "channel_layout": _text(track.get("channel_layout")), "bit_depth": _number(track.get("bit_depth")),
                "language": _text(_get(track, "language", "language_string"), "Unknown") or "Unknown",
                "bitrate": _number(track.get("bit_rate")), "title": _text(track.get("title")),
                "default": _text(track.get("default")), "forced": _text(track.get("forced")), "raw": track,
            })
        elif kind in ("text", "subtitle"):
            record["text"].append({
                "format": _text(track.get("format")), "codec": _codec(track),
                "codec_id": _text(track.get("codec_id")),
                "language": _text(_get(track, "language", "language_string"), "Unknown") or "Unknown",
                "title": _text(track.get("title")), "default": _text(track.get("default")),
                "forced": _text(track.get("forced")), "raw": track,
            })
        elif kind != "general":
            record["other_tracks"].append(track)
    return record


def _snapshot(path):
    stat = Path(path).stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def find_tool(name, tool_paths=None):
    configured = (tool_paths or {}).get(name)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise MediaToolError(f"The configured {name} executable does not exist.")
        return str(path.resolve())
    located = shutil.which(name)
    if located:
        return located
    if os.name == "nt" and name == "mkvpropedit":
        for base in (os.environ.get("ProgramFiles", "C:/Program Files"), os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")):
            path = Path(base) / "MKVToolNix" / "mkvpropedit.exe"
            if path.is_file():
                return str(path)
    return None


def _run(command, timeout=120, cancel_event=None):
    check_cancel(cancel_event)
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
              "encoding": "utf-8", "errors": "replace"}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([str(item) for item in command], **kwargs)
    start = time.monotonic()
    try:
        while True:
            try:
                output, error = process.communicate(timeout=0.15)
                return process.returncode, output, error
            except subprocess.TimeoutExpired:
                check_cancel(cancel_event)
                if time.monotonic() - start > timeout:
                    raise MediaToolError(f"{Path(command[0]).name} exceeded its {timeout}-second time limit.")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _ffprobe_record(path, executable, cancel_event=None):
    code, output, error = _run([executable, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], cancel_event=cancel_event)
    if code:
        raise MediaToolError(error.strip() or "ffprobe could not read this file.")
    source = json.loads(output)
    info = source.get("format", {})
    tags = info.get("tags", {})
    general = {"track_type": "General", "format": info.get("format_long_name") or info.get("format_name"),
               "duration": float(info.get("duration", 0)) * 1000, "file_size": info.get("size"),
               "overall_bit_rate": info.get("bit_rate"), "title": tags.get("title", "")}
    tracks = [general]
    for stream in source.get("streams", []):
        kind = {"video": "Video", "audio": "Audio", "subtitle": "Text"}.get(stream.get("codec_type"), "Other")
        rate = stream.get("avg_frame_rate", "0/0")
        try:
            numerator, denominator = map(float, rate.split("/"))
            frame_rate = numerator / denominator if denominator else None
        except (ValueError, AttributeError):
            frame_rate = _number(rate)
        stream_tags = stream.get("tags", {})
        track = {"track_type": kind, "format": stream.get("codec_name"),
                 "codec_id": stream.get("codec_tag_string"), "format_profile": stream.get("profile"),
                 "width": stream.get("width"), "height": stream.get("height"),
                 "display_aspect_ratio": stream.get("display_aspect_ratio"), "frame_rate": frame_rate,
                 "bit_rate": stream.get("bit_rate"), "color_space": stream.get("color_space"),
                 "color_primaries": stream.get("color_primaries"), "color_transfer": stream.get("color_transfer"),
                 "chroma_subsampling": stream.get("pix_fmt"), "bit_depth": stream.get("bits_per_raw_sample"),
                 "sampling_rate": stream.get("sample_rate"), "channel_s": stream.get("channels"),
                 "channel_layout": stream.get("channel_layout"), "language": stream_tags.get("language"),
                 "title": stream_tags.get("title"), "side_data_text": json.dumps(stream.get("side_data_list", [])),
                 "raw_ffprobe": stream}
        tracks.append(track)
    record = normalise_mediainfo({"tracks": tracks}, path, engine="ffprobe")
    record["raw"]["ffprobe"] = source
    return record


def probe_file(path, tool_paths=None, cancel_event=None):
    path = Path(path).resolve()
    check_cancel(cancel_event)
    if not path.is_file():
        raise MediaToolError("The media file no longer exists.")
    before = _snapshot(path)
    errors = []
    mediainfo_cli = (tool_paths or {}).get("mediainfo")
    try:
        if mediainfo_cli:
            executable = find_tool("mediainfo", tool_paths)
            code, output, error = _run([executable, "--Output=JSON", str(path)], cancel_event=cancel_event)
            if code:
                raise MediaToolError(error.strip() or "MediaInfo CLI could not inspect this file.")
            raw = json.loads(output)
            # CLI uses CamelCase and @type; pymediainfo gives snake_case.
            tracks = []
            for track in raw.get("media", {}).get("track", []):
                def snake(key):
                    key = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key)
                    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
                    return re.sub(r"_+", "_", key).lower()
                converted = {snake(key): value for key, value in track.items()}
                if "channels" in converted:
                    converted["channel_s"] = converted["channels"]
                converted["track_type"] = track.get("@type", "")
                if converted.get("duration"):
                    converted["duration"] = float(converted["duration"]) * 1000
                tracks.append(converted)
            record = normalise_mediainfo({"tracks": tracks, "cli_raw": raw}, path, "MediaInfo CLI")
        else:
            from pymediainfo import MediaInfo
            parsed = MediaInfo.parse(str(path), full=True)
            record = normalise_mediainfo(parsed.to_data(), path)
        if not record["video"] and not record["audio"] and not record["text"]:
            raise MediaToolError("MediaInfo found no readable media streams.")
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, Cancelled):
            raise
        errors.append(str(exc))
        ffprobe = find_tool("ffprobe", tool_paths)
        if not ffprobe:
            raise MediaToolError("MediaInfo could not read this file and ffprobe is unavailable. " + "; ".join(errors)) from exc
        record = _ffprobe_record(path, ffprobe, cancel_event)
        record["parser_warning"] = "MediaInfo fallback: " + "; ".join(errors)
    check_cancel(cancel_event)
    if before != _snapshot(path):
        raise MediaToolError("The file changed while its streams were being inspected.")
    record["snapshot"] = before
    return record


def summarise(records):
    categories = {name: Counter() for name in ("containers", "video_codecs", "audio_codecs", "subtitle_codecs",
                   "resolutions", "hdr", "audio_languages", "subtitle_languages", "bit_depths", "audio_bit_depths",
                   "audio_channels", "audio_sample_rates", "frame_rates", "chroma_subsampling", "video_profiles", "audio_profiles")}
    stream_counts = {name: Counter() for name in ("video_codecs", "audio_codecs", "subtitle_codecs", "audio_languages", "subtitle_languages")}
    good = [record for record in records if not record.get("error")]
    for record in good:
        categories["containers"][record.get("container") or "Unknown"] += 1
        mapping = {"video_codecs": ("video", "codec"), "audio_codecs": ("audio", "codec"),
                   "subtitle_codecs": ("text", "codec"), "audio_languages": ("audio", "language"),
                   "subtitle_languages": ("text", "language"), "resolutions": ("video", "resolution"),
                   "bit_depths": ("video", "bit_depth"), "audio_bit_depths": ("audio", "bit_depth"),
                   "audio_channels": ("audio", "channels"), "audio_sample_rates": ("audio", "sample_rate"),
                   "frame_rates": ("video", "frame_rate"), "chroma_subsampling": ("video", "chroma_subsampling"),
                   "video_profiles": ("video", "profile"), "audio_profiles": ("audio", "profile")}
        for category, (kind, key) in mapping.items():
            values = [_text(track.get(key)) or "Unknown" for track in record.get(kind, [])]
            categories[category].update(set(values))
            if category in stream_counts:
                stream_counts[category].update(values)
        categories["hdr"].update({label for track in record.get("video", []) for label in track.get("hdr", ["Unknown"])})
    result = {name: dict(counter.most_common()) for name, counter in categories.items()}
    result.update({"files_total": len(records), "files_ok": len(good), "files_failed": len(records) - len(good),
                   "total_size": sum(record.get("size", 0) for record in records),
                   "total_duration": sum(record.get("duration", 0) for record in good),
                   "stream_counts": {name: dict(counter.most_common()) for name, counter in stream_counts.items()},
                   "video_streams": sum(len(record.get("video", [])) for record in good),
                   "audio_streams": sum(len(record.get("audio", [])) for record in good),
                   "subtitle_streams": sum(len(record.get("text", [])) for record in good),
                   "counting_note": "Category counts are distinct files. A file may belong to several codec/HDR categories; stream_counts counts every stream."})
    return result


def scan_library(paths, cancel_event=None, progress=None, cache_path=None, tool_paths=None):
    cache = {}
    if cache_path:
        try:
            data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            if data.get("version") == CACHE_VERSION:
                cache = data.get("files", {})
        except (OSError, ValueError, AttributeError):
            pass
    # Deduplicate before work, including the Windows case-insensitive path form.
    unique = {}
    for path in paths:
        resolved = Path(path).resolve()
        unique.setdefault(os.path.normcase(str(resolved)), resolved)
    records, refreshed = [], {}
    for index, path in enumerate(unique.values(), 1):
        check_cancel(cancel_event)
        if progress:
            progress(index - 1, len(unique), f"Inspecting {path.name}")
        try:
            snapshot = _snapshot(path)
            cache_key = os.path.normcase(str(path))
            entry = cache.get(cache_key, {})
            if entry.get("snapshot") == snapshot and entry.get("tool_paths", {}) == (tool_paths or {}):
                record = dict(entry["record"])
                record["cached"] = True
            else:
                record = probe_file(path, tool_paths=tool_paths, cancel_event=cancel_event)
                record["cached"] = False
            refreshed[cache_key] = {"snapshot": snapshot, "tool_paths": tool_paths or {}, "record": record}
        except Cancelled:
            if cache_path:
                _atomic_json(cache_path, {"version": CACHE_VERSION, "files": refreshed})
            raise
        except Exception as exc:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            record = {"path": str(path), "size": size, "duration": 0, "container": "Unknown",
                      "video": [], "audio": [], "text": [], "raw": {}, "engine": "", "error": str(exc)}
        records.append(record)
        if progress:
            progress(index, len(unique), f"Inspected {path.name}")
    if cache_path:
        _atomic_json(cache_path, {"version": CACHE_VERSION, "files": refreshed})
    check_cancel(cancel_event)
    return MediaReport(records=records, summary=summarise(records),
                       errors=[{"path": item["path"], "error": item["error"]} for item in records if item.get("error")],
                       engine=" / ".join(sorted({item.get("engine", "") for item in records if item.get("engine")})))


def export_report(report, path, format=None):
    path = Path(path)
    format = (format or path.suffix.lstrip(".")).lower()
    if format == "json":
        _atomic_json(path, report.to_dict() if isinstance(report, MediaReport) else report)
    elif format == "csv":
        records = report.records if isinstance(report, MediaReport) else report["records"]
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = ["path", "size", "duration", "container", "format_profile", "commercial_name", "overall_bitrate", "title",
                   "video_codecs", "video_codec_ids", "video_profiles", "resolutions", "dimensions", "aspect_ratios", "hdr",
                   "video_bit_depths", "color_spaces", "color_primaries", "transfer_characteristics", "chroma_subsampling",
                   "frame_rates", "video_bitrates", "audio_codecs", "audio_codec_ids", "audio_languages", "audio_channels",
                   "audio_channel_layouts", "audio_sample_rates", "audio_bit_depths", "audio_bitrates", "subtitle_codecs",
                   "subtitle_codec_ids", "subtitle_languages", "engine", "error"]
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            for item in records:
                row = {key: item.get(key, "") for key in ("path", "size", "duration", "container", "format_profile", "commercial_name",
                                                         "overall_bitrate", "title", "engine", "error")}
                for name, group, field_name in (("video_codecs", "video", "codec"), ("resolutions", "video", "resolution"),
                        ("video_codec_ids", "video", "codec_id"), ("video_profiles", "video", "profile"),
                        ("aspect_ratios", "video", "aspect_ratio"), ("color_spaces", "video", "color_space"),
                        ("color_primaries", "video", "color_primaries"), ("transfer_characteristics", "video", "transfer_characteristics"),
                        ("chroma_subsampling", "video", "chroma_subsampling"),
                        ("video_bit_depths", "video", "bit_depth"), ("frame_rates", "video", "frame_rate"),
                        ("video_bitrates", "video", "bitrate"), ("audio_codecs", "audio", "codec"),
                        ("audio_codec_ids", "audio", "codec_id"), ("audio_bit_depths", "audio", "bit_depth"),
                        ("audio_bitrates", "audio", "bitrate"), ("audio_channel_layouts", "audio", "channel_layout"),
                        ("audio_languages", "audio", "language"), ("audio_channels", "audio", "channels"),
                        ("audio_sample_rates", "audio", "sample_rate"), ("subtitle_codecs", "text", "codec"),
                        ("subtitle_codec_ids", "text", "codec_id"), ("subtitle_languages", "text", "language")):
                    row[name] = " | ".join(_text(track.get(field_name)) for track in item.get(group, []))
                row["dimensions"] = " | ".join(f"{track.get('width')}x{track.get('height')}" for track in item.get("video", []))
                row["hdr"] = " | ".join(" + ".join(track.get("hdr", [])) for track in item.get("video", []))
                # Spreadsheet apps must not treat movie titles/paths as formulas.
                for key, value in row.items():
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                        row[key] = "'" + value
                writer.writerow(row)
    else:
        raise ValueError("Export format must be JSON or CSV.")
    return path


def metadata_capability(path, tool_paths=None):
    suffix = Path(path).suffix.casefold()
    if suffix in {".mp4", ".m4v", ".mov"}:
        return "MP4 title and year"
    if suffix == ".mkv":
        return "MKV title" if find_tool("mkvpropedit", tool_paths) else "Unavailable: install MKVToolNix or choose mkvpropedit in settings"
    return "Unsupported: descriptive tags are currently editable for MP4, M4V, MOV and MKV"


def _read_mp4_tags(path):
    from mutagen.mp4 import MP4
    media = MP4(path)
    return media, {key: list(media.tags[key]) if media.tags and key in media.tags else None for key in ("\xa9nam", "\xa9day")}


def update_tags(path, title, year=None, journal_dir=None, tool_paths=None, cancel_event=None):
    """Change only the reviewed title/year and save an independent undo record."""
    path = Path(path).resolve()
    if not title or not str(title).strip():
        raise ValueError("A title is required for an embedded-tag update.")
    if year is not None and not re.fullmatch(r"\d{4}", str(year)):
        raise ValueError("The metadata year must contain four digits.")
    if journal_dir is None:
        raise ValueError("A journal directory is required before changing embedded tags.")
    check_cancel(cancel_event)
    capability = metadata_capability(path, tool_paths)
    if capability.startswith(("Unavailable", "Unsupported")):
        raise MediaToolError(capability)
    suffix = path.suffix.casefold()
    before = _snapshot(path)
    if suffix == ".mkv":
        original = probe_file(path, tool_paths=tool_paths, cancel_event=cancel_event)
        old = {"title": original.get("title") or None}
    else:
        media, old = _read_mp4_tags(path)
    manifest = Path(journal_dir) / f"metadata-{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    record = {"version": 1, "kind": "metadata", "path": str(path), "created_at": datetime.now(timezone.utc).isoformat(),
              "status": "prepared", "undone": False, "before": before, "old_tags": old,
              "new_tags": {"title": str(title).strip(), "year": str(year) if year is not None else None},
              "format": "mkv" if suffix == ".mkv" else "mp4",
              "note": "Undo restores targeted descriptive tags. It does not recreate the original binary layout."}
    _atomic_json(manifest, record)
    check_cancel(cancel_event)
    if before != _snapshot(path):
        record.update(status="not-applied", error="File changed before editing.")
        _atomic_json(manifest, record)
        raise MediaToolError(record["error"])
    record["status"] = "writing"
    _atomic_json(manifest, record)
    try:
        if suffix == ".mkv":
            executable = find_tool("mkvpropedit", tool_paths)
            code, output, error = _run([executable, str(path), "--edit", "info", "--set", "title=" + str(title).strip()], cancel_event=None)
            if code not in (0, 1):
                raise MediaToolError(error.strip() or output.strip() or "MKV title update failed.")
            if code == 1:
                record["warning"] = output.strip() or error.strip() or "MKVToolNix reported a warning; inspect the file."
        else:
            if media.tags is None:
                media.add_tags()
            media.tags["\xa9nam"] = [str(title).strip()]
            if year is not None:
                media.tags["\xa9day"] = [str(year)]
            media.save()
        record["after"] = _snapshot(path)
        record["status"] = "done"
        _atomic_json(manifest, record)
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        record["after"] = _snapshot(path) if path.exists() else None
        _atomic_json(manifest, record)
        raise MediaToolError(f"Embedded-tag editing failed; a recovery record remains at {manifest}. {exc}") from exc
    return {"status": "done", "manifest": str(manifest), "path": str(path), "details": capability, "warning": record.get("warning", "")}


def undo_tags(manifest, tool_paths=None, cancel_event=None):
    manifest = Path(manifest)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    if record.get("kind") != "metadata":
        raise MediaToolError("This is not an embedded-tag recovery record.")
    if record.get("undone"):
        return {"status": "already-undone", "manifest": str(manifest), "path": record["path"]}
    if record.get("status") != "done":
        raise MediaToolError("The tag update did not complete cleanly. Inspect the recovery record before restoring tags.")
    path = Path(record["path"])
    check_cancel(cancel_event)
    if not path.exists() or record.get("after") != _snapshot(path):
        raise MediaToolError("The file has changed since the metadata update; Undo left it untouched.")
    if record["format"] == "mkv":
        executable = find_tool("mkvpropedit", tool_paths)
        if not executable:
            raise MediaToolError("MKVToolNix is required to restore the MKV title.")
        value = record["old_tags"].get("title")
        action = ["--set", "title=" + str(value)] if value is not None else ["--delete", "title"]
        code, output, error = _run([executable, str(path), "--edit", "info", *action], cancel_event=None)
        if code not in (0, 1):
            raise MediaToolError(error.strip() or output.strip() or "Restoring the MKV title failed.")
    else:
        media, current = _read_mp4_tags(path)
        if media.tags is None:
            media.add_tags()
        for key, value in record["old_tags"].items():
            if value is None:
                media.tags.pop(key, None)
            else:
                media.tags[key] = value
        media.save()
    record.update(undone=True, status="undone", undone_at=datetime.now(timezone.utc).isoformat())
    _atomic_json(manifest, record)
    return {"status": "undone", "manifest": str(manifest), "path": str(path)}
