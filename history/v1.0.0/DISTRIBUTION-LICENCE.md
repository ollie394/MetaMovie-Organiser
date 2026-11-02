# Application source and Windows distribution

The original MetaMovie Organiser source files are available under the MIT licence in `LICENSE`.

The combined Windows distribution includes Mutagen, which is licensed under GPL-2.0-or-later. Its GPL terms apply when distributing the combined application; the MIT licence on MMO's own source does not replace those terms. Other bundled components also retain their own licences. The Windows package is not offered as an MIT-only binary.

The package carries the relevant materials alongside the executable:

- `third_party/licenses/mutagen/COPYING`: the GNU General Public License supplied by Mutagen.
- `third_party/source/mutagen-<version>/`: the installed Mutagen package's complete Python source files.
- `third_party/source/MetaMovie-Organiser/`: MMO's source, tests, build specification and packaging scripts.
- `third_party/licenses/`: retained licence files for bundled dependencies, including MediaInfo's `License.html`.
- `DEPENDENCIES.txt`: the Python and dependency versions used for this build.

The application source archive is also included in `source/`. Preserve these materials when redistributing the Windows package. See Mutagen's `COPYING` for the full licence conditions and warranty disclaimer.
