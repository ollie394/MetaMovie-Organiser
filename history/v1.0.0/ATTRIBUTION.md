# Acknowledgements and third-party components

This product uses the TMDB API but is not endorsed or certified by TMDB. Movie metadata and the TMDB logo are provided by [TMDB](https://www.themoviedb.org/). TMDB's name, logo and metadata retain their respective owners' rights.

[MediaInfo](https://mediaarea.net/en/MediaInfo) by MediaArea supplies technical media information through [pymediainfo](https://pymediainfo.readthedocs.io/). The Windows package includes the library from the pymediainfo wheel and its `License.html`.

[Mutagen](https://mutagen.readthedocs.io/) reads and writes supported container tags. Its installed distribution declares GPL-2.0-or-later. The Windows package includes its full `COPYING` licence and Python source, as well as MMO's source and build scripts, under `third_party/`. See [DISTRIBUTION-LICENCE.md](DISTRIBUTION-LICENCE.md) for the combined distribution's licence notice.

Requests, BLAKE3, keyring, cryptography and Pillow retain their own licences. Their distribution licence metadata is copied into the Windows package. `DEPENDENCIES.txt` records the versions used for that build.

Optional external MediaInfo CLI, FFprobe and MKVToolNix tools are not distributed with this project. Their licences and installation requirements apply separately.

The application's [MIT licence](LICENSE) covers its source. It does not replace the licences for third-party software, logos, trademarks or metadata.
