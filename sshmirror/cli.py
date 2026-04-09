import argparse
import asyncio
import math
import os
import signal
import sys
from collections import OrderedDict

try:
    from .config import SSHMirrorCallbacks, SSHMirrorConfig
    from .core.schemas import DiffFileChange, DiffVersionInfo
    from rich import box
    from rich.table import Table
    from .prompts import prompt_choice, prompt_confirm, prompt_discard_files, prompt_secret, prompt_text
    from .sshmirror import SSHMirror, STASH_METADATA_FILE, console
    from .core.exceptions import UserAbort
    from .core.utils import read_text_file
except ImportError:
    from config import SSHMirrorCallbacks, SSHMirrorConfig
    from core.schemas import DiffFileChange, DiffVersionInfo
    from rich import box
    from rich.table import Table
    from prompts import prompt_choice, prompt_confirm, prompt_discard_files, prompt_secret, prompt_text
    from sshmirror import SSHMirror, STASH_METADATA_FILE, console
    from core.exceptions import UserAbort
    from core.utils import read_text_file


VERSION_PAGE_SIZE = 20
VERSION_NUMBER_COLUMN_WIDTH = 5
VERSION_TIME_COLUMN_WIDTH = 23
VERSION_AUTHOR_COLUMN_WIDTH = 12
VERSION_UID_COLUMN_WIDTH = 8
VERSION_MESSAGE_COLUMN_WIDTH = 28


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
    # username: root

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
        callbacks.secret = prompt_secret
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
        inspectable_actions = [item for item in file_actions if item.action == 'change' and item.inspectable]
        if len(inspectable_actions) == 0:
            console.print('No changed files available for textual inspection', style='yellow')
            return

        choice_map = OrderedDict((item.path, item) for item in inspectable_actions)
        choice = prompt_choice('Choose changed file to inspect', list(choice_map.keys()) + ['Back'])
        if choice == 'Back':
            return

        detail = await mirror.get_current_change_detail(choice_map[choice].path)
        mirror.render_diff_detail(detail)
        prompt_choice('Diff actions', ['Back'], default='Back')
        console.clear()


def _build_version_page_choices(total_versions: int, page: int, page_size: int = VERSION_PAGE_SIZE) -> tuple[bool, bool, int]:
    total_pages = max(1, math.ceil(total_versions / page_size))
    normalized_page = max(0, min(page, total_pages - 1))
    has_newer = normalized_page > 0
    has_older = (normalized_page + 1) * page_size < total_versions
    return has_newer, has_older, total_pages


def _format_version_page_prompt(prompt: str, page: int, total_versions: int, page_size: int = VERSION_PAGE_SIZE) -> str:
    total_pages = max(1, math.ceil(total_versions / page_size))
    normalized_page = max(0, min(page, total_pages - 1))
    if total_versions == 0:
        return prompt

    shown_to = total_versions - (normalized_page * page_size)
    shown_from = max(1, shown_to - page_size + 1)
    return f'{prompt} (page {normalized_page + 1}/{total_pages}, showing {shown_from}-{shown_to} of {total_versions}, newest first)'


def _build_version_choice_map(page_versions: list[DiffVersionInfo]) -> OrderedDict[str, DiffVersionInfo]:
    return OrderedDict((_format_version_choice_label(version), version) for version in page_versions)


def _format_fixed_width_column(value: str, width: int) -> str:
    normalized = (value or '-').strip() or '-'
    if len(normalized) > width:
        if width <= 3:
            return normalized[:width]
        normalized = normalized[:width - 3] + '...'
    return normalized.ljust(width)


def _format_version_choice_parts(version: DiffVersionInfo) -> tuple[str, str, str, str, str]:
    global_number = f'{((version.index + 1) if version.index is not None else 0):>{VERSION_NUMBER_COLUMN_WIDTH}}'
    display_time = version.label.split(' | ')[0] if ' | ' in version.label else version.dt
    display_time = _format_fixed_width_column(display_time, VERSION_TIME_COLUMN_WIDTH)
    author = _format_fixed_width_column(version.author or '-', VERSION_AUTHOR_COLUMN_WIDTH)
    message = _format_fixed_width_column(version.message or 'update', VERSION_MESSAGE_COLUMN_WIDTH)
    uid = _format_fixed_width_column(version.uid[:8] or '-', VERSION_UID_COLUMN_WIDTH)
    return global_number, display_time, author, uid, message


def _format_version_choice_label(version: DiffVersionInfo) -> str:
    global_number, display_time, author, uid, message = _format_version_choice_parts(version)
    return f'{global_number} | {display_time} | {author} | {uid} | {message}'


