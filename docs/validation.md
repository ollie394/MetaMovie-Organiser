# Validation for 1.1.0

The full automated suite passed on Windows with Python 3.13: **233 tests passed in 14.18 seconds** on 2 November 2026.

Checks use disposable media and isolated MMO state. They cover movie parsing/matching, no-copy filesystem renames, reviewed transfers and verification modes, subtitles, conflicts, cancellation, file recovery, NFO generation, linked tag recovery, supported MP4/MKV title/year/comment changes, MediaInfo analysis and exports, credentials and real Tk workflows. Collection checks cover confirmed-owned versus not-found and upcoming films, duplicate versions, safe local NFO identifiers, ambiguous matches, provider failures, caching/refresh and read-only GUI exports. Provider responses are mocked; no real provider credentials or live account access were used.

Prepared native executables were version-checked: MediaInfo CLI 26.10, controlled FFprobe 9.0.2 and MKVToolNix 103.0 (`mkvpropedit` and `mkvextract`). PE imports require Windows system libraries; MediaInfo's companion `LIBCURL.DLL` is retained. The FFprobe build uses unmodified FFmpeg source with optional external libraries disabled. Native source archive hashes were checked against the recorded manifest; see [the provenance inventory](native-tool-sources.md) for the explicit limits of the publisher's MKV recipe correspondence.

The final packaged-executable launch check is separate from source tests and is recorded after the Windows build. These checks do not establish that every media file is supported, that a live TMDB/TVDB account works, or that the executable is code-signed.
