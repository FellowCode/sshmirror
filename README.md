# SSHMirror

Sync a working directory with a remote server over SSH, inspect diffs before applying changes, and keep a lightweight local history for rollback-oriented workflows.

SSHMirror is both:

- a Python library you can import;
- a CLI you can run inside a project folder;
- a sync workflow that keeps local and remote changes inspectable.

It can also be used as a lightweight sync workflow for a shared project: multiple developers can make changes to the same remote-backed codebase and inspect differences before pulling or pushing updates.

Internet access is not required for SSHMirror itself. It only needs network access to the target SSH host or Docker host you are synchronizing with.

When you run `sshmirror` without arguments, it opens an interactive menu.
Depending on project state, the menu can include:

- `Create sshmirror.config.yml`
- `Create sshmirror.ignore.txt`
- `Initialization`
- `Pull & Push`
- `Status`
- `View current changes`
- `View version changes`
- `Pull only`
- `Stash changes`
- `Restore stashed changes`
- `Force pull`
- `Discard all local changes`
- `Discard selected files`
- `Downgrade remote version`
- `Test connection`
- `Exit`

## Why It Stands Out

- Preview first. Inspect current file changes and version-to-version diffs before syncing. 🔍
- Built for real SSH workflows. Passwords, SSH keys, passphrases, and ssh-agent fallback are supported. 🔐
- Keeps local state. SSHMirror stores version and migration metadata under `.sshmirror/`. 🗂️
- Works as a library or a command-line tool. 🐍
- Useful for team workflows. Several developers can work on one project and sync changes through the same remote environment. 🤝
- Optional remote container restart after sync. 🐳

## Install 🚀

```bash
pip install sshmirror
```

For local development:

```bash
pip install -e .
```

## Quick Start ⚡

1. Create a config file:

   ```bash
   sshmirror
   ```

   On first interactive launch, SSHMirror can create `sshmirror.config.yml` for you.

2. Start from this minimal config:

   ```yaml
   host: '192.168.12.22'
   port: '50022'
   username: 'root'
   localdir: '.'
   remotedir: '/app'
   author: your-name
   ```

3. Check status or connect-test before syncing:

   ```bash
   sshmirror --status
   sshmirror --test-connection
   ```

## Example Configuration ⚙️

The full example lives in [sshmirror.config.example.yml](sshmirror.config.example.yml).

Supported auth patterns:

- password auth;
- private key auth;
- private key with passphrase;
- default SSH keys or ssh-agent when no key and no password are provided.

Example with optional container restart:

```yaml
host: '192.168.12.22'
port: '50022'
username: 'root'
localdir: '.'
remotedir: '/app'
author: your-name

restart_container:
   # Optional. If omitted, host/port/username are reused from the main SSH config.
   # host: '192.168.12.22'
   # port: '23322'
   # username: user
  sudo: true
  container_name: testcontainer
```

`restart_container` connects to the Docker host where the container is running. If `host`, `port`, or `username` are not specified there, SSHMirror uses the main connection values.

## CLI Commands 🧰

```bash
sshmirror --help
```

Main non-interactive flags:

- `--status` show local and remote sync status;
- `--current-diff` inspect current local versus remote differences;
- `--version-diff` inspect changes between remote versions;
- `--pull` only pull from remote;
- `--stash-changes` stash local changes before syncing;
- `--restore-stash` restore previously stashed changes;
- `--force-pull` overwrite local files from remote;
- `--discard` discard all local changes;
- `--discard-files <paths...>` discard only selected files;
- `--downgrade` downgrade remote version;
- `--test-connection` validate SSH access to the remote host and configured Docker host.

## Library Usage 📦

```python
from sshmirror import SSHMirror, SSHMirrorConfig

mirror = SSHMirror(
    config=SSHMirrorConfig(
        host='127.0.0.1',
        port=22,
        username='root',
        localdir='.',
        remotedir='/app',
    )
)
```

The public API is exported from [sshmirror/__init__.py](sshmirror/__init__.py).

## Project Layout

```text
sshmirror/
  sshmirror/          # importable package
  tests/              # smoke tests
  pyproject.toml      # packaging metadata
  sshmirror.config.example.yml
```

## Development 🛠️

Run tests:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Build locally:

```bash
python -m build
```

## Status

This repository is structured as a Python package with a CLI entry point. If you plan to publish to PyPI, keep package metadata, README, and license in sync with actual behavior.