def _render_version_page(page_versions: list[DiffVersionInfo], prompt: str) -> OrderedDict[str, DiffVersionInfo]:
    choice_map = _build_version_choice_map(page_versions)
    return choice_map


def _build_styled_version_choice(version: DiffVersionInfo, *, is_base: bool = False) -> 'questionary.Choice | None':
    try:
        import questionary as _q
        from prompt_toolkit.formatted_text import FormattedText
    except Exception:
        return None

    global_number, time_part, author, uid_part, message = _format_version_choice_parts(version)

    parts: list[tuple[str, str]] = [
        ('class:yellow', global_number),
        ('', ' | '),
        ('class:green', time_part),
        ('', ' | '),
        ('class:magenta', author),
        ('', ' | '),
        ('class:cyan', uid_part),
        ('', ' | '),
        ('', message),
    ]
    if is_base:
        parts.append(('class:base_marker', '  ◀ base'))

    title = FormattedText(parts)
    value = _format_version_choice_label(version)
    if is_base:
        return _q.Choice(title=title, value=value, disabled='base')
    return _q.Choice(title=title, value=value)


def _normalize_diff_action_label(action: str) -> str:
    normalized = action.strip().lower()
    if normalized == 'change':
        return 'changed'
    if normalized == 'create':
        return 'created'
    if normalized == 'delete':
        return 'deleted'
    return normalized


def _build_styled_file_change_choice(file_action: DiffFileChange) -> 'questionary.Choice | None':
    try:
        import questionary as _q
        from prompt_toolkit.formatted_text import FormattedText
    except Exception:
        return None

    action_label = _normalize_diff_action_label(file_action.action)
    badge_style = {
        'changed': 'file_change_changed',
        'created': 'file_change_created',
        'deleted': 'file_change_deleted',
    }.get(action_label, 'file_change_other')
    path_style = {
        'changed': 'file_change_path_changed',
        'created': 'file_change_path_created',
        'deleted': 'file_change_path_deleted',
    }.get(action_label, 'file_change_path_other')
    marker = {
        'changed': '~',
        'created': '+',
        'deleted': '-',
    }.get(action_label, '*')
    is_selectable = action_label == 'changed' and file_action.inspectable
    title_parts: list[tuple[str, str]] = [
        (f'class:{badge_style}', f' {marker} {action_label.upper():<7} '),
        ('class:file_change_separator', '  '),
        (f'class:{path_style}', file_action.path),
    ]
    if not is_selectable:
        title_parts.extend([
            ('class:file_change_separator', '  '),
            ('class:file_change_note', '[view only]'),
        ])

    title = FormattedText(title_parts)
    if not is_selectable:
        return _q.Choice(title=title, value=file_action.path, disabled=action_label)
    return _q.Choice(title=title, value=file_action.path)


def _format_file_change_prompt(prompt: str, file_actions: list[DiffFileChange]) -> str:
    if len(file_actions) == 0:
        return prompt

    lines = [prompt, '']
    for item in file_actions:
        action_label = _normalize_diff_action_label(item.action)
        suffix = '' if action_label == 'changed' and item.inspectable else ' (not selectable)'
        lines.append(f'  {action_label:<7} | {item.path}{suffix}')
    return '\n'.join(lines)


_VERSION_SELECT_STYLE = None
try:
    from prompt_toolkit.styles import Style as _PTStyle
    _VERSION_SELECT_STYLE = _PTStyle([
        ('yellow', '#e5c07b'),
        ('green', '#98c379'),
        ('magenta', '#c678dd'),
        ('cyan', '#56b6c2'),
        ('base_marker', '#e06c75 bold'),
        ('file_change_changed', '#98c379 bold'),
        ('file_change_created', '#56b6c2 bold'),
        ('file_change_deleted', '#e06c75 bold'),
        ('file_change_other', '#e5c07b bold'),
        ('file_change_path_changed', '#d9f6c1'),
        ('file_change_path_created', '#c8eeff'),
        ('file_change_path_deleted', '#ffccd1'),
        ('file_change_path_other', '#f6e1a6'),
        ('file_change_separator', '#7f848e'),
        ('file_change_note', '#7f848e italic'),
    ])
except Exception:
    pass


