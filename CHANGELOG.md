# Changelog

## 1.1.1 — 2026-11-03

### Fixed

- Fetch GMP source from GNU's download server while retaining the pinned checksum; reject invalid downloads before caching them and replace invalid cached archives.
- Update GitHub build actions to Node.js 24-compatible versions, removing the deprecated-runtime warning from Windows package uploads.

## 1.1.0 — 2026-11-02

- Added movie matching through TheTVDB v4, with a separate API key and optional subscriber PIN.
- Restored Quick, Verified and Custom transfer validation, including XXH3-128, BLAKE3 and SHA-256.
- Added Dry run enabled by default, optional movie NFO output and opt-in embedded title/year/comment edits for rename, Copy and Move.
- Linked metadata/NFO recovery to file-operation Undo while retaining independent tag restoration.
- Added subtitles stored in movie subtitle subfolders and separate external subtitle analysis/counts.
- Added read-only TMDB film-collection comparisons, distinguishing confirmed local movies, released titles not found, upcoming releases and unresolved local files, with JSON/CSV exports and refreshable caches.
- Bundled MediaInfo CLI 26.10, a controlled FFprobe 9.0.2 build and MKVToolNix 103.0 tools, with automatic discovery and configurable overrides.
- Added wrapping, scrollbars and full-value inspection for long properties and raw data.
- Added provider, validation, workflow and native-media regression checks, plus retained native source archives and build recipes.

## 1.0.0 — 2026-11-02

- Added batch movie matching from one folder or a nested folder tree using TMDB.
- Added reviewed rename-in-place operations that do not copy movie contents.
- Added Copy and Move organisation into individual `Title (Year)` movie folders.
- Added matching subtitle and existing local-poster handling, conflict checks and recovery journals.
- Added MediaInfo analysis for files and streams, collection summaries and JSON/CSV exports.
- Added separate, opt-in supported title/year tag correction and restoration of previous tag values.
- Added independent MMO settings, credential storage, branding and Windows packaging.

Source tests and packaged-app checks are documented separately from live-provider validation.
