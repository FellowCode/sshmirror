---
description: "Use when editing this repository. After any user-visible code, config, workflow, CLI, packaging, or documentation change, append a concise entry to .github/skills/version-control/assets/pending-changes.md. When the user asks to create a new version, fix the latest version, or generate changelog entries, use the version-control skill and clear applied pending items after updating CHANGELOG.md."
applyTo: "**"
---

# Version Tracking

- After each non-trivial repository change, append one concise bullet to `.github/skills/version-control/assets/pending-changes.md`.
- Use only these prefixes in the pending file: `Added:`, `Changed:`, `Fixed:`, `Removed:`, `Docs:`.
- Keep pending entries user-facing and release-note ready.
- When the user asks to create or update a version entry, use the `version-control` skill workflow.
- After updating `CHANGELOG.md`, remove only the pending bullets that were applied.