async def _choose_version_interactively(mirror: SSHMirror, prompt: str, start_index: int = 0, base_version: 'DiffVersionInfo | None' = None) -> DiffVersionInfo | None:
    page = 0
    while True:
        page_versions, total_versions = await mirror.list_remote_versions_page(
            page=page,
            page_size=VERSION_PAGE_SIZE,
            start_index=start_index,
        )
        if total_versions == 0:
            return None

        has_newer, has_older, total_pages = _build_version_page_choices(total_versions, page)
        page_prompt = prompt
        if total_versions > VERSION_PAGE_SIZE:
            page_prompt = _format_version_page_prompt(prompt, page, total_versions)

        choice_map = _render_version_page(page_versions, page_prompt)
        choices = list(choice_map.keys())

        styled_choices = [_build_styled_version_choice(v) for v in page_versions]
        use_styled = all(c is not None for c in styled_choices)

        # Prepend base version as a disabled highlighted entry
        base_styled_choice = None
        if base_version is not None and use_styled:
            base_styled_choice = _build_styled_version_choice(base_version, is_base=True)

        nav_choices: list[str] = []
        if has_newer:
            nav_choices.append('Newer versions')
        if has_older:
            nav_choices.append('Older versions')
        nav_choices.append('Back')

        if use_styled:
            try:
                import questionary as _q
                base_prefix = [base_styled_choice] if base_styled_choice is not None else []
                all_styled = base_prefix + list(styled_choices) + [_q.Choice(title=nav, value=nav) for nav in nav_choices]
            except Exception:
                use_styled = False

        default_choice = next(reversed(choice_map)) if len(choice_map) > 0 else 'Back'

        # For fallback mode, show base version in the prompt
        display_prompt = page_prompt
        if base_version is not None and not use_styled:
            base_label = _format_version_choice_label(base_version)
            display_prompt = f'{page_prompt}\n  ◀ base: {base_label}'

        if use_styled:
            choice = prompt_choice(
                page_prompt, choices + nav_choices,
                default=default_choice,
                styled_choices=all_styled,
                style=_VERSION_SELECT_STYLE,
            )
        else:
            choice = prompt_choice(display_prompt, choices + nav_choices, default=default_choice)

        if choice == 'Back':
            return None
        if choice == 'Newer versions':
            page -= 1
            continue
        if choice == 'Older versions':
            page += 1
            continue

        selected = choice_map.get(choice)
        if selected is not None:
            return selected

        raise ValueError(f'Unknown version choice {choice!r}')


async def _show_version_changes_cli(mirror: SSHMirror) -> None:
    console.print('Connect to remote...', style='yellow')
    _first_page_versions, total_versions = await mirror.list_remote_versions_page(page=0, page_size=VERSION_PAGE_SIZE)
    if total_versions < 2:
        console.print('Need at least two remote versions to inspect changes', style='yellow')
        return

    base_version = await _choose_version_interactively(mirror, 'Choose base version')
    if base_version is None:
        return

    if base_version.index is None:
        raise ValueError('Selected version is missing index information')

    if total_versions - (base_version.index + 1) <= 0:
        console.print('No later versions available for comparison', style='yellow')
        return

    if base_version.filename is None:
        raise ValueError('Selected version is missing filename information')

    target_version = await _choose_version_interactively(mirror, 'Choose target version', start_index=base_version.index + 1, base_version=base_version)
    if target_version is None:
        return

    if target_version.filename is None:
        raise ValueError('Selected version is missing filename information')

    file_actions = await mirror.list_version_changes_by_filenames(base_version.filename, target_version.filename)
    if len(file_actions) == 0:
        console.print('No file changes between selected versions', style='yellow')
        return

    while True:
        inspectable_actions = [item for item in file_actions if item.action == 'change' and item.inspectable]
        choice_map = OrderedDict((item.path, item) for item in inspectable_actions)
        selectable_choices = list(choice_map.keys()) + ['Back']
        styled_choices = [_build_styled_file_change_choice(item) for item in file_actions]
        use_styled = all(choice is not None for choice in styled_choices)

        if use_styled:
            try:
                import questionary as _q
                all_styled = list(styled_choices) + [_q.Choice(title='Back', value='Back')]
                choice = prompt_choice(
                    'Choose file change to inspect',
                    selectable_choices,
                    default='Back' if len(choice_map) == 0 else next(iter(choice_map.keys())),
                    styled_choices=all_styled,
                    style=_VERSION_SELECT_STYLE,
                )
            except Exception:
                use_styled = False

        if not use_styled:
            choice = prompt_choice(
                _format_file_change_prompt('Choose file change to inspect', file_actions),
                selectable_choices,
                default='Back' if len(choice_map) == 0 else next(iter(choice_map.keys())),
            )

        if choice == 'Back':
            return

        detail = await mirror.get_version_change_detail_by_range(
            base_version.filename,
            target_version.filename,
            base_version.index,
            target_version.index,
            choice_map[choice].path,
        )
        mirror.render_diff_detail(detail)
        prompt_choice('Diff actions', ['Back'], default='Back')
        console.clear()


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