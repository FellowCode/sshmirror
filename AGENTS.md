# AGENTS.md

## Scope

These instructions apply to the whole repository.

## Project Intent

- `sshmirror` is a Python library with a CLI entry point.
- Preserve both use cases: importable public API and command-line workflow.
- Keep documentation aligned with real CLI flags and config keys.

## Packaging Rules

- Keep packaging centered on `pyproject.toml`.
- Do not commit generated artifacts such as `__pycache__/`, `dist/`, `build/`, or `*.egg-info/`.
- Prefer minimal metadata changes and keep the package name, version, and exported API consistent.

## Code Change Rules

- Preserve the public exports in `sshmirror/__init__.py` unless a breaking change is explicitly requested.
- Keep the sample config in `sshmirror.config.example.yml` synchronized with supported config fields.
- If CLI options change, update both the README and tests that validate the CLI help surface.
- Prefer focused fixes over broad refactors.

## Verification

- After packaging or CLI changes, validate with editable install when possible.
- Run the smoke tests after changing public API, packaging, or CLI behavior.