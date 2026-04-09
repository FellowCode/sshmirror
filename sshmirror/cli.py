import argparse
import asyncio
import os
import signal
import sys

try:
    from .config import SSHMirrorCallbacks, SSHMirrorConfig
    from .prompts import prompt_choice, prompt_confirm, prompt_discard_files, prompt_text
    from .sshmirror import SSHMirror, STASH_METADATA_FILE, console
    from .core.exceptions import UserAbort
    from .core.utils import read_text_file
except ImportError:
    from config import SSHMirrorCallbacks, SSHMirrorConfig
    from prompts import prompt_choice, prompt_confirm, prompt_discard_files, prompt_text
    from sshmirror import SSHMirror, STASH_METADATA_FILE, console
    from core.exceptions import UserAbort
    from core.utils import read_text_file


DEFAULT_CONFIG_TEMPLATE = """# SSH host or IP address used for the main sync connection.
host: '127.0.0.1'

# SSH port for the main sync connection.
port: '22'

# SSH username for the main sync connection.
username: 'root'

# Optional. If omitted together with password, asyncssh will try the user's default SSH keys / ssh-agent.
# private_key: '~/.ssh/id_ed25519'

# Optional. Passphrase for the private key above.
# private_key_passphrase: 'KeyPassphrase'

# Optional. Password-based SSH authentication.
# password: 'password'

# Local project directory that will be synchronized.
localdir: '.'

# Remote project directory that will be synchronized.
remotedir: '/app'

# Optional author label stored in generated sync versions.
author: user

# Optional. If set, SSHMirror can restart a container after sync.
# This is useful when the remote directory is mounted into a container and you want changes applied immediately.
restart_container:
    # Optional. If omitted, values from the main connection are reused.
    # host: '127.0.0.1'
    # port: '22'
    # user: root

    # Same auth rules as the main connection. If omitted here, values from the main
    # connection are reused, including the default user key fallback.
    # private_key: '~/.ssh/id_ed25519'
    # private_key_passphrase: 'KeyPassphrase'
    # password: 'password'

    # Optional. Run docker commands with sudo on the Docker host.
    sudo: true

    # Optional. Needed only when sudo requires a password and SSH auth uses a key.
    # sudo_password: 'password'

    # Docker container name that should be restarted after sync.
    container_name: testcontainer
"""


def _is_sshmirror_initialized() -> bool:
    if os.path.isdir(SSHMirror.versions_directory):
        return any(os.scandir(SSHMirror.versions_directory))

    return False


def _find_default_cli_path(path: str) -> str | None:
    for candidate in (path, os.path.join('.sshmirror', path)):
        if os.path.exists(candidate):
            return candidate
    return None


def _has_stashed_changes() -> bool:
    return os.path.exists(STASH_METADATA_FILE)


def _create_default_config() -> bool:
    config_example_path = 'sshmirror.config.example.yml'
    target_path = 'sshmirror.config.yml'

    if os.path.exists(target_path):
        console.print(f'{target_path} already exists', style='yellow')
        return False

    if os.path.exists(config_example_path):
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(read_text_file(config_example_path))
    else:
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(DEFAULT_CONFIG_TEMPLATE)

    console.print(f'Created {target_path}', style='green')
    return True


def _create_default_ignore() -> None:
    target_path = 'sshmirror.ignore.txt'
    if os.path.exists(target_path):
        console.print(f'{target_path} already exists', style='yellow')
        return

    with open(target_path, 'w', encoding='utf-8') as f:
        f.write('# One path or pattern per line\n')

    console.print(f'Created {target_path}', style='green')


def _menu_item(label: str, action: str) -> tuple[str, str]:
    return label, action


