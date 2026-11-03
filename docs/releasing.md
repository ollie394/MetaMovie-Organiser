# Preparing a release

## Check the source

1. Keep the application version, README, changelog and `packaging/version_info.txt` consistent.
2. Install `requirements-dev.txt` in a clean Python environment and run `python -m pytest -q`.
3. Check the five GUI tabs using `MMO_STATE_DIR` and disposable media. Exercise Preview names, Dry run, approved rename-in-place, Copy, Move, cancellation and file-operation Undo.
4. Check subtitles, local posters, duplicate titles, ambiguous matches and occupied destinations. A no-copy rename must remain a filesystem rename.
5. Check MediaInfo on representative disposable files, including files with multiple streams and missing fields. Confirm the JSON/CSV outputs distinguish files from tracks.
6. Check NFO output and supported embedded title/year/comment edits in all three operations, then exercise their linked Undo. Check standalone tag restoration too. Run an MKV check using the bundled `mkvpropedit`/`mkvextract`; do not infer that a source-only unit test validates a packaged executable.

Live TMDB/TVDB validation is a separate manual check with an authorised account. If it was not performed, state that in the release notes.

## Build and inspect Windows output

Run `scripts/build-windows.ps1`. It runs the tests, prepares the pinned native tools/source kit, creates an allowlisted application source archive, packages the app, records dependency versions and creates ZIPs with SHA-256 checksums under `dist/1.1.1`. Use `-DistPath` to keep another version's running package untouched.

Run the packaged executable on a clean Windows account or computer without a separate Python installation. Confirm the icon, GUI, MediaInfo analysis, BLAKE3 transfers, credential backend and portable vault work. Inspect the output for bundled `pymediainfo/MediaInfo.dll` and dependency licence material. Source tests do not replace the packaged-app check.

The package contains the application source archive, Mutagen's Python source and dependency licence information. Native media tools are bundled with their notices. The separate `MetaMovie-Organiser-NativeToolSources.zip` retains native source/build materials; publish it from the same release location as the Windows ZIP. Review [the source provenance](native-tool-sources.md) and retain its stated inference limits. The executable is not code-signed by this build.

## Prepare the GitHub upload

Run `python scripts/package_source.py` after the source and screenshots are final. It creates `dist/MetaMovie-Organiser-Source.zip` and its checksum using an explicit list of source files and project directories. Runtime state, personal settings, reports, caches, virtual environments and build outputs are excluded.

Extract the archive to prepare the initial Git commit. Its top-level `MetaMovie-Organiser` folder includes GitHub workflows. Review the selected contents before uploading them; do not commit the generated ZIP itself.

## Publish

GitHub checks run offline tests on Windows and source-syntax checks on Ubuntu. The Windows package workflow runs manually or on a version tag beginning with `v`. It uploads a build artifact for review and does not publish a GitHub release.

After reviewing the source and Windows archive, publish the matching version, attach the Windows/source/native-source ZIPs and their checksums, and describe the platforms and features actually checked. Keep any unperformed live-provider or packaged-tool checks explicit.
