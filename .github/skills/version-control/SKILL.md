---
name: version-control
description: 'Use when the user asks to create a new version, bump a release, prepare a release, fix the latest version, update the changelog from recorded edits, or manage version notes. Reads pending changes from the tracking file, writes changelog entries, clears applied items after release notes are recorded, prepares a short git commit summary for the release, and can optionally publish the release to git with a version tag after confirmation.'
argument-hint: 'Version task, for example: prepare release 0.1.20 from pending changes'
user-invocable: true
---

# Version Control

This skill manages release notes from a tracked pending-changes file.

It must only preserve and release changes to the sshmirror library and CLI behavior.
Do not include agent, skill, instruction, README, or other repository-maintenance changes in release notes unless the user explicitly overrides this rule.

## Files

- Pending change log: [pending-changes.md](./assets/pending-changes.md)

## When To Use

- The user says `зафиксируй новую версию`
- The user says `создай версию`
- The user says `подготовь релиз`
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
2. Filter the pending items down to sshmirror library and CLI changes only.
   - Ignore agent, skill, instruction, README, changelog-process, or other repository-maintenance entries.
   - Ignore documentation-only entries unless they document a library change the user explicitly wants in the release.
3. Group the remaining items by changelog section:
   - `Added:` -> `### Added`
   - `Changed:` -> `### Changed`
   - `Fixed:` -> `### Fixed`
   - `Removed:` -> `### Removed`
   - `Docs:` -> include under `### Changed` unless the user explicitly wants documentation isolated.
4. Update the requested version entry in `CHANGELOG.md`.
5. Update all version metadata files consistently, including `pyproject.toml`, `sshmirror/__init__.py`, and `sshmirror/_version.py` when present.
6. Preserve existing release entries and append or insert only the needed sections.
7. Prepare a short git commit summary for the release based on the same applied changes.
   - Required format: `{version} {description}`
8. When the user asks to `подготовь релиз`, ask whether the new version should also be uploaded to git.
   - Ask this only after the release notes and version files are updated successfully.
   - If the answer is positive, perform the git release flow yourself:
     - create a normal git commit for the release files using the prepared commit summary
     - create a version tag in the form `v{version}`
     - push the branch commit and the new tag so the existing automation can publish the release to PyPI
   - If the answer is negative, do not run any git commands.
9. After the changelog is successfully updated, remove the applied items from [pending-changes.md](./assets/pending-changes.md).
10. Leave any unapplied or ambiguous items in the tracking file.

## Procedure For Updating The Latest Version

1. Read [pending-changes.md](./assets/pending-changes.md).
2. Filter the pending items down to sshmirror library and CLI changes only.
3. Read the latest version section in `CHANGELOG.md`.
4. Merge pending items into that latest version section without rewriting older releases.
5. Update all version metadata files consistently, including `pyproject.toml`, `sshmirror/__init__.py`, and `sshmirror/_version.py` when present.
6. Prepare a short git commit summary that matches the updated release notes.
   - Required format: `{version} {description}`
7. Clear only the items that were added to the changelog.

## Output Requirements For Release Preparation

- When the user asks to `подготовь релиз`, include both:
   - the changelog-ready release notes
   - a short git commit summary in one concise sentence or phrase
- For `подготовь релиз`, also ask whether the agent should upload the release to git.
- If the user approves git upload, complete the release commit, create the `v{version}` tag, and push both the commit and tag.
- If the user declines git upload, stop after preparing files and reporting the release notes plus commit summary.
- The git commit summary must use this exact shape: `{version} {description}`.
- Example: `0.1.21 restore push and pull confirmation prompts`.
- Present the git commit summary as copyable code text, not as a shell command.
- Do not prefix it with `git commit -m` or any CLI wrapper.
- Keep the git commit summary shorter than the changelog section and focused on the main user-visible outcome.
- Do not invent technical details that are not reflected in the applied pending changes.

## Safety Rules

- Do not invent changelog entries that are not present in [pending-changes.md](./assets/pending-changes.md) unless the user explicitly asks for editorial cleanup.
- Do not include non-library repository maintenance in releases by default: no agent, skill, instruction, README, or similar housekeeping entries.
- Do not clear the tracking file before `CHANGELOG.md` is updated successfully.
- Keep release wording concise and user-facing.
- Keep the git commit summary concise, aligned with the release notes, and in `{version} {description}` format.
- Never push a release commit or tag without explicitly asking the user first during `подготовь релиз`.
- If the pending file is empty, report that there is nothing to release instead of inventing entries.