def _build_interactive_menu_items(
    *,
    has_config: bool,
    has_ignore: bool,
    initialized: bool,
    has_stash: bool,
) -> list[tuple[str, str]]:
    if not has_config:
        menu_items: list[tuple[str, str]] = []
    elif initialized:
        menu_items = [
            _menu_item('Pull & Push', 'Pull & Push'),
            _menu_item('Status', 'Status'),
            _menu_item('View current changes', 'View current changes'),
            _menu_item('View version changes', 'View version changes'),
            _menu_item('Pull only', 'Pull only'),
            _menu_item('Restore stashed changes', 'Restore stashed changes') if has_stash else _menu_item('Stash changes', 'Stash changes'),
            _menu_item('Force pull', 'Force pull'),
            _menu_item('Discard all local changes', 'Discard all local changes'),
            _menu_item('Discard selected files', 'Discard selected files'),
            _menu_item('Downgrade remote version', 'Downgrade remote version'),
            _menu_item('Test connection', 'Test connection'),
        ]
    else:
        menu_items = [
            _menu_item('Initialization', 'Initialization'),
            _menu_item('Status', 'Status'),
            _menu_item('View current changes', 'View current changes'),
            _menu_item('Restore stashed changes', 'Restore stashed changes') if has_stash else None,
            _menu_item('Test connection', 'Test connection'),
        ]
        menu_items = [item for item in menu_items if item is not None]

    if not has_config:
        menu_items.insert(0, _menu_item('Create sshmirror.config.yml', 'Create sshmirror.config.yml'))
    if not has_ignore:
        insert_at = 1 if not has_config else 0
        menu_items.insert(insert_at, _menu_item('Create sshmirror.ignore.txt', 'Create sshmirror.ignore.txt'))

    menu_items.append(_menu_item('Exit', 'Exit'))
    return menu_items


def _configure_interactive_args(args: argparse.Namespace) -> argparse.Namespace:
    args.exit_requested = False
    while True:
        has_config = _find_default_cli_path('sshmirror.config.yml') is not None
        has_ignore = _find_default_cli_path('sshmirror.ignore.txt') is not None
        initialized = _is_sshmirror_initialized()
        has_stash = _has_stashed_changes()

        if has_stash:
            console.print('Reminder: stashed changes are waiting to be restored', style='yellow')

        if not has_config:
            console.print('SSHMirror config is missing', style='yellow')
        elif initialized:
            console.print('SSHMirror is initialized', style='green')
        else:
            console.print('SSHMirror is not initialized yet', style='yellow')

        menu_items = _build_interactive_menu_items(
            has_config=has_config,
            has_ignore=has_ignore,
            initialized=initialized,
            has_stash=has_stash,
        )
        labels = [label for label, _action in menu_items]
        action_by_label = {label: action for label, action in menu_items}
        action = action_by_label[prompt_choice('SSHMirror action:', labels)]

        if action == 'Create sshmirror.config.yml':
            if _create_default_config():
                args.exit_requested = True
                return args
            continue
        if action == 'Create sshmirror.ignore.txt':
            _create_default_ignore()
            continue
        if action == 'Exit':
            args.exit_requested = True
            return args
        if action == 'Pull only':
            args.pull = True
        elif action == 'Status':
            args.status = True
        elif action == 'View current changes':
            args.current_diff = True
        elif action == 'View version changes':
            args.version_diff = True
        elif action == 'Stash changes':
            args.stash_changes = True
        elif action == 'Restore stashed changes':
            args.restore_stash = True
        elif action == 'Force pull':
            args.force_pull = True
        elif action == 'Discard all local changes':
            args.discard = True
        elif action == 'Discard selected files':
            args.discard_files = prompt_discard_files()
        elif action == 'Downgrade remote version':
            args.downgrade = True
        elif action == 'Test connection':
            args.test_connection = True
        return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser('SSH directory synchronization')
    parser.add_argument('-p', '--pull', action='store_true', help='Only pull from remote')
    parser.add_argument('--status', action='store_true', help='Show local and remote synchronization status')
    parser.add_argument('--current-diff', action='store_true', help='Interactively inspect current local versus remote file differences')
    parser.add_argument('--version-diff', action='store_true', help='Interactively inspect file changes between local versions')
    parser.add_argument('--stash-changes', action='store_true', help='Stash local changes and sync from remote')
    parser.add_argument('--restore-stash', action='store_true', help='Restore previously stashed local changes')
    parser.add_argument('--force-pull', action='store_true', help='Force pull from remote. Overwrite local files')
    parser.add_argument('--discard', action='store_true', help='Discard all local changes')
    parser.add_argument('--downgrade', action='store_true', help='Downgrade remote version')
    parser.add_argument('--discard-files', nargs='+', help='Files to discard (will be load from remote)')
    parser.add_argument('--test-connection', action='store_true', help='Test SSH access to the remote host and configured Docker host')
    return parser


