# Changelog

## 0.1.20

### Changed

- Reworked downgrade to match the push and pull preview flow with clearer confirmation, live status updates, and post-downgrade sync.
- Updated View current changes to use the same file-change list format and selection flow as version comparison.
- Renamed the interactive Pull & Push menu action to Sync local and remote for clearer CLI wording.
- Grouped discard actions into a Discard submenu and marked submenu items with `...` in the interactive CLI.
- Added a live progress indicator during full local/remote project scans so long verification steps no longer look stuck.

### Fixed

- Cleared the preview table before showing live push and pull progress so sync runs no longer leave duplicate tables in the terminal.
- Kept the entered push version description visible in the terminal after confirmation instead of clearing it with the preview screen.

## 0.1.19

### Fixed

- Restored the `restart_container` prompt after successful `push`, including fast-path syncs where the full post-push compare is skipped.

### Added

- Added `restart_container.local: true` to restart a Docker container on the same machine where `sshmirror` is running, without an extra SSH Docker host connection.

## 0.1.18

### Fixed

- Fixed a missing `uuid` import in the remote push path which caused `NameError` during metadata file creation.

## 0.1.17

### Fixed

- Moved `push` and `pull` confirmation prompts ahead of the live sync table so interactive confirmation and version description input work correctly.

## 0.1.16

### Changed

- Replaced the interactive `View version changes` menu item with a `Versions` submenu containing `History` and `Compare`.
- Added automatic previous-version diff browsing for `Versions -> History`.
- Reworked `push` and `pull` execution so the existing preview table stays on screen and the `Status` column updates live during synchronization.
- Made version descriptions mandatory during `push` instead of silently defaulting to `update`.

### Fixed

- Removed duplicate visible CLI actions for force-discarding local changes, leaving a single user-facing discard action.
- Skipped the expensive full project re-compare after a normal `push` when no sync hook commands are configured.
- Cleared the temporary `Connect to remote...` line before interactive version lists are shown.
- Adjusted version pagination controls so `Older versions` appears before the version list and `Newer versions` appears after it.
- Made remote `push` synchronization atomic by rolling back remote changes when any file sync step fails.
- Switched generated downgrade scripts to relative paths so rollback metadata no longer embeds absolute remote project paths.

## 0.1.15

### Changed

- Improved interactive version history rendering with fixed-width columns for version number, timestamp, author, uid, and message.
- Improved file change browsing so `changed`, `created`, and `deleted` entries are shown together with clearer color-coded styling.
- Improved `push` and `pull` previews with structured summary panels and color-coded action tables before confirmation.

## 0.1.14

### Changed

- Highlighted the selected base version during target version selection so the comparison source stays visible in interactive history browsing.

### Fixed

- Removed duplicated version history rendering so interactive diff browsing shows a single styled selection list instead of both a table and a separate prompt list.

## 0.1.13

### Fixed

- Fixed interactive `questionary` prompts inside async CLI flows so they no longer fail with `asyncio.run() cannot be called from a running event loop`.
- Fixed version and diff browsing prompts to run safely while the CLI event loop is active.

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