# 0012. PySide6 (LGPL-3.0) is an allowed exception to the license list

Date: 2026-10-21. Status: accepted. Tracked in issue #115.

## Context

New dependencies need a permissive license (MIT, BSD, Apache-2.0 and the rest of the list in CONTRIBUTING.md). PySide6, Shiboken6 and the Qt DLLs they load are LGPL-3.0. The UI runs on them until the Zig-native UI replaces Qt (decision 0011).

The LGPL allows shipping a library inside an MIT program when the user can replace that library and gets its license and source.

## Decision

- PySide6, Shiboken6 and Qt stay, under the LGPL-3.0.
- The pin is the version range in `pyproject.toml` (`PySide6>=6.7`), with the exact version in `uv.lock`. The exception covers every LGPL-3.0 release of PySide6 6.x.
- The release zip is a PyInstaller onedir build. Qt ships as separate, unmodified DLLs in `_internal/PySide6`, so a user can swap in their own build.
- The zip carries `LICENSE`, `NOTICE` and `licenses/` (LGPL-3.0, GPL-3.0, OFL-1.1, PSF-2.0) at its root. `NOTICE` names each part, its license and where to get the Qt source. `release.yml` fails the build when a file is missing.

## Consequences

- No onefile build and no static Qt link: either one breaks the replace-the-library condition.
- No Qt module under a GPL-only license (for example Qt Charts or Qt Data Visualization) may be imported.
- The exception ends when the Qt UI is gone (decision 0011).