def _create_mirror_from_args(args: argparse.Namespace) -> SSHMirror:
    args.config = _find_default_cli_path('sshmirror.config.yml')
    args.ignore = _find_default_cli_path('sshmirror.ignore.txt')

    if args.config and os.path.exists(args.config):
        callbacks = SSHMirrorCallbacks(confirm=prompt_confirm, choose=prompt_choice)
        callbacks.text = prompt_text
        return SSHMirror(
            config=SSHMirrorConfig.from_file(
                args.config,
                ignore=args.ignore,
                pull_only=args.pull,
                downgrade=args.downgrade,
                discard_files=args.discard_files,
            ),
            callbacks=callbacks,
        )

    raise FileNotFoundError('Config not found. Expected sshmirror.config.yml or .sshmirror/sshmirror.config.yml')


async def _show_current_changes_cli(mirror: SSHMirror) -> None:
    console.print('Connect to remote...', style='yellow')
    file_actions = await mirror.list_current_changes()
    if len(file_actions) == 0:
        console.print('No current file differences between local and remote', style='yellow')
        return

    while True:
        choice_map = {f'{item.action} {item.path}': item for item in file_actions}
        choice = prompt_choice('Choose current file change to inspect', list(choice_map.keys()) + ['Back'])
        if choice == 'Back':
            return

        detail = await mirror.get_current_change_detail(choice_map[choice].path)
        mirror.render_diff_detail(detail)


async def _show_version_changes_cli(mirror: SSHMirror) -> None:
    console.print('Connect to remote...', style='yellow')
    versions = await mirror.list_remote_versions()
    if len(versions) < 2:
        console.print('Need at least two remote versions to inspect changes', style='yellow')
        return

    version_labels = {version.label: version for version in versions}
    base_label = prompt_choice('Choose base version', list(version_labels.keys()))
    base_version = version_labels[base_label]
    base_index = versions.index(base_version)
    later_versions = versions[base_index + 1:]
    if len(later_versions) == 0:
        console.print('No later versions available for comparison', style='yellow')
        return

    target_labels = {version.label: version for version in later_versions}
    target_label = prompt_choice('Choose target version', list(target_labels.keys()))
    target_version = target_labels[target_label]

    file_actions = await mirror.list_version_changes(base_version.uid, target_version.uid)
    if len(file_actions) == 0:
        console.print('No file changes between selected versions', style='yellow')
        return

    while True:
        choice_map = {f'{item.action} {item.path}': item for item in file_actions}
        choice = prompt_choice('Choose file change to inspect', list(choice_map.keys()) + ['Back'])
        if choice == 'Back':
            return

        detail = await mirror.get_version_change_detail(base_version.uid, target_version.uid, choice_map[choice].path)
        mirror.render_diff_detail(detail)


def main(argv: list[str] | None = None) -> int:
    def signal_term_handler(*_args):
        console.print('\nCancel by user', style='red', end='')
        raise SystemExit(1)

    signal.signal(signal.SIGINT, signal_term_handler)

    parser = build_parser()
    args = parser.parse_args(argv)
    is_interactive_launch = argv is None and len(sys.argv) == 1
    if is_interactive_launch:
        args = _configure_interactive_args(args)
        if getattr(args, 'exit_requested', False):
            return 0

    try:
        mirror = _create_mirror_from_args(args)
    except FileNotFoundError as exc:
        console.print(str(exc), style='red')
        return 1

    try:
        if _has_stashed_changes() and not is_interactive_launch:
            console.print('Reminder: stashed changes are available. Use restore stash to bring them back.', style='yellow')
        if args.test_connection:
            asyncio.run(mirror.test_connection())
        elif args.status:
            asyncio.run(mirror.status())
        elif args.current_diff:
            asyncio.run(_show_current_changes_cli(mirror))
        elif args.version_diff:
            asyncio.run(_show_version_changes_cli(mirror))
        elif args.restore_stash:
            asyncio.run(mirror.restore_stash())
        elif args.stash_changes:
            asyncio.run(mirror.stash_changes())
        elif args.force_pull or args.discard:
            asyncio.run(mirror.force_pull())
        else:
            asyncio.run(mirror.run())
        return 0
    except UserAbort as exc:
        if str(exc):
            console.print(str(exc), style='yellow')
        return 0
    except Exception as exc:
        console.print(str(exc), style='red')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())