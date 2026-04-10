# Pending Changes

Record every user-visible repository change here until it is moved into CHANGELOG.md.

Format:

- Added: User-visible new capability
- Changed: Behavior or workflow change
- Fixed: Bug fix or regression fix
- Removed: Removed capability or compatibility
- Docs: Documentation-only update

Rules:

- Keep one change per bullet.
- Prefer short user-facing wording.
- When a version is finalized, move applied items into CHANGELOG.md and delete them from this file.

Pending items:
- Changed: Updated the release-preparation workflow to optionally commit, tag, and push a confirmed release to git for PyPI publishing.
- Changed: Displayed the current sshmirror version unobtrusively when starting the interactive CLI.
- Fixed: Stopped Ctrl+C in the interactive menu from surfacing a UserAbort error message.
- Changed: Reworked the Status view into a staged overview with structured local, remote, and live-diff panels.
