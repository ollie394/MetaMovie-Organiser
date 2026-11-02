# Contributing

Use Python 3.11 or newer. Windows is the primary desktop and packaging target.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe mmo.py
```

## Project layout

| Path | Purpose |
| --- | --- |
| `mmo.py` | Desktop workflow and application entry point |
| `mmo_core.py` | Movie matching, batch planning and reviewed file operations |
| `media_tools.py` | MediaInfo analysis, exports and supported embedded-tag editing |
| `tvdb_provider.py` | TheTVDB v4 movie matching and session handling |
| `collections_tools.py` | Read-only TMDB collection membership and library comparisons |
| `workflow.py` | Dry run, NFO output and linked file/tag recovery |
| `support.py` | MMO state, credentials and shared application services |
| `ui_theme.py` | Desktop colours, typography and widget helpers |
| `assets/` | MMO branding and TMDB attribution logo |
| `tests/` | Offline tests using disposable files and metadata fixtures |
| `packaging/` | Windows executable metadata and PyInstaller specification |
| `scripts/` | Windows build and clean source-archive tools |
| `packaging/native-sources.json` | Pinned native dependency source inventory and provenance |

## File operations and embedded metadata

Preserve the distinction between in-place renames, file transfers and embedded tag edits. A rename-in-place must not silently fall back to copying file contents. Keep associated files in the reviewed plan and journal each completed step so partial runs can be recovered.

For metadata changes, test both edits and restoration of prior supported values. Restoration is semantic: container tools may change byte layout. Do not describe it as a byte-exact undo.

Use temporary folders and disposable files for tests. Set `MMO_STATE_DIR` to isolate file-based settings, logs and journals. Replace the keyring backend with a test double when exercising credentials; the state-directory override does not redirect the operating system keyring.

## Analysis and providers

Keep separate audio and subtitle streams separate in reports, and external subtitle records separate from embedded streams. Preserve missing values rather than substituting guesses. HDR classifications need explicit supporting fields. Construct or record provider responses without keys, personal paths or account data. Automated tests should not need a TMDB/TVDB account or contact a provider.

## Interface and pull requests

Check the main tabs and dialogs at common Windows display scales and the minimum window size. Make long paths and file-operation consequences visible during review. Include a screenshot when it helps explain a visible change.

Describe the concrete problem, resulting behaviour and validation in a pull request. Bug reports should include the MMO version, Windows version, minimal reproduction steps and a few representative filenames. Remove credentials and personal paths from any shared logs or reports.
