# Native tool source inventory

The packaging workflow retains native-tool licences and source material with the Windows distribution. The small MMO source-upload archive excludes downloaded executables and the large native source archives; the preparation scripts and [pinned source manifest](../packaging/native-sources.json) describe how the source material is fetched and checked.

## MKVToolNix 103.0

The CLI tools come from the publisher's [64-bit Windows 103.0 portable archive](https://mkvtoolnix.download/windows/releases/103.0/). The publisher supplies [SHA-256 checksums](https://mkvtoolnix.download/windows/releases/103.0/sha256sums.txt). Its `mkvpropedit` and `mkvextract` executables import Windows system DLLs and statically include other libraries. Excluding the GUI does not remove the QtCore dependency.

The source kit includes the [original MKVToolNix 103.0 source](https://mkvtoolnix.download/sources/mkvtoolnix-103.0.tar.xz), Qtbase source, every dependency archive listed below, and the publisher's pinned MXE build recipes. The MKV source archive contains `COPYING`, `README.md`, `Building.for.Windows.md`, the Windows setup script, and bundled source for libEBML, libMatroska, fmt, nlohmann JSON, pugixml, utf8-cpp, avilib and librmff. Original copyright and licence files remain in their source archives. The portable package's `doc/COPYING.txt` and `doc/licenses/` contain its distributed notices.

| Source archive | Version |
| --- | --- |
| Qtbase | 6.12.0 |
| Boost | 1.92.0 |
| FLAC | 1.5.0 |
| GNU gettext | 1.0 |
| GMP | 6.3.0 |
| GNU libiconv | 1.19 |
| libOgg | 1.3.6 |
| libVorbis | 1.3.7 |
| zlib | 1.3.2 |
| PCRE2 | 10.49 |
| Zstandard | 1.5.7 |
| cmark | 0.31.2 |
| libdvdread | 6.1.3 |
| libdvdcss | 1.4.3 |
| bzip2 | 1.0.8 |
| Expat | 2.9.0 |
| MXE recipes | `ba36dff2e8c4ca28d607f25e56813daf5f47537b` |

These dependency and recipe archives total approximately **270 MiB**. The original MKVToolNix source archive adds approximately 11 MiB. The manifest records individual URLs, hashes and download sizes.

## Verified versions and inference

The official 103.0 binaries contain identifiable strings for Qt **6.12.0**, GCC **16.2.0** and zlib **1.3.2**. `mkvextract` also identifies Vorbis **1.3.7**. An [adjacent official Windows build log](https://buildbot.mkvtoolnix.download/api/v2/logs/121103/contents), dated 17 October 2026, reports the same Qt and GCC versions. That development-build log is not an attestation of the release build.

The selected MXE commit is the latest commit in the publisher's fork dated before the Windows release archive. Its Qt, zlib and Vorbis recipe versions agree with the binary evidence. Selection by chronology is an **inference**: the publisher did not name this commit as the release's exact build recipe. The other versions in the table are recipe pins, rather than independently verified versions of every library linked into the release executables.

Dependency hashes were copied from that immutable recipe snapshot. The MXE archive hash was computed from the publisher's immutable-commit archive. Download-time verification confirms the retrieved files match these recorded archives; it does not establish the unreported release-build dependency versions. This inventory records the available source material and provenance without certifying the complete release-build correspondence.

## Other native tools

MediaInfo CLI is obtained from MediaArea's official Windows download. Preserve the supplied licence and copyright notices. No publisher checksum for the 26.10 CLI ZIP was located; its recorded SHA-256 identifies the archive downloaded over the official HTTPS link.

FFprobe is built from the selected FFmpeg source using the recorded local build recipe. Its licence, original source, configuration and build instructions belong in the native source package. A third-party FFmpeg binary's source link alone does not identify all of that build's optional library sources.
