# Preparing a release

## Check the source

1. Keep the application version, README, changelog and `packaging/version_info.txt` consistent.
2. Install `requirements-dev.txt` in a clean Python environment and run `python -m pytest -q`.
3. Check the GUI using `MMO_STATE_DIR` and disposable media. Exercise preview, approved rename-in-place, Copy, Move, cancellation and file-operation Undo.
4. Check subtitles, local posters, duplicate titles, ambiguous matches and occupied destinations. A no-copy rename must remain a filesystem rename.
5. Check MediaInfo on representative disposable files, including files with multiple streams and missing fields. Confirm the JSON/CSV outputs distinguish files from tracks.
6. Check supported embedded-tag edits separately and restore previous values through the metadata journal. Configure `mkvpropedit` for an MKV check; do not infer that source tests validate an installed external tool.

Live TMDB validation is a separate manual check with an authorised account. If it was not performed, state that in the release notes.

## Build and inspect Windows output

Run `scripts/build-windows.ps1`. It runs the tests, creates an allowlisted source archive, packages the app, records dependency versions and creates a Windows ZIP with a SHA-256 checksum.

Run the packaged executable on a clean Windows account or computer without a separate Python installation. Confirm the icon, GUI, MediaInfo analysis, BLAKE3 transfers, credential backend and portable vault work. Inspect the output for bundled `pymediainfo/MediaInfo.dll` and dependency licence material. Source tests do not replace the packaged-app check.

The package contains the application source archive, Mutagen's Python source and dependency licence information. Review these materials together when distributing the executable. Optional external tools are not bundled, and the executable is not code-signed by this build.

## Prepare the GitHub upload

Run `python scripts/package_source.py` after the source and screenshots are final. It creates `dist/MetaMovie-Organiser-Source.zip` and its checksum using an explicit list of source files and project directories. Runtime state, personal settings, reports, caches, virtual environments and build outputs are excluded.

Extract the archive to prepare the initial Git commit. Its top-level `MetaMovie-Organiser` folder includes GitHub workflows. Review the selected contents before uploading them; do not commit the generated ZIP itself.

## Publish

GitHub checks run offline tests on Windows and source-syntax checks on Ubuntu. The Windows package workflow runs manually or on a version tag beginning with `v`. It uploads a build artifact for review and does not publish a GitHub release.

After reviewing the source and Windows archive, publish the matching version, attach the ZIP and checksum, and describe the platforms and features actually checked. Keep any unperformed live-provider or external-tool checks explicit.
