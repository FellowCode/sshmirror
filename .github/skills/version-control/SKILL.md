---
name: version-control
description: 'Use when the user asks to create a new version, bump a release, fix the latest version, update the changelog from recorded edits, or manage version notes. Reads pending changes from the tracking file, writes changelog entries, and clears applied items after release notes are recorded.'
argument-hint: 'Version task, for example: create 0.1.20 from pending changes'
user-invocable: true
---

# Version Control

This skill manages release notes from a tracked pending-changes file.

## Files

- Pending change log: [pending-changes.md](./assets/pending-changes.md)

## When To Use

- The user says `зафиксируй новую версию`
- The user says `создай версию`
- The user says `внеси изменения в последнюю версию`
- The user asks to build or update `CHANGELOG.md` from previously tracked edits
- The user asks what changed since the last version

## Pending Change Format

Each tracked item must use one of these prefixes:

- `Added:`
- `Changed:`
- `Fixed:`
- `Removed:`
- `Docs:`

Example:

```markdown
- Fixed: Restored the restart_container prompt after successful push.
- Added: Added restart_container.local for restarting a local Docker container.
```

## Procedure For Release Requests

1. Read [pending-changes.md](./assets/pending-changes.md).
2. Group items by changelog section:
   - `Added:` -> `### Added`
   - `Changed:` -> `### Changed`
   - `Fixed:` -> `### Fixed`
   - `Removed:` -> `### Removed`
   - `Docs:` -> include under `### Changed` unless the user explicitly wants documentation isolated.
3. Update the requested version entry in `CHANGELOG.md`.
4. Preserve existing release entries and append or insert only the needed sections.
5. After the changelog is successfully updated, remove the applied items from [pending-changes.md](./assets/pending-changes.md).
6. Leave any unapplied or ambiguous items in the tracking file.

## Procedure For Updating The Latest Version

1. Read [pending-changes.md](./assets/pending-changes.md).
2. Read the latest version section in `CHANGELOG.md`.
3. Merge pending items into that latest version section without rewriting older releases.
4. Clear only the items that were added to the changelog.

## Safety Rules

- Do not invent changelog entries that are not present in [pending-changes.md](./assets/pending-changes.md) unless the user explicitly asks for editorial cleanup.
- Do not clear the tracking file before `CHANGELOG.md` is updated successfully.
- Keep release wording concise and user-facing.
- If the pending file is empty, report that there is nothing to release instead of inventing entries.