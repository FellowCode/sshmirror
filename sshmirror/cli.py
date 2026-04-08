import argparse
import asyncio
import os
import signal
import sys

try:
    from .config import SSHMirrorCallbacks, SSHMirrorConfig
    from .prompts import prompt_choice, prompt_confirm, prompt_discard_files
    from .sshmirror import SSHMirror, STASH_METADATA_FILE, console
    from .core.exceptions import UserAbort
    from .core.utils import read_text_file
except ImportError:
    from config import SSHMirrorCallbacks, SSHMirrorConfig
    from prompts import prompt_choice, prompt_confirm, prompt_discard_files
    from sshmirror import SSHMirror, STASH_METADATA_FILE, console
    from core.exceptions import UserAbort
    from core.utils import read_text_file


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


def _create_default_config() -> None:
    config_example_path = 'sshmirror.config.example.yml'
    target_path = 'sshmirror.config.yml'

    if os.path.exists(target_path):
        console.print(f'{target_path} already exists', style='yellow')
        return

    if os.path.exists(config_example_path):
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(read_text_file(config_example_path))
    else:
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(
                "host: '127.0.0.1'\n"
                "port: '22'\n"
                "username: 'root'\n"
                "localdir: '.'\n"
                "remotedir: '/app'\n"
                "author: user\n"
            )

    console.print(f'Created {target_path}', style='green')


def _create_default_ignore() -> None:
    target_path = 'sshmirror.ignore.txt'
    if os.path.exists(target_path):
        console.print(f'{target_path} already exists', style='yellow')
        return

    with open(target_path, 'w', encoding='utf-8') as f:
        f.write('# One path or pattern per line\n')

    console.print(f'Created {target_path}', style='green')


def _configure_interactive_args(args: argparse.Namespace) -> argparse.Namespace:
    while True:
        has_config = _find_default_cli_path('sshmirror.config.yml') is not None
        has_ignore = _find_default_cli_path('sshmirror.ignore.txt') is not None
        initialized = _is_sshmirror_initialized()
        has_stash = _has_stashed_changes()

        if has_stash:
            console.print('Reminder: stashed changes are waiting to be restored', style='yellow')

        if not has_config:
            console.print('SSHMirror config is missing', style='yellow')
            choices = []
        elif initialized:
            console.print('SSHMirror is initialized', style='green')
            choices = [
                'Pull & Push',
                'Status',
                'View current changes',
                'View version changes',
                'Pull only',
                'Restore stashed changes' if has_stash else 'Stash changes',
                'Force pull',
                'Discard all local changes',
                'Discard selected files',
                'Downgrade remote version',
                'Test connection',
            ]
        else:
            console.print('SSHMirror is not initialized yet', style='yellow')
            choices = [
                'Initialization',
                'Status',
                'View current changes',
                'Restore stashed changes' if has_stash else None,
                'Test connection',
            ]
            choices = [choice for choice in choices if choice is not None]

        if not has_config:
            choices.insert(0, 'Create sshmirror.config.yml')
        if not has_ignore:
            insert_at = 1 if not has_config else 0
            choices.insert(insert_at, 'Create sshmirror.ignore.txt')

        choices.append('Exit')
        action = prompt_choice('SSHMirror action:', choices)

        if action == 'Create sshmirror.config.yml':
            _create_default_config()
            continue
        if action == 'Create sshmirror.ignore.txt':
            _create_default_ignore()
            continue
        if action == 'Exit':
            raise UserAbort('Cancelled by user')
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