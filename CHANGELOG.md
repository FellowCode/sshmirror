# Changelog

## 0.1.12

### Fixed

- Fixed `Ctrl+C` handling in interactive menus so cancellation exits cleanly instead of falling back to plain input.
- Fixed prompt cancellation handling for both `questionary` prompts and plain-input fallback paths.

## 0.1.11

### Changed

- Finalized interactive diff browsing so version pages open with the newest item on the current page selected by default.
- Restricted textual diff inspection to inspectable changed files only.

### Fixed

- Prevented oversized changed files from being selectable in interactive diff views.

## 0.1.10

### Changed

- Reworked `View version changes` for large version histories.
- Replaced full-history rendering with interactive paginated browsing.
- Improved version timestamp presentation in interactive history views.
- Added colored tabular rendering and numeric shortcuts for version selection.

### Added

- Added lazy remote paging for interactive version history browsing.
- Added author display in interactive version history views.
- Added a 50-character validation limit for version descriptions.

## 0.1.9

### Fixed

- Fixed sudo password authentication for `restart_container`.

### Added

- Added step-by-step Docker host diagnostics for Docker host checks.

## 0.1.8

### Fixed

- Fixed `restart_container` connection handling.

### Changed

- Removed deprecated `restart_container.user` support and standardized on `restart_container.username`.

### Added

- Added startup validation for the full configuration, including `restart_container` consistency checks.

## 0.1.7

### Changed

- Optimized local and remote scanning so ignored directories are pruned instead of scanned and filtered after traversal.

## 0.1.6

### Fixed

- Fixed postponed annotation evaluation and runtime annotation handling.