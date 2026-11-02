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
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from support import Cancelled, check_cancel


CACHE_VERSION = 2


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
    if name in {"mkvextract", "mkvmerge"} and (tool_paths or {}).get("mkvpropedit"):
        selected = Path(tool_paths["mkvpropedit"]).expanduser()
        if not selected.is_file():
            raise MediaToolError("The configured mkvpropedit executable does not exist.")
        sibling = selected.with_name(name + (".exe" if selected.suffix.lower() == ".exe" else ""))
        if sibling.is_file():
            return str(sibling.resolve())
    family = "mkvpropedit" if name in {"mkvextract", "mkvmerge"} else name
    filenames = ["MediaInfo.exe", "mediainfo.exe"] if name == "mediainfo" else [name + ".exe", name]
    roots = [Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)), Path(__file__).resolve().parent]
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    for root in dict.fromkeys(roots):
        for folder in (root / "tools" / family, root / "tools"):
            for filename in filenames:
                candidate = folder / filename
                if candidate.is_file():
                    return str(candidate.resolve())
    located = shutil.which(name)
    if located:
        return located
    if os.name == "nt" and name in {"mkvpropedit", "mkvextract", "mkvmerge"}:
        for base in (os.environ.get("ProgramFiles", "C:/Program Files"), os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")):
            path = Path(base) / "MKVToolNix" / (name + ".exe")
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
    external = {}
    for record in records:
        for track in record.get("external_text", []):
            external.setdefault(os.path.normcase(track["path"]), []).append(track)
    external_codecs, external_languages = Counter(), Counter()
    external_ok = 0
    for tracks in external.values():
        readable = [track for track in tracks if not track.get("error")]
        if readable:
            external_ok += 1
            external_codecs.update({track.get("codec") or "Unknown" for track in readable})
            external_languages.update({track.get("language") or "Unknown" for track in readable})
    result.update(external_subtitles_total=len(external), external_subtitles_ok=external_ok,
                  external_subtitles_failed=len(external) - external_ok,
                  external_subtitle_codecs=dict(external_codecs.most_common()),
                  external_subtitle_languages=dict(external_languages.most_common()))
    return result


def scan_library(paths, cancel_event=None, progress=None, cache_path=None, tool_paths=None, external_subtitles=None):
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
    companions = {os.path.normcase(str(Path(movie).resolve())): list(subtitles)
                  for movie, subtitles in (external_subtitles or {}).items()}
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
            record.pop("external_text", None)
            refreshed[cache_key] = {"snapshot": snapshot, "tool_paths": tool_paths or {}, "record": dict(record)}
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
        record["external_text"] = []
        seen_companions = set()
        for subtitle in companions.get(os.path.normcase(str(path)), []):
            check_cancel(cancel_event)
            subtitle = Path(subtitle).resolve()
            key = os.path.normcase(str(subtitle))
            if key in seen_companions:
                continue
            seen_companions.add(key)
            if progress:
                progress(index - 1, len(unique), f"Inspecting subtitle {subtitle.name}")
            try:
                snapshot = _snapshot(subtitle)
                entry = refreshed.get(key, cache.get(key, {}))
                if entry.get("snapshot") == snapshot and entry.get("tool_paths", {}) == (tool_paths or {}):
                    subtitle_record = entry["record"]
                else:
                    subtitle_record = probe_file(subtitle, tool_paths=tool_paths, cancel_event=cancel_event)
                if not subtitle_record.get("text"):
                    raise MediaToolError("No subtitle stream was reported in this companion file.")
                refreshed[key] = {"snapshot": snapshot, "tool_paths": tool_paths or {}, "record": dict(subtitle_record)}
                for track in subtitle_record["text"]:
                    record["external_text"].append({**track, "path": str(subtitle), "size": subtitle_record.get("size", 0),
                        "container": subtitle_record.get("container", ""), "engine": subtitle_record.get("engine", ""), "error": ""})
            except Cancelled:
                if cache_path:
                    _atomic_json(cache_path, {"version": CACHE_VERSION, "files": refreshed})
                raise
            except Exception as exc:
                record["external_text"].append({"path": str(subtitle), "codec": "Unknown", "codec_id": "", "language": "Unknown",
                                                "format": "Unknown", "raw": {}, "error": str(exc)})
        records.append(record)
        if progress:
            progress(index, len(unique), f"Inspected {path.name}")
    if cache_path:
        _atomic_json(cache_path, {"version": CACHE_VERSION, "files": refreshed})
    check_cancel(cancel_event)
    errors = [{"path": item["path"], "error": item["error"]} for item in records if item.get("error")]
    errors.extend({"path": item["path"], "error": item["error"], "kind": "external-subtitle"}
                  for item in {track["path"]: track for record in records for track in record["external_text"] if track.get("error")}.values())
    return MediaReport(records=records, summary=summarise(records), errors=errors,
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
                   "subtitle_codec_ids", "subtitle_languages", "external_subtitle_paths", "external_subtitle_codecs",
                   "external_subtitle_languages", "external_subtitle_errors", "engine", "error"]
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
                for name, key in (("external_subtitle_paths", "path"), ("external_subtitle_codecs", "codec"),
                                  ("external_subtitle_languages", "language"), ("external_subtitle_errors", "error")):
                    row[name] = " | ".join(dict.fromkeys(_text(track.get(key)) for track in item.get("external_text", [])))
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
        return "MP4 title, year and optional comment"
    if suffix == ".mkv":
        if not find_tool("mkvpropedit", tool_paths):
            return "Unavailable: install MKVToolNix or choose mkvpropedit in settings"
        return "MKV title, year and optional comment" if find_tool("mkvextract", tool_paths) else "MKV segment title only; MKVExtract is required for year/comment and global-title tags"
    return "Unsupported: descriptive tags are currently editable for MP4, M4V, MOV and MKV"


def validate_tag_update(path, year=None, comment=None, tool_paths=None):
    """Preflight tools/types without reading or changing a destination file."""
    if year is not None and not re.fullmatch(r"\d{4}", str(year)):
        raise ValueError("The metadata year must contain four digits.")
    capability = metadata_capability(path, tool_paths)
    if capability.startswith(("Unavailable", "Unsupported")):
        raise MediaToolError(capability)
    if Path(path).suffix.casefold() == ".mkv" and (year is not None or comment is not None) and not find_tool("mkvextract", tool_paths):
        raise MediaToolError("MKVExtract is required to preserve existing tags while editing MKV year/comment. Choose the complete bundled MKVToolNix tools, or request only a title.")
    return capability


def _ebml_header(stream, boundary):
    """Read a bounded EBML header, skipping payloads rather than media bytes."""
    if stream.tell() >= boundary:
        return None
    first = stream.read(1)
    if not first:
        return None
    length = next((length for length in range(1, 5) if first[0] & (0x80 >> (length - 1))), None)
    if length is None:
        raise MediaToolError("Cannot safely read the Matroska element identifier.")
    identifier = first + stream.read(length - 1)
    if len(identifier) != length:
        raise MediaToolError("The Matroska element header is truncated.")
    first = stream.read(1)
    if not first:
        raise MediaToolError("The Matroska element length is missing.")
    size_length = next((length for length in range(1, 9) if first[0] & (0x80 >> (length - 1))), None)
    if size_length is None:
        raise MediaToolError("Cannot safely read the Matroska element length.")
    encoded = first + stream.read(size_length - 1)
    if len(encoded) != size_length:
        raise MediaToolError("The Matroska element length is truncated.")
    size = int.from_bytes(encoded, "big") & ((1 << (7 * size_length)) - 1)
    if size == (1 << (7 * size_length)) - 1:
        size = None
    if size is not None and stream.tell() + size > boundary:
        raise MediaToolError("The Matroska element extends beyond its container.")
    return int.from_bytes(identifier, "big"), size


def _read_mkv_segment_title(path):
    """Read the exact segment Title, not a possibly different global TITLE tag.

    IDs follow the Matroska element specification: Info 0x1549A966, Title
    0x7BA9. Only headers and the title payload are read. Unknown-size children
    before Info are refused instead of making an unsafe metadata backup.
    """
    boundary = Path(path).stat().st_size
    with Path(path).open("rb") as stream:
        for _ in range(32):
            header = _ebml_header(stream, boundary)
            if header is None:
                break
            element, size = header
            if element == 0x18538067:
                segment_end = stream.tell() + size if size is not None else boundary
                for _ in range(10000):
                    child = _ebml_header(stream, segment_end)
                    if child is None:
                        break
                    child_id, child_size = child
                    if child_size is None:
                        raise MediaToolError("Cannot safely locate the original MKV title beyond an unknown-size element.")
                    if child_id == 0x1549A966:
                        info_end = stream.tell() + child_size
                        while stream.tell() < info_end:
                            info = _ebml_header(stream, info_end)
                            if info is None:
                                break
                            info_id, info_size = info
                            if info_size is None:
                                raise MediaToolError("The MKV Info header has an unsupported unknown-size child.")
                            if info_id == 0x7BA9:
                                if info_size > 1024 * 1024:
                                    raise MediaToolError("The original MKV title is too large to back up safely.")
                                return stream.read(info_size).decode("utf-8").rstrip("\0")
                            stream.seek(info_size, os.SEEK_CUR)
                        return None
                    stream.seek(child_size, os.SEEK_CUR)
                break
            if size is None:
                break
            stream.seek(size, os.SEEK_CUR)
    raise MediaToolError("The original MKV segment title could not be read safely; no tags were changed.")


def _extract_mkv_tags(path, tool_paths=None, cancel_event=None):
    executable = find_tool("mkvextract", tool_paths)
    if not executable:
        raise MediaToolError("MKVExtract is required to preserve existing MKV tags.")
    with tempfile.TemporaryDirectory(prefix="mmo-mkv-tags-") as directory:
        output = Path(directory) / "original.xml"
        code, text, error = _run([executable, str(path), "tags", str(output)], cancel_event=cancel_event)
        if code not in (0, 1):
            raise MediaToolError(error.strip() or text.strip() or "Existing MKV tags could not be extracted.")
        if not output.exists():
            return None
        content = output.read_text(encoding="utf-8-sig")
        _parse_mkv_tags(content)
        return content


def _parse_mkv_tags(content):
    try:
        root = ET.fromstring(content) if content else ET.Element("Tags")
    except ET.ParseError as exc:
        raise MediaToolError("The existing MKV tag XML could not be preserved safely.") from exc
    if root.tag != "Tags":
        raise MediaToolError("The MKV tag XML has an unsupported root element.")
    return root


def _edited_mkv_tags(content, title, year=None, comment=None):
    root = _parse_mkv_tags(content)
    globals = []
    for tag in root.findall("Tag"):
        targets = tag.find("Targets")
        if targets is None or not any(targets.find(name) is not None for name in ("TrackUID", "EditionUID", "ChapterUID", "AttachmentUID")):
            globals.append(tag)
    if not globals:
        tag = ET.SubElement(root, "Tag")
        ET.SubElement(tag, "Targets")
        globals.append(tag)
    edits = {"TITLE": str(title)}
    if year is not None:
        edits["DATE_RELEASED"] = str(year)
    if comment is not None:
        edits["COMMENT"] = str(comment)
    for name, value in edits.items():
        found = []
        for tag in globals:
            for simple in list(tag.findall("Simple")):
                if (simple.findtext("Name") or "").upper() == name:
                    if name == "COMMENT" and not value:
                        tag.remove(simple)
                    else:
                        found.append(simple)
        if name == "COMMENT" and not value:
            continue
        if not found:
            simple = ET.SubElement(globals[0], "Simple")
            ET.SubElement(simple, "Name").text = name
            found.append(simple)
        for simple in found:
            for binary in list(simple.findall("Binary")):
                simple.remove(binary)
            string = simple.find("String")
            if string is None:
                string = ET.SubElement(simple, "String")
            string.text = value
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _write_mkv_tags_and_title(path, title, tags_xml, tool_paths=None, restore=False):
    executable = find_tool("mkvpropedit", tool_paths)
    if not executable:
        raise MediaToolError("MKVToolNix is required to edit the MKV title.")
    with tempfile.TemporaryDirectory(prefix="mmo-mkv-update-") as directory:
        action = ["--delete", "title"] if title is None else ["--set", "title=" + str(title)]
        command = [executable, str(path), "--edit", "info", *action]
        if tags_xml is not _NO_MKV_XML:
            if tags_xml is None:
                command.extend(["--tags", "all:"])
            else:
                xml_path = Path(directory) / "tags.xml"
                xml_path.write_text(tags_xml, encoding="utf-8")
                command.extend(["--tags", "all:" + str(xml_path)])
        code, output, error = _run(command, cancel_event=None)
        if code not in (0, 1):
            raise MediaToolError(error.strip() or output.strip() or "MKV tag update failed.")
        return output.strip() or error.strip() if code == 1 else ""


_NO_MKV_XML = object()


def _read_mp4_tags(path):
    from mutagen.mp4 import MP4
    media = MP4(path)
    return media, {key: list(media.tags[key]) if media.tags and key in media.tags else None for key in ("\xa9nam", "\xa9day")}


def update_tags(path, title, year=None, journal_dir=None, tool_paths=None, cancel_event=None, comment=None, file_manifest=None):
    """Edit reviewed tags; comment=None preserves it, '' removes it, text replaces it."""
    path = Path(path).resolve()
    if not title or not str(title).strip():
        raise ValueError("A title is required for an embedded-tag update.")
    if journal_dir is None:
        raise ValueError("A journal directory is required before changing embedded tags.")
    check_cancel(cancel_event)
    capability = validate_tag_update(path, year, comment, tool_paths)
    suffix = path.suffix.casefold()
    before = _snapshot(path)
    new_mkv_xml = _NO_MKV_XML
    if suffix == ".mkv":
        old = {"title": _read_mkv_segment_title(path)}
        if find_tool("mkvextract", tool_paths):
            old["tags_xml"] = _extract_mkv_tags(path, tool_paths, cancel_event)
            new_mkv_xml = _edited_mkv_tags(old["tags_xml"], str(title).strip(), year, comment)
    else:
        media, old = _read_mp4_tags(path)
        if comment is not None:
            old["\xa9cmt"] = list(media.tags["\xa9cmt"]) if media.tags and "\xa9cmt" in media.tags else None
    manifest = Path(journal_dir) / f"metadata-{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    record = {"version": 1, "kind": "metadata", "path": str(path), "created_at": datetime.now(timezone.utc).isoformat(),
              "status": "prepared", "undone": False, "before": before, "old_tags": old,
              "file_manifest": str(file_manifest) if file_manifest is not None else None,
              "new_tags": {"title": str(title).strip(), "year": str(year) if year is not None else None},
              "format": "mkv" if suffix == ".mkv" else "mp4",
              "note": "Undo restores targeted descriptive tags. It does not recreate the original binary layout."}
    if comment is not None:
        record["new_tags"]["comment"] = str(comment)
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
            warning = _write_mkv_tags_and_title(path, str(title).strip(), new_mkv_xml, tool_paths)
            if warning:
                record["warning"] = warning
        else:
            if media.tags is None:
                media.add_tags()
            media.tags["\xa9nam"] = [str(title).strip()]
            if year is not None:
                media.tags["\xa9day"] = [str(year)]
            if comment is not None:
                if str(comment):
                    media.tags["\xa9cmt"] = [str(comment)]
                else:
                    media.tags.pop("\xa9cmt", None)
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
        value = record["old_tags"].get("title")
        _write_mkv_tags_and_title(path, value, record["old_tags"].get("tags_xml", _NO_MKV_XML), tool_paths, restore=True)
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
    record.update(undone=True, status="undone", undone_at=datetime.now(timezone.utc).isoformat(), restored_snapshot=_snapshot(path))
    _atomic_json(manifest, record)
    return {"status": "undone", "manifest": str(manifest), "path": str(path)}
