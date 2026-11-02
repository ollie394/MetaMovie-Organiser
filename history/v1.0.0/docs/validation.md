# Version 1.0.0 validation

Checked on 2 November 2026 with Windows 11 (64-bit) and Python 3.13.2.

All 92 offline tests pass: 41 movie-planning/file-operation tests, 34 media-analysis/tag-edit tests, and 17 state/credential/interface tests. Tests use temporary files, generated media and mocked provider responses. They do not read personal credentials or alter an existing media library.

Rename-in-place and its Undo are checked with copying and hashing helpers forbidden. Folder/file identities remain unchanged. The suite covers collision refusal, ambiguous matches, nested subtitles, paired subtitles, posters, multipart movies, case-only changes, cancellation, crash checkpoints, interrupted Undo, modified files and unknown folder contents.

MediaInfo is checked against generated MP4/MKV/WAV media. Filename claims such as 1080p or HDR do not override actual stream properties. Aggregate tests distinguish files from individual streams and check conservative HDR identification. Actual MP4 title/year edits and restoration preserve media payload bytes, codecs, duration and unrelated tags. Restoring tags is not a byte-identical container restoration or a full-file crash backup.

Real Tk interface tests cover preview, manual correction, approved rename and Undo, mode switching, destination conflicts, stale-plan invalidation, native analysis and separate embedded-edit consent. The footer was also checked at a 1000×720 window size. Documentation screenshots use a demonstration app with generated files.

The Windows application is built with PyInstaller 6.15.0. The bundled MediaInfo.dll was loaded independently and successfully read the generated AVC/AAC test video at its actual 320×180 dimensions. The packaged application opens with its icon and theme. Source and Windows archives include checksums; runtime settings, cached reports, logs and recovery records are excluded.

Live TMDB authentication and a real external MKVPropEdit installation were not tested. macOS/Linux GUI behaviour and clean-machine Windows installation remain separate checks. The executable is unsigned. See the release guide and distribution-licence notes before publishing.
