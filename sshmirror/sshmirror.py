import asyncssh
from asyncssh import SSHClientConnection, SFTPClient
import asyncio
import difflib
import os
import re
import shlex
import json
import hashlib
import yaml
import typing
import datetime
import aiofiles
import pathlib
import shutil
import aioshutil
from rich import print
from rich.console import Console
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box
import beartype as bt

try:
    from .config import SSHMirrorCallbacks, SSHMirrorConfig
    from .core.schemas import CmdConfig, Command, CopyPath, DiffDetail, DiffFileChange, DiffVersionInfo, MigrationChanges
    from .core.filemap import FileEntry, FileMap, DirVersion, Migration, Conflicts
    from .core.filewatcher import Filewatcher
    from .core.utils import check_path_is_ignored, clear_n_console_rows, compile_ignore_rules, parse_ignore_file, read_text_file, write_text_file_atomic_async
    from .core.exceptions import ErrorLocalVersion, UserAbort, VersionAlreadyExists
except ImportError:
    from config import SSHMirrorCallbacks, SSHMirrorConfig
    from core.schemas import CmdConfig, Command, CopyPath, DiffDetail, DiffFileChange, DiffVersionInfo, MigrationChanges
    from core.filemap import FileEntry, FileMap, DirVersion, Migration, Conflicts
    from core.filewatcher import Filewatcher
    from core.utils import check_path_is_ignored, clear_n_console_rows, compile_ignore_rules, parse_ignore_file, read_text_file, write_text_file_atomic_async
    from core.exceptions import ErrorLocalVersion, UserAbort, VersionAlreadyExists

console = Console()

STASH_DIRECTORY = '.sshmirror/stash/current'
STASH_FILES_DIRECTORY = f'{STASH_DIRECTORY}/files'
STASH_METADATA_FILE = f'{STASH_DIRECTORY}/metadata.json'


def _is_sshmirror_initialized() -> bool:
    if os.path.isdir(SSHMirror.versions_directory):
        return any(os.scandir(SSHMirror.versions_directory))

    return False


def _has_stashed_changes() -> bool:
    return os.path.exists(STASH_METADATA_FILE)


def _prompt_choice(prompt: str, choices: list[str]) -> str:
    try:
        from .prompts import prompt_choice
    except ImportError:
        from prompts import prompt_choice

    return prompt_choice(prompt, choices)


def _prompt_initialization_source() -> str:
    try:
        from .prompts import prompt_initialization_source
    except ImportError:
        from prompts import prompt_initialization_source

    return prompt_initialization_source()


@bt.beartype
class SSHMirror:
    WARNING_SIZE = 10 * 1024 * 1024 # 100Mb
    DIFF_CONTEXT_LINES = 2
    VERSION_MESSAGE_MAX_LENGTH = 50
    versions_directory = '.sshmirror/versions'
    migrations_directory = '.sshmirror/migrations'
    conflicts_file = '.sshmirror/conflicts.json'
    prevstate_file = '.sshmirror/prevstate.json'
    filesize_ignore_file = 'sshmirror.ignoresize.txt'
    stash_directory = STASH_DIRECTORY
    stash_files_directory = STASH_FILES_DIRECTORY
    stash_metadata_file = STASH_METADATA_FILE
    default_ignore_filename = 'sshmirror.ignore.txt'
    
    def __init__(self, 
                 config: str | SSHMirrorConfig | None = None, 
                 host: str | None = None, 
                 port: int = 22,
                 username: str | None = None,
                 password: str | None = None,
                 private_key: str | None = None,
                 private_key_passphrase: str | None = None,
                 localdir: str | None = None, 
                 remotedir: str | None = None,
                 ignore: str | None = None,
                 restart_container: dict | None = None,
                 aliases=None,
                 watch: bool = False,
                 no_sync: bool = False,
                 author: str = None,
                 pull_only: bool = False,
                 downgrade: bool = False,
                 discard_files: list[str] | None = None,
                 callbacks: SSHMirrorCallbacks | None = None) -> None:
                
        try:
            os.makedirs(self.versions_directory)
        except:
            pass

        if isinstance(config, SSHMirrorConfig):
            resolved_config = config
        elif isinstance(config, str):
            resolved_config = SSHMirrorConfig.from_file(
                config,
                password=password,
                private_key=private_key,
                private_key_passphrase=private_key_passphrase,
                ignore=ignore,
                author=author,
                pull_only=pull_only,
                downgrade=downgrade,
                discard_files=discard_files,
                aliases=aliases,
                watch=watch,
                no_sync=no_sync,
            )
        else:
            resolved_config = SSHMirrorConfig(
                host=host,
                port=port,
                username=username,
                password=password,
                private_key=private_key,
                private_key_passphrase=private_key_passphrase,
                localdir=localdir,
                remotedir=remotedir,
                ignore=ignore,
                restart_container=restart_container,
                aliases=aliases or {},
                watch=watch,
                no_sync=no_sync,
                author=author,
                pull_only=pull_only,
                downgrade=downgrade,
                discard_files=discard_files,
            )

        resolved_config = resolved_config.validate()

        self.callbacks = callbacks or SSHMirrorCallbacks()
        self.commands = resolved_config.commands
            
        self.author = resolved_config.author
        self.host = resolved_config.host
        self.port = resolved_config.port
        self.username = resolved_config.username
        self.password = resolved_config.password
        self.private_key = self._normalize_private_key_path(resolved_config.private_key)
        self.private_key_passphrase = resolved_config.private_key_passphrase
        self.auth_kwargs = self._build_auth_kwargs(
            password=self.password,
            private_key=self.private_key,
            private_key_passphrase=self.private_key_passphrase,
        )
        self.localdir = resolved_config.localdir
        self.remotedir = resolved_config.remotedir
        self.pull_only = resolved_config.pull_only
        self.downgrade = resolved_config.downgrade
        self.discard_files = resolved_config.discard_files
        self.restart_container = resolved_config.restart_container
        if not self.remotedir.endswith('/'):
            self.remotedir += '/'
        self.aliases = resolved_config.aliases or {}
        self.watch = resolved_config.watch
        self.no_sync = resolved_config.no_sync
        
        self.remote_version = None
        self.local_version = None
        
        self.configured_ignore_path = resolved_config.ignore
        self.ignore_file_path = None

        self.filewatcher = Filewatcher(self.localdir, None)
        self._runtime_restart_sudo_password = None

        self._refresh_ignore_file_path()

    def _build_local_project_path_candidates(self, path: str) -> list[str]:
        candidates: list[str] = []
        if os.path.isabs(path):
            return [path]

        base_dir = os.path.abspath(self.localdir or '.')
        candidates.append(os.path.join(base_dir, path))
        candidates.append(os.path.join(base_dir, '.sshmirror', path))
        candidates.append(path)
        return candidates

    def _resolve_ignore_file_path(self) -> str | None:
        if self.configured_ignore_path:
            candidates = self._build_local_project_path_candidates(self.configured_ignore_path)
            for candidate in candidates:
                if os.path.exists(candidate):
                    return os.path.abspath(candidate)
            return os.path.abspath(candidates[0])

        for candidate in self._build_local_project_path_candidates(self.default_ignore_filename):
            if os.path.exists(candidate):
                return os.path.abspath(candidate)

        return None

    def _refresh_ignore_file_path(self) -> None:
        self.ignore_file_path = self._resolve_ignore_file_path()
        self.filewatcher.ignore_file_path = self.ignore_file_path
        FileMap.init(ignore_file_path=self.ignore_file_path)

    def _get_ignore_sync_target(self) -> tuple[str, str] | None:
        base_dir = os.path.abspath(self.localdir or '.')
        if self.ignore_file_path is not None:
            ignore_path = os.path.abspath(self.ignore_file_path)
        elif self.configured_ignore_path:
            ignore_path = os.path.abspath(self._build_local_project_path_candidates(self.configured_ignore_path)[0])
        else:
            ignore_path = os.path.join(base_dir, self.default_ignore_filename)

        try:
            relative_path = os.path.relpath(ignore_path, base_dir)
        except ValueError:
            return None

        if relative_path.startswith('..'):
            return None
        return relative_path.replace('\\', '/'), ignore_path

    @staticmethod
    async def _build_local_file_entry(path: str, reference_entry: FileEntry | None = None) -> FileEntry | None:
        if not os.path.exists(path):
            return None

        stat_result = os.stat(path)
        size = int(stat_result.st_size)
        mtime = int(stat_result.st_mtime_ns)
        if reference_entry is not None and reference_entry.stat_matches(size, mtime):
            return FileEntry(md5=reference_entry.md5, size=size, mtime=mtime)

        async with aiofiles.open(path, 'rb') as f:
            md5 = hashlib.md5(await f.read()).hexdigest()
        return FileEntry(md5=md5, size=size, mtime=mtime)

    async def _get_remote_file_stat(self, conn: SSHClientConnection, path: str) -> tuple[int, int] | None:
        remote_path = self._remote_get_abs_path(path)
        result = await conn.run(
            f'test -f {shlex.quote(remote_path)} && stat -c "%Y\t%s" {shlex.quote(remote_path)}',
            check=False,
        )
        if result.exit_status != 0 or result.stdout.strip() == '':
            return None

        mtime, size = result.stdout.strip().split('\t', 1)
        return int(float(mtime) * 1_000_000_000), int(size)

    async def _download_remote_file_to_path(
        self,
        conn: SSHClientConnection,
        remote_relative_path: str,
        local_path: str,
        mtime_ns: int | None = None,
    ) -> None:
        pathlib.Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        async with conn.start_sftp_client() as sftp:
            await sftp.get(self._remote_get_abs_path(remote_relative_path), local_path)
        if mtime_ns is not None:
            os.utime(local_path, ns=(mtime_ns, mtime_ns))

    async def _sync_ignore_file_before_transfer(self, conn: SSHClientConnection) -> None:
        self._refresh_ignore_file_path()
        ignore_target = self._get_ignore_sync_target()
        if ignore_target is None:
            return
        ignore_sync_path, local_ignore_path = ignore_target

        remote_stat = await self._get_remote_file_stat(conn, ignore_sync_path)
        if remote_stat is None:
            return

        remote_mtime_ns, _remote_size = remote_stat
        local_exists = os.path.exists(local_ignore_path)
        local_mtime_ns = int(os.stat(local_ignore_path).st_mtime_ns) if local_exists else -1
        if local_exists and remote_mtime_ns <= local_mtime_ns:
            return

        prevstate = await self._load_prevstate()
        prevstate_entry = prevstate.get_file(ignore_sync_path) if prevstate is not None else None
        local_entry = await self._build_local_file_entry(local_ignore_path, prevstate_entry) if local_exists else None
        has_local_conflict = local_entry is not None and (
            prevstate_entry is None or not FileMap._entries_equal(local_entry, prevstate_entry)
        )

        if has_local_conflict:
            conflicts = Conflicts(remote_version_uid='ignore-file-sync', files=[ignore_sync_path], dirs=[])
            await self._resolve_conflicts(conflicts)

        await self._download_remote_file_to_path(conn, ignore_sync_path, local_ignore_path, mtime_ns=remote_mtime_ns)
        self._refresh_ignore_file_path()
        if not has_local_conflict:
            console.print(f'Updated ignore config from remote: {ignore_sync_path}', style='yellow')
            return

        raise UserAbort('Resolve ignore file conflict and retry sync')

    @staticmethod
    def _format_version_label(version: DirVersion) -> str:
        display_dt = version.dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        author = (version.author or '').strip()
        message = (version.message or 'update').strip() or 'update'
        if author:
            return f'{display_dt} | {version.uid[:8]} | {author} | {message}'
        return f'{display_dt} | {version.uid[:8]} | {message}'

    @staticmethod
    def _normalize_version_message(message: str | None) -> str:
        normalized = (message or '').strip()
        if normalized == '':
            return 'update'
        if len(normalized) > SSHMirror.VERSION_MESSAGE_MAX_LENGTH:
            raise ValueError(
                f'Version description must be at most {SSHMirror.VERSION_MESSAGE_MAX_LENGTH} characters long'
            )
        return normalized

    def _prompt_version_message(self) -> str:
        if self.callbacks.text is None:
            return 'update'
        return self._normalize_version_message(
            self.callbacks.text(
                f'Version description (max {self.VERSION_MESSAGE_MAX_LENGTH} chars)',
                'update',
            )
        )

    def _confirm(self, message: str, abort_message: str) -> bool:
        if self.callbacks.confirm is None:
            raise UserAbort(abort_message)
        return self.callbacks.confirm(message)

    def _choose(self, message: str, choices: list[str], abort_message: str) -> str:
        if self.callbacks.choose is None:
            raise UserAbort(abort_message)
        choice = self.callbacks.choose(message, choices)
        if choice not in choices:
            raise ValueError(f'Invalid choice {choice!r} for {message!r}')
        return choice

    async def _read_remote_text_file(self, conn: SSHClientConnection, remote_path: str) -> str | None:
        try:
            result = await conn.run(f'cat {shlex.quote(remote_path)}', check=False)
            if result.exit_status != 0:
                return None
            return result.stdout
        except Exception:
            return None

    async def _get_remote_migration_changes_cached(
        self,
        conn: SSHClientConnection,
        cache: dict[str, MigrationChanges],
        version: DirVersion,
    ) -> MigrationChanges:
        cached = cache.get(version.uid)
        if cached is not None:
            return cached
        migration = await self._get_remote_migration_changes(conn, version)
        cache[version.uid] = migration
        return migration

    async def _get_remote_version_file_text(
        self,
        conn: SSHClientConnection,
        versions: list[DirVersion],
        version_index: int,
        path: str,
        migration_cache: dict[str, MigrationChanges],
    ) -> str | None:
        version = versions[version_index]
        if version.filemap.get_file(path) is None:
            return None

        for later_index in range(version_index + 1, len(versions)):
            later_version = versions[later_index]
            migration = await self._get_remote_migration_changes_cached(conn, migration_cache, later_version)
            if path in migration.files.changed or path in migration.files.deleted:
                snapshot_path = self._remote_get_abs_path(f'{self.migrations_directory}/{later_version.name()}/downgrade/{path}')
                return await self._read_remote_text_file(conn, snapshot_path)
            if path in migration.files.created:
                return None

        return await self._read_remote_text_file(conn, self._remote_get_abs_path(path))

    @staticmethod
    def _get_local_file_text(path: str) -> str | None:
        if not os.path.exists(path):
            return None
        return read_text_file(path)

    @staticmethod
    def _build_diff_text(value: str, base_style: str) -> Text:
        return Text(value, style=base_style)

    @classmethod
    def _build_replaced_line_pair(cls, before_line: str, after_line: str) -> tuple[Text, Text]:
        before_text = cls._build_diff_text(before_line, 'red')
        after_text = cls._build_diff_text(after_line, 'green')

        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before_line, after_line).get_opcodes():
            if tag == 'equal':
                continue
            if i1 != i2:
                before_text.stylize('bold white on red', i1, i2)
            if j1 != j2:
                after_text.stylize('bold black on green', j1, j2)

        return before_text, after_text

    @staticmethod
    def _build_sync_action_rows(migration: Migration, conflicts: Conflicts | None = None) -> list[tuple[str, str, str, str]]:
        rows: list[tuple[str, str, str, str]] = []

        for path in migration.dirs.created:
            rows.append(('+', 'create dir', path, 'conflict' if conflicts and path in conflicts else 'pending'))
        for path in migration.dirs.deleted:
            rows.append(('-', 'delete dir', path, 'conflict' if conflicts and path in conflicts else 'pending'))
        for path in migration.files.created:
            rows.append(('+', 'create', path, 'conflict' if conflicts and path in conflicts else 'pending'))
        for path in migration.files.changed:
            rows.append(('~', 'update', path, 'conflict' if conflicts and path in conflicts else 'pending'))
        for path in migration.files.deleted:
            rows.append(('-', 'delete', path, 'conflict' if conflicts and path in conflicts else 'pending'))

        return rows

    @staticmethod
    def _sync_action_style(action_name: str) -> str:
        if action_name.startswith('create'):
            return 'green bold'
        if action_name.startswith('delete'):
            return 'red bold'
        if action_name == 'update':
            return 'magenta bold'
        return 'yellow bold'

    @classmethod
    def _render_sync_plan(
        cls,
        title: str,
        subtitle: str,
        migration: Migration,
        *,
        conflicts: Conflicts | None = None,
        border_style: str = 'cyan',
    ) -> None:
        summary = Table.grid(padding=(0, 2))
        summary.add_column(style='dim', justify='right')
        summary.add_column()
        summary.add_row('files', f"+{len(migration.files.created)}  ~{len(migration.files.changed)}  -{len(migration.files.deleted)}")
        summary.add_row('dirs', f"+{len(migration.dirs.created)}  -{len(migration.dirs.deleted)}")
        if conflicts is not None and not conflicts.empty():
            summary.add_row('conflicts', f'{len(conflicts.files) + len(conflicts.dirs)} items need local preservation')

        details = Table(box=box.SIMPLE_HEAVY, show_header=True, expand=True, pad_edge=False)
        details.add_column('Action', width=12, no_wrap=True)
        details.add_column('Path', ratio=1, overflow='fold')
        details.add_column('Status', width=12, no_wrap=True)

        for marker, action_name, path, status in cls._build_sync_action_rows(migration, conflicts):
            details.add_row(
                Text(f' {marker} {action_name.upper()} ', style=cls._sync_action_style(action_name)),
                Text(path, style='white'),
                Text(status, style='orange_red1' if status == 'conflict' else 'dim'),
            )

        if details.row_count == 0:
            details.add_row(Text(' OK ', style='green bold'), Text('No changes', style='dim'), Text('clean', style='dim'))

        console.print(
            Panel(
                Group(
                    Text(subtitle, style='yellow'),
                    Text(''),
                    summary,
                    Text(''),
                    details,
                ),
                title=title,
                border_style=border_style,
                expand=True,
            )
        )

    @classmethod
    def _render_diff_row(
        cls,
        table: Table,
        before_number: int | None,
        before_line: str | None,
        after_number: int | None,
        after_line: str | None,
        tag: str,
    ) -> None:
        if tag == 'equal':
            before_render = Text(before_line or '', style='dim')
            after_render = Text(after_line or '', style='dim')
        elif tag == 'replace':
            before_render, after_render = cls._build_replaced_line_pair(before_line or '', after_line or '')
        elif tag == 'delete':
            before_render = Text(before_line or '', style='red')
            after_render = Text('', style='dim')
        else:
            before_render = Text('', style='dim')
            after_render = Text(after_line or '', style='green')

        table.add_row(
            Text(str(before_number) if before_number is not None else '', style='dim'),
            before_render,
            Text(str(after_number) if after_number is not None else '', style='dim'),
            after_render,
        )

    @classmethod
    def _print_unified_diff(cls, path: str, before_text: str, after_text: str, before_label: str, after_label: str):
        before_lines = before_text.splitlines()
        after_lines = after_text.splitlines()
        grouped_opcodes = list(
            difflib.SequenceMatcher(None, before_lines, after_lines).get_grouped_opcodes(cls.DIFF_CONTEXT_LINES)
        )

        if len(grouped_opcodes) == 0:
            console.print('No textual diff available, file metadata changed only.', style='yellow')
            return

        table = Table(
            box=box.SIMPLE_HEAVY,
            show_header=True,
            show_lines=False,
            expand=True,
            pad_edge=False,
        )
        table.add_column(before_label, width=6, justify='right', no_wrap=True)
        table.add_column('Before', ratio=1, overflow='fold')
        table.add_column(after_label, width=6, justify='right', no_wrap=True)
        table.add_column('After', ratio=1, overflow='fold')

        for group_index, group in enumerate(grouped_opcodes):
            if group_index > 0:
                table.add_row('', Text('...', style='dim'), '', Text('...', style='dim'))

            first_tag, first_i1, _, first_j1, _ = group[0]
            before_start = first_i1 + 1 if first_tag != 'insert' else first_i1
            after_start = first_j1 + 1 if first_tag != 'delete' else first_j1
            table.add_row(
                '',
                Text(f'Hunk starting at {before_label}:{before_start}', style='cyan'),
                '',
                Text(f'{after_label}:{after_start}', style='cyan'),
            )

            for tag, i1, i2, j1, j2 in group:
                if tag == 'equal':
                    for before_index, after_index in zip(range(i1, i2), range(j1, j2)):
                        cls._render_diff_row(
                            table,
                            before_index + 1,
                            before_lines[before_index],
                            after_index + 1,
                            after_lines[after_index],
                            tag,
                        )
                elif tag == 'delete':
                    for before_index in range(i1, i2):
                        cls._render_diff_row(
                            table,
                            before_index + 1,
                            before_lines[before_index],
                            None,
                            None,
                            tag,
                        )
                elif tag == 'insert':
                    for after_index in range(j1, j2):
                        cls._render_diff_row(
                            table,
                            None,
                            None,
                            after_index + 1,
                            after_lines[after_index],
                            tag,
                        )
                else:
                    row_count = max(i2 - i1, j2 - j1)
                    for offset in range(row_count):
                        before_index = i1 + offset
                        after_index = j1 + offset
                        cls._render_diff_row(
                            table,
                            before_index + 1 if before_index < i2 else None,
                            before_lines[before_index] if before_index < i2 else None,
                            after_index + 1 if after_index < j2 else None,
                            after_lines[after_index] if after_index < j2 else None,
                            tag,
                        )

        console.print(Panel(table, title=path, border_style='blue'))

    def _is_large_diff_file(self, *entries) -> bool:
        for entry in entries:
            if entry is None:
                continue
            if entry.size is not None and entry.size > self.WARNING_SIZE:
                return True
        return False

    @staticmethod
    def _build_file_actions(migration: MigrationChanges | Migration, created_action: str, deleted_action: str) -> list[DiffFileChange]:
        actions = [DiffFileChange(action='change', path=path) for path in migration.files.changed]
        actions += [DiffFileChange(action=created_action, path=path, inspectable=False) for path in migration.files.created]
        actions += [DiffFileChange(action=deleted_action, path=path, inspectable=False) for path in migration.files.deleted]
        return actions

    def _build_file_actions_with_entries(
        self,
        migration: MigrationChanges | Migration,
        before_map: FileMap,
        after_map: FileMap,
        created_action: str,
        deleted_action: str,
    ) -> list[DiffFileChange]:
        actions = [
            DiffFileChange(
                action='change',
                path=path,
                inspectable=not self._is_large_diff_file(before_map.get_file(path), after_map.get_file(path)),
            )
            for path in migration.files.changed
        ]
        actions += [DiffFileChange(action=created_action, path=path, inspectable=False) for path in migration.files.created]
        actions += [DiffFileChange(action=deleted_action, path=path, inspectable=False) for path in migration.files.deleted]
        return actions

    @staticmethod
    def _find_file_action(file_actions: list[DiffFileChange], path: str) -> DiffFileChange:
        for item in file_actions:
            if item.path == path:
                return item
        raise ValueError(f'No diff entry found for path {path}')

    @staticmethod
    def _entry_asdict(entry) -> dict[str, typing.Any] | None:
        return entry.asdict() if entry is not None else None

    def render_diff_detail(self, detail: DiffDetail) -> None:
        console.print(f'Diff for {detail.path}', style='green bold')
        if detail.is_large or not detail.text_available:
            if detail.message:
                console.print(detail.message, style='yellow')
            console.print(f'  action: {detail.action}', style='cyan')
            console.print(f'  before: {detail.before_entry}', style='cyan')
            console.print(f'  after: {detail.after_entry}', style='cyan')
            return

        assert detail.before_text is not None
        assert detail.after_text is not None
        self._print_unified_diff(detail.path, detail.before_text, detail.after_text, detail.before_label, detail.after_label)

    async def list_current_changes(self) -> list[DiffFileChange]:
        prevstate = await self._load_prevstate()
        local_state = await self.filewatcher.get_filemap(reference_map=prevstate)
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            local_version = await self._get_local_version()
            try:
                remote_versions = await self._get_remote_versions_stack(conn, start_version=local_version)
            except ErrorLocalVersion:
                remote_versions = await self._get_remote_versions_stack(conn)
            remote_reference = remote_versions[-1].filemap if len(remote_versions) > 0 else local_version.filemap if local_version else None
            remote_map = await self._get_remote_map(conn, reference_map=remote_reference)

        migration = local_state.migrate_to(remote_map)
        return self._build_file_actions_with_entries(
            migration,
            local_state,
            remote_map,
            created_action='create on server',
            deleted_action='delete on server',
        )

    async def get_current_change_detail(self, path: str) -> DiffDetail:
        prevstate = await self._load_prevstate()
        local_state = await self.filewatcher.get_filemap(reference_map=prevstate)
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            local_version = await self._get_local_version()
            try:
                remote_versions = await self._get_remote_versions_stack(conn, start_version=local_version)
            except ErrorLocalVersion:
                remote_versions = await self._get_remote_versions_stack(conn)
            remote_reference = remote_versions[-1].filemap if len(remote_versions) > 0 else local_version.filemap if local_version else None
            remote_map = await self._get_remote_map(conn, reference_map=remote_reference)

            migration = local_state.migrate_to(remote_map)
            file_action = self._find_file_action(
                self._build_file_actions(migration, created_action='create on server', deleted_action='delete on server'),
                path,
            )
            local_entry = local_state.get_file(path)
            remote_entry = remote_map.get_file(path)
            if self._is_large_diff_file(local_entry, remote_entry):
                return DiffDetail(
                    path=path,
                    action=file_action.action,
                    before_label='local-current',
                    after_label='remote-current',
                    before_entry=self._entry_asdict(local_entry),
                    after_entry=self._entry_asdict(remote_entry),
                    is_large=True,
                    text_available=False,
                    message='Viewing textual diff for large files is unavailable.',
                )

            local_text = self._get_local_file_text(path) if file_action.action != 'create on server' else ''
            remote_text = await self._read_remote_text_file(conn, self._remote_get_abs_path(path)) if file_action.action != 'delete on server' else ''
            if local_text is None or remote_text is None:
                return DiffDetail(
                    path=path,
                    action=file_action.action,
                    before_label='local-current',
                    after_label='remote-current',
                    before_entry=self._entry_asdict(local_entry),
                    after_entry=self._entry_asdict(remote_entry),
                    text_available=False,
                    message='Text content is unavailable for one side of the current diff.',
                )

            return DiffDetail(
                path=path,
                action=file_action.action,
                before_label='local-current',
                after_label='remote-current',
                before_text=local_text,
                after_text=remote_text,
                before_entry=self._entry_asdict(local_entry),
                after_entry=self._entry_asdict(remote_entry),
            )

    async def list_remote_versions(self) -> list[DiffVersionInfo]:
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            versions = await self._get_remote_versions_stack(conn)

        return [
            DiffVersionInfo(
                uid=version.uid,
                label=self._format_version_label(version),
                dt=version.dt.isoformat(),
                author=version.author,
                message=version.message,
                index=index,
            )
            for index, version in enumerate(versions)
        ]

    async def list_remote_versions_page(
        self,
        page: int = 0,
        page_size: int = 20,
        start_index: int = 0,
    ) -> tuple[list[DiffVersionInfo], int]:
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            page_filenames, total_versions, start, _end = await self._get_remote_version_page_filenames(
                conn,
                page=page,
                page_size=page_size,
                start_index=start_index,
            )
            if total_versions == 0:
                return [], 0
            versions = await self._load_remote_versions_by_filenames(conn, page_filenames)

        page_infos: list[DiffVersionInfo] = []
        for relative_index, version, filename in zip(range(start, start + len(versions)), versions, page_filenames):
            page_infos.append(
                DiffVersionInfo(
                    uid=version.uid,
                    label=self._format_version_label(version),
                    dt=version.dt.isoformat(),
                    author=version.author,
                    message=version.message,
                    index=start_index + relative_index,
                    filename=filename,
                )
            )

        return page_infos, total_versions

    async def list_version_changes(self, base_version_uid: str, target_version_uid: str) -> list[DiffFileChange]:
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            version_filenames = await self._get_remote_version_filenames(conn)
            base_filename = self._find_remote_version_filename(version_filenames, base_version_uid)
            target_filename = self._find_remote_version_filename(version_filenames, target_version_uid)
            if base_filename is None:
                raise ValueError(f'Unknown base version uid {base_version_uid}')
            if target_filename is None:
                raise ValueError(f'Unknown target version uid {target_version_uid}')

            base_index = version_filenames.index(base_filename)
            target_index = version_filenames.index(target_filename)
            if base_index >= target_index:
                raise ValueError('Target version must be later than base version')

            base_version, target_version = await self._load_remote_versions_by_filenames(conn, [base_filename, target_filename])

        if base_version is None:
            raise ValueError(f'Unknown base version uid {base_version_uid}')
        if target_version is None:
            raise ValueError(f'Unknown target version uid {target_version_uid}')

        migration = base_version.filemap.migrate_to(target_version.filemap)
        return self._build_file_actions_with_entries(
            migration,
            base_version.filemap,
            target_version.filemap,
            created_action='create',
            deleted_action='delete',
        )

    async def list_version_changes_by_filenames(self, base_filename: str, target_filename: str) -> list[DiffFileChange]:
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            base_version, target_version = await self._load_remote_versions_by_filenames(conn, [base_filename, target_filename])

        migration = base_version.filemap.migrate_to(target_version.filemap)
        return self._build_file_actions_with_entries(
            migration,
            base_version.filemap,
            target_version.filemap,
            created_action='create',
            deleted_action='delete',
        )

    async def get_version_change_detail(self, base_version_uid: str, target_version_uid: str, path: str) -> DiffDetail:
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            version_filenames = await self._get_remote_version_filenames(conn)
            base_filename = self._find_remote_version_filename(version_filenames, base_version_uid)
            target_filename = self._find_remote_version_filename(version_filenames, target_version_uid)
            if base_filename is None:
                raise ValueError(f'Unknown base version uid {base_version_uid}')
            if target_filename is None:
                raise ValueError(f'Unknown target version uid {target_version_uid}')

            base_index = version_filenames.index(base_filename)
            target_index = version_filenames.index(target_filename)
            if base_index >= target_index:
                raise ValueError('Target version must be later than base version')

            selected_filenames = version_filenames[base_index:target_index + 1]
            versions = await self._load_remote_versions_by_filenames(conn, selected_filenames)
            migration_cache: dict[str, MigrationChanges] = {}
            version_map = {version.uid: version for version in versions}
            if base_version_uid not in version_map:
                raise ValueError(f'Unknown base version uid {base_version_uid}')
            if target_version_uid not in version_map:
                raise ValueError(f'Unknown target version uid {target_version_uid}')
            base_version = version_map[base_version_uid]
            target_version = version_map[target_version_uid]
            base_index = versions.index(base_version)
            target_index = versions.index(target_version)
            if base_index >= target_index:
                raise ValueError('Target version must be later than base version')

            migration = base_version.filemap.migrate_to(target_version.filemap)
            file_action = self._find_file_action(
                self._build_file_actions(migration, created_action='create', deleted_action='delete'),
                path,
            )
            before_entry = base_version.filemap.get_file(path)
            after_entry = target_version.filemap.get_file(path)
            if self._is_large_diff_file(before_entry, after_entry):
                return DiffDetail(
                    path=path,
                    action=file_action.action,
                    before_label=base_version.uid[:8],
                    after_label=target_version.uid[:8],
                    before_entry=self._entry_asdict(before_entry),
                    after_entry=self._entry_asdict(after_entry),
                    is_large=True,
                    text_available=False,
                    message='Viewing textual diff for large files is unavailable.',
                )

            before_text = await self._get_remote_version_file_text(conn, versions, base_index, path, migration_cache) if file_action.action != 'create' else ''
            after_text = await self._get_remote_version_file_text(conn, versions, target_index, path, migration_cache) if file_action.action != 'delete' else ''
            if before_text is None or after_text is None:
                return DiffDetail(
                    path=path,
                    action=file_action.action,
                    before_label=base_version.uid[:8],
                    after_label=target_version.uid[:8],
                    before_entry=self._entry_asdict(before_entry),
                    after_entry=self._entry_asdict(after_entry),
                    text_available=False,
                    message='Server downgrade snapshot is unavailable for one of the selected versions.',
                )

            return DiffDetail(
                path=path,
                action=file_action.action,
                before_label=base_version.uid[:8],
                after_label=target_version.uid[:8],
                before_text=before_text,
                after_text=after_text,
                before_entry=self._entry_asdict(before_entry),
                after_entry=self._entry_asdict(after_entry),
            )

    async def get_version_change_detail_by_filenames(self, base_filename: str, target_filename: str, path: str) -> DiffDetail:
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            base_index = await self._get_remote_version_index_by_filename(conn, base_filename)
            target_index = await self._get_remote_version_index_by_filename(conn, target_filename)
            if base_index is None:
                raise ValueError(f'Unknown base version filename {base_filename}')
            if target_index is None:
                raise ValueError(f'Unknown target version filename {target_filename}')
            if base_index >= target_index:
                raise ValueError('Target version must be later than base version')

            selected_filenames = await self._get_remote_version_filenames_in_range(conn, base_index, target_index)
            versions = await self._load_remote_versions_by_filenames(conn, selected_filenames)
            migration_cache: dict[str, MigrationChanges] = {}
            version_map = {version.filename(): version for version in versions}
            if base_filename not in version_map:
                raise ValueError(f'Unknown base version filename {base_filename}')
            if target_filename not in version_map:
                raise ValueError(f'Unknown target version filename {target_filename}')
            base_version = version_map[base_filename]
            target_version = version_map[target_filename]
            base_index = versions.index(base_version)
            target_index = versions.index(target_version)
            if base_index >= target_index:
                raise ValueError('Target version must be later than base version')

            migration = base_version.filemap.migrate_to(target_version.filemap)
            file_action = self._find_file_action(
                self._build_file_actions(migration, created_action='create', deleted_action='delete'),
                path,
            )
            before_entry = base_version.filemap.get_file(path)
            after_entry = target_version.filemap.get_file(path)
            if self._is_large_diff_file(before_entry, after_entry):
                return DiffDetail(
                    path=path,
                    action=file_action.action,
                    before_label=base_version.uid[:8],
                    after_label=target_version.uid[:8],
                    before_entry=self._entry_asdict(before_entry),
                    after_entry=self._entry_asdict(after_entry),
                    is_large=True,
                    text_available=False,
                    message='Viewing textual diff for large files is unavailable.',
                )

            before_text = await self._get_remote_version_file_text(conn, versions, base_index, path, migration_cache) if file_action.action != 'create' else ''
            after_text = await self._get_remote_version_file_text(conn, versions, target_index, path, migration_cache) if file_action.action != 'delete' else ''
            if before_text is None or after_text is None:
                return DiffDetail(
                    path=path,
                    action=file_action.action,
                    before_label=base_version.uid[:8],
                    after_label=target_version.uid[:8],
                    before_entry=self._entry_asdict(before_entry),
                    after_entry=self._entry_asdict(after_entry),
                    text_available=False,
                    message='Server downgrade snapshot is unavailable for one of the selected versions.',
                )

            return DiffDetail(
                path=path,
                action=file_action.action,
                before_label=base_version.uid[:8],
                after_label=target_version.uid[:8],
                before_text=before_text,
                after_text=after_text,
                before_entry=self._entry_asdict(before_entry),
                after_entry=self._entry_asdict(after_entry),
            )

    async def get_version_change_detail_by_range(
        self,
        base_filename: str,
        target_filename: str,
        base_index: int | None,
        target_index: int | None,
        path: str,
    ) -> DiffDetail:
        if base_index is None:
            raise ValueError('Base version is missing index information')
        if target_index is None:
            raise ValueError('Target version is missing index information')
        if base_index >= target_index:
            raise ValueError('Target version must be later than base version')

        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            selected_filenames = await self._get_remote_version_filenames_in_range(conn, base_index, target_index)
            versions = await self._load_remote_versions_by_filenames(conn, selected_filenames)
            migration_cache: dict[str, MigrationChanges] = {}
            version_map = {version.filename(): version for version in versions}
            if base_filename not in version_map:
                raise ValueError(f'Unknown base version filename {base_filename}')
            if target_filename not in version_map:
                raise ValueError(f'Unknown target version filename {target_filename}')

            base_version = version_map[base_filename]
            target_version = version_map[target_filename]
            base_range_index = versions.index(base_version)
            target_range_index = versions.index(target_version)
            if base_range_index >= target_range_index:
                raise ValueError('Target version must be later than base version')

            migration = base_version.filemap.migrate_to(target_version.filemap)
            file_action = self._find_file_action(
                self._build_file_actions(migration, created_action='create', deleted_action='delete'),
                path,
            )
            before_entry = base_version.filemap.get_file(path)
            after_entry = target_version.filemap.get_file(path)
            if self._is_large_diff_file(before_entry, after_entry):
                return DiffDetail(
                    path=path,
                    action=file_action.action,
                    before_label=base_version.uid[:8],
                    after_label=target_version.uid[:8],
                    before_entry=self._entry_asdict(before_entry),
                    after_entry=self._entry_asdict(after_entry),
                    is_large=True,
                    text_available=False,
                    message='Viewing textual diff for large files is unavailable.',
                )

            before_text = await self._get_remote_version_file_text(conn, versions, base_range_index, path, migration_cache) if file_action.action != 'create' else ''
            after_text = await self._get_remote_version_file_text(conn, versions, target_range_index, path, migration_cache) if file_action.action != 'delete' else ''
            if before_text is None or after_text is None:
                return DiffDetail(
                    path=path,
                    action=file_action.action,
                    before_label=base_version.uid[:8],
                    after_label=target_version.uid[:8],
                    before_entry=self._entry_asdict(before_entry),
                    after_entry=self._entry_asdict(after_entry),
                    text_available=False,
                    message='Server downgrade snapshot is unavailable for one of the selected versions.',
                )

            return DiffDetail(
                path=path,
                action=file_action.action,
                before_label=base_version.uid[:8],
                after_label=target_version.uid[:8],
                before_text=before_text,
                after_text=after_text,
                before_entry=self._entry_asdict(before_entry),
                after_entry=self._entry_asdict(after_entry),
            )

    @staticmethod
    def _normalize_private_key_path(private_key: str | None) -> str | None:
        if private_key is None:
            return None
        return os.path.expandvars(os.path.expanduser(private_key))

    @classmethod
    def _build_auth_kwargs(
        cls,
        password: str | None = None,
        private_key: str | None = None,
        private_key_passphrase: str | None = None,
    ) -> dict[str, typing.Any]:
        auth_kwargs: dict[str, typing.Any] = {}
        if password:
            auth_kwargs['password'] = password

        normalized_key = cls._normalize_private_key_path(private_key)
        if normalized_key:
            auth_kwargs['client_keys'] = [normalized_key]

        if private_key_passphrase:
            auth_kwargs['passphrase'] = private_key_passphrase

        return auth_kwargs

    def _build_connect_kwargs(
        self,
        host: str,
        port: int,
        username: str,
        auth_kwargs: dict[str, typing.Any] | None = None,
    ) -> dict[str, typing.Any]:
        connect_kwargs: dict[str, typing.Any] = {
            'host': host,
            'port': port,
            'username': username,
            'known_hosts': None,
        }
        if auth_kwargs:
            connect_kwargs.update(auth_kwargs)
        return connect_kwargs

    def _get_restart_container_auth_kwargs(self) -> dict[str, typing.Any]:
        restart_container = self.restart_container or {}
        password = restart_container.get('password', self.password)
        private_key = restart_container.get(
            'private_key',
            restart_container.get('ssh_key', self.private_key),
        )
        private_key_passphrase = restart_container.get(
            'private_key_passphrase',
            restart_container.get('ssh_key_passphrase', self.private_key_passphrase),
        )
        return self._build_auth_kwargs(
            password=password,
            private_key=private_key,
            private_key_passphrase=private_key_passphrase,
        )

    def _get_restart_container_connect_kwargs(self) -> dict[str, typing.Any]:
        if self.restart_container is None:
            raise ValueError('restart_container is not configured')

        restart_container = self.restart_container
        return self._build_connect_kwargs(
            host=restart_container.get('host', self.host),
            port=int(restart_container.get('port', self.port)),
            username=restart_container.get('username', self.username),
            auth_kwargs=self._get_restart_container_auth_kwargs(),
        )

    def _restart_container_uses_main_connection(self) -> bool:
        if self.restart_container is None:
            return False

        restart_connect_kwargs = self._get_restart_container_connect_kwargs()
        return (
            restart_connect_kwargs['host'] == self.host
            and restart_connect_kwargs['port'] == self.port
            and restart_connect_kwargs['username'] == self.username
        )

    def _get_restart_container_sudo_password(self) -> str | None:
        if self.restart_container is None:
            return None

        sudo_password = self.restart_container.get(
            'sudo_password',
            self.restart_container.get('password', self.password),
        )
        if sudo_password:
            return sudo_password
        if self._runtime_restart_sudo_password:
            return self._runtime_restart_sudo_password
        if self.callbacks.secret is None:
            return None

        provided_password = self.callbacks.secret('Sudo password for Docker host')
        normalized_password = provided_password.rstrip('\r\n')
        if normalized_password:
            self._runtime_restart_sudo_password = normalized_password
            return normalized_password
        return None

    def _build_restart_container_docker_cmd(self, docker_subcommand: str) -> str:
        if self.restart_container is None:
            raise ValueError('restart_container is not configured')

        docker_cmd = f'docker {docker_subcommand} {shlex.quote(self.restart_container["container_name"])}'
        return self._wrap_restart_container_command_for_sudo(docker_cmd)

    def _wrap_restart_container_command_for_sudo(self, command: str) -> str:
        if self.restart_container is None:
            raise ValueError('restart_container is not configured')

        if not self.restart_container.get('sudo', False):
            return command

        sudo_password = self._get_restart_container_sudo_password()
        if sudo_password:
            return f"printf '%s\\n' {shlex.quote(sudo_password)} | sudo -S -k -p '' -- {command}"

        return f'sudo -n -- {command}'

    @staticmethod
    def _command_error_text(result) -> str:
        return result.stderr.strip() or result.stdout.strip() or 'command failed'

    async def _run_restart_container_diagnostics(self, conn: SSHClientConnection) -> None:
        docker_check = await conn.run('command -v docker >/dev/null 2>&1', check=False)
        if docker_check.exit_status != 0:
            raise RuntimeError('restart_container diagnostic failed: docker is not installed or is not available in PATH on the Docker host')

        if not self.restart_container or not self.restart_container.get('sudo', False):
            return

        sudo_check = await conn.run(self._wrap_restart_container_command_for_sudo('true'), check=False)
        if sudo_check.exit_status == 0:
            return

        error_text = self._command_error_text(sudo_check)
        lowered_error = error_text.lower()
        if 'sorry, try again' in lowered_error or 'no password was provided' in lowered_error:
            raise RuntimeError(
                'restart_container sudo check failed: sudo password was rejected or not accepted by sudo on the Docker host'
            )
        if 'a password is required' in lowered_error:
            raise RuntimeError(
                'restart_container sudo check failed: sudo requires a password on the Docker host. Set restart_container.sudo_password or use the interactive password prompt.'
            )
        if 'tty' in lowered_error:
            raise RuntimeError(
                f'restart_container sudo check failed: {error_text}. The Docker host may require a tty for sudo.'
            )
        raise RuntimeError(f'restart_container sudo check failed: {error_text}')

    async def _test_restart_container(self):
        if self.restart_container is None:
            return

        console.print('Connect to Docker host...', style='yellow')
        async with asyncssh.connect(**self._get_restart_container_connect_kwargs()) as conn:
            clear_n_console_rows(1)
            console.print('Run Docker host diagnostics...', style='yellow')
            await self._run_restart_container_diagnostics(conn)
            clear_n_console_rows(1)
            console.print('Check docker container...', style='yellow')
            result = await conn.run(
                self._build_restart_container_docker_cmd('inspect --type container'),
                check=False,
            )
            clear_n_console_rows(1)
            if result.exit_status != 0:
                stderr = self._command_error_text(result)
                raise RuntimeError(
                    f'restart_container check failed for {self.restart_container["container_name"]}: {stderr}'
                )
            console.print('Docker host connection ok', style='green')

    async def test_connection(self):
        console.print('Connect to remote host...', style='yellow')
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            result = await conn.run('true', check=False)
            clear_n_console_rows(1)
            if result.exit_status != 0:
                stderr = result.stderr.strip() or result.stdout.strip() or 'ssh command failed'
                raise RuntimeError(f'remote host check failed: {stderr}')
            console.print('Remote host connection ok', style='green')

        if self.restart_container is not None and not self._restart_container_uses_main_connection():
            await self._test_restart_container()
        
    async def _load_or_create_prevstate(self) -> FileMap:
        prevstate = await self._load_prevstate()
        if prevstate is None:
            prevstate = await self.filewatcher.get_filemap()
            await self._save_prevstate(prevstate)
            return prevstate
        return prevstate

    async def _load_prevstate(self) -> FileMap | None:
        if not os.path.exists(self.prevstate_file):
            return None
        async with aiofiles.open(self.prevstate_file, 'r') as f:
            return FileMap.loads(await f.read())
    
    async def _save_prevstate(self, prevstate: FileMap):
        assert isinstance(prevstate, FileMap)
        await write_text_file_atomic_async(self.prevstate_file, prevstate.dumps())

    async def _load_stash_metadata(self) -> dict[str, typing.Any]:
        async with aiofiles.open(self.stash_metadata_file, 'r', encoding='utf-8') as f:
            return json.loads(await f.read())

    @staticmethod
    def _stash_changes_from_metadata(metadata: dict[str, typing.Any]) -> MigrationChanges:
        return MigrationChanges.model_validate(
            {
                'directories': metadata['directories'],
                'files': metadata['files'],
            }
        )

    @staticmethod
    def _stash_base_filemap_from_metadata(metadata: dict[str, typing.Any]) -> FileMap | None:
        base_filemap = metadata.get('base_filemap')
        if base_filemap is None:
            return None
        return FileMap.from_dict(base_filemap)

    async def _save_stash_metadata(self, migration: Migration, base_filemap: FileMap):
        pathlib.Path(self.stash_directory).mkdir(parents=True, exist_ok=True)
        data = {
            'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'base_filemap': base_filemap.asdict(),
            'directories': migration.dirs.model_dump(),
            'files': migration.files.model_dump(),
        }
        await write_text_file_atomic_async(self.stash_metadata_file, json.dumps(data, indent=4), encoding='utf-8')

    async def _clear_stash(self):
        if os.path.isdir(self.stash_directory):
            shutil.rmtree(self.stash_directory)

    async def _resolve_stash_conflicts(self, conflicts: Conflicts):
        copied: list[str] = []
        not_found: list[str] = []

        for file in conflicts.files:
            source = pathlib.Path(self.stash_files_directory, file)
            target = pathlib.Path(file).with_stem(pathlib.Path(file).stem + conflicts.local_suffix)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                await aioshutil.copy(source.as_posix(), target.as_posix())
                copied.append(target.as_posix())
            except FileNotFoundError:
                conflicts.remove(file)
                not_found.append(file)

        console.print('Conflicts', style='orange_red1 bold')
        if len(copied) > 0:
            console.print('  Copied:', style='yellow bold')
            print('\n'.join([' '*4 + f for f in copied]))
        if len(not_found) > 0:
            console.print('  Not found files:', style='yellow bold')
            print('\n'.join([' '*4 + f for f in not_found]))

        await write_text_file_atomic_async(self.conflicts_file, conflicts.dumps())

    async def _stash_local_changes(self, migration: Migration, base_filemap: FileMap):
        await self._clear_stash()
        pathlib.Path(self.stash_files_directory).mkdir(parents=True, exist_ok=True)

        for directory in migration.dirs.created:
            pathlib.Path(self.stash_files_directory, directory).mkdir(parents=True, exist_ok=True)

        for file in migration.files.created + migration.files.changed:
            target = pathlib.Path(self.stash_files_directory, file)
            target.parent.mkdir(parents=True, exist_ok=True)
            await aioshutil.copy(file, target.as_posix())

        await self._save_stash_metadata(migration, base_filemap)

    async def stash_changes(self):
        self._refresh_ignore_file_path()
        if _has_stashed_changes():
            console.print('Stashed changes already exist. Restore them before creating a new stash.', style='yellow')
            return

        if not await self._validate_conflicts():
            return

        prevstate = await self._load_or_create_prevstate()
        state = await self.filewatcher.get_filemap(reference_map=prevstate)
        local_migration = prevstate.migrate_to(state)
        if local_migration.empty():
            console.print('No local changes to stash', style='yellow')
            await self.force_pull(require_confirm=False)
            return

        console.print('Stash changes', style='green bold')
        console.print('The following local changes will be stashed and the project will be synced from remote:', style='yellow')
        local_migration.print_actions(prefix='  stash > ')
        if not self._confirm('Continue with stashing local changes?', 'Stash cancelled by user'):
            return

        await self._stash_local_changes(local_migration, prevstate)
        await self.force_pull(require_confirm=False)
        console.print('Local changes stashed and project synced from remote', style='green')

    async def restore_stash(self):
        self._refresh_ignore_file_path()
        if not _has_stashed_changes():
            console.print('No stashed changes found', style='yellow')
            return

        if not await self._validate_conflicts():
            return

        stash_metadata = await self._load_stash_metadata()
        stash = self._stash_changes_from_metadata(stash_metadata)
        stash_base = self._stash_base_filemap_from_metadata(stash_metadata)
        console.print('Restore stashed changes', style='green bold')
        stash.print()
        if stash_base is None:
            console.print('Stash was created by an older sshmirror version, so conflict detection is unavailable for this restore.', style='yellow')

        conflicts = None
        if stash_base is not None:
            current_state = await self.filewatcher.get_filemap()
            current_migration = stash_base.migrate_to(current_state)
            conflicts = Conflicts(
                remote_version_uid=stash_metadata.get('created_at', 'stash'),
                files=[path for path in current_migration.files.all() if path in stash.files.created + stash.files.changed],
                dirs=[],
                local_suffix='._stash',
            )

        if conflicts is not None and not conflicts.empty():
            console.print('Conflicts detected with files updated since the stash was created', style='orange_red1')
            print('\n'.join(['  ' + path for path in conflicts.files + conflicts.dirs]))

        if not self._confirm('Apply stashed changes?', 'Restore stash cancelled by user'):
            return

        if conflicts is not None and not conflicts.empty():
            await self._resolve_stash_conflicts(conflicts)

        for file in stash.files.deleted:
            if os.path.exists(file):
                os.remove(file)

        for directory in sorted(stash.directories.deleted, key=len, reverse=True):
            if os.path.isdir(directory):
                shutil.rmtree(directory)

        for directory in stash.directories.created:
            pathlib.Path(directory).mkdir(parents=True, exist_ok=True)

        for file in stash.files.created + stash.files.changed:
            if conflicts is not None and file in conflicts.files:
                continue
            source = pathlib.Path(self.stash_files_directory, file)
            pathlib.Path(file).parent.mkdir(parents=True, exist_ok=True)
            await aioshutil.copy(source.as_posix(), file)

        await self._clear_stash()
        console.print('Stashed changes restored', style='green')

    async def status(self):
        self._refresh_ignore_file_path()
        console.print('Status', style='green bold')
        console.print(f'  initialized: {_is_sshmirror_initialized()}', style='cyan')
        console.print(f'  stash: {_has_stashed_changes()}', style='cyan')
        console.print(f'  conflicts: {os.path.exists(self.conflicts_file)}', style='cyan')

        prevstate = await self._load_prevstate()
        local_state = await self.filewatcher.get_filemap(reference_map=prevstate)
        if prevstate is None:
            console.print('  local sync state: not created yet', style='yellow')
        else:
            local_migration = prevstate.migrate_to(local_state)
            console.print('Local changes:', style='yellow')
            if local_migration.empty():
                console.print('  no local changes', style='green')
            else:
                local_migration.print_actions(prefix='  local > ')

        console.print('Connect to remote...', style='yellow')
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            local_version = await self._get_local_version()
            try:
                remote_versions = await self._get_remote_versions_stack(conn, start_version=local_version)
            except ErrorLocalVersion:
                remote_versions = await self._get_remote_versions_stack(conn)
            remote_reference = remote_versions[-1].filemap if len(remote_versions) > 0 else local_version.filemap if local_version else None
            remote_map = await self._get_remote_map(conn, reference_map=remote_reference)

        if local_version is None:
            console.print('  local version state: not initialized', style='yellow')
        elif len(remote_versions) <= 1:
            console.print('Remote changes:', style='yellow')
            console.print('  no remote changes since local version', style='green')
        else:
            remote_migration = remote_versions[0].filemap.migrate_to(remote_versions[-1].filemap)
            console.print('Remote changes:', style='yellow')
            remote_migration.print_actions(prefix='  remote > ')

        live_diff = local_state.migrate_to(remote_map)
        console.print('Live local vs remote:', style='yellow')
        if live_diff.empty():
            console.print('  projects are equal', style='green')
        else:
            live_diff.print_actions(prefix='  diff > ')
            
    async def run(self):
        self._refresh_ignore_file_path()
        console.print('Connect to remote...', style='yellow')
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            clear_n_console_rows(1)
            
            
            if self.discard_files is not None:
                await self._download_files(conn, self.discard_files, '  remote > update', style='violet')
                return
            
            console.print('Check exists no resolved conflicts...', style='yellow')
            if not await self._validate_conflicts():
                return 
            clear_n_console_rows(1)

            await self._sync_ignore_file_before_transfer(conn)
            
            console.print('Look local changes...', style='yellow')
            prevstate = await self._load_or_create_prevstate()
            state, _ = await asyncio.gather(
                self.filewatcher.get_filemap(reference_map=prevstate),
                self._remote_mk_dir(conn, self.versions_directory),
            )
            
            local_migration = prevstate.migrate_to(state)
            clear_n_console_rows(1)
            
            if self.downgrade:
                if not local_migration.empty():
                    console.print('Before downgrade push or discard changes!', style='red')
                    return
                versions = await self._get_remote_versions_stack(conn, last_n=2)
                migration_changes = await self._get_remote_migration_changes(conn, versions[-1])
                migration_changes.inversed().print()
                if not self._confirm(
                    f'Do you want switch to version {versions[-2].dt.isoformat()} UTC?',
                    'Downgrade cancelled by user',
                ):
                    return

                await conn.run(f'sh {self.remotedir}{self.migrations_directory}/{versions[-1].name()}/_downgrade.sh')
                try:
                    os.remove(f'{self.versions_directory}/{versions[-1].filename()}')
                except FileNotFoundError:
                    pass
                await self.force_pull()
                return
            
            # Check file sizes
            large_files = []
            for path in state.path_list():
                size = os.path.getsize(path)
                if size > self.WARNING_SIZE:
                    large_files.append(path)
                    
            if os.path.exists(self.filesize_ignore_file):
                sizeignores = set(map(lambda x: x.strip(), read_text_file(self.filesize_ignore_file).splitlines()))
                large_files = list(set(large_files).difference(sizeignores))
            
            # Large files
            if len(large_files) > 0:
                for path in large_files:
                    size_mb = int(os.path.getsize(path) / 1024 / 1024)
                    console.print(f'WARNING! Large file size={size_mb}Mb path="{path}"', style='yellow')
                    
                if not self._confirm(
                    'Add large files to ignore size list?',
                    'Large files were not added to sshmirror.ignoresize.txt',
                ):
                    console.print('Add this files to sshmirror.ignore.txt and try again', style='yellow')
                    raise UserAbort('Large files were not added to sshmirror.ignoresize.txt')
                
                with open(self.filesize_ignore_file, 'a') as f:
                    f.write('\n'.join(large_files) + '\n')
            
            console.print('Look remote changes...', style='yellow')
            while True:
                try:
                    local_version = await self._get_local_version()    
                    remote_versions = await self._get_remote_versions_stack(conn, start_version=local_version)
                    break
                except ErrorLocalVersion:
                    os.remove(os.path.join(self.versions_directory, local_version.filename()))
            clear_n_console_rows(1)
            
            if local_version is None:
                remote_reference = remote_versions[-1].filemap if len(remote_versions) > 0 else None
                remote_map = await self._get_remote_map(conn, reference_map=remote_reference)
                newer_source = self._choose(
                    'Which version is newer?',
                    ['My local version', 'Remote server version'],
                    'Initialization source is required',
                )
                if newer_source == 'Remote server version':
                    migration = state.migrate_to(remote_map)
                    if len(remote_versions) == 0:
                        remote_version = self._create_version(remote_map)
                        await self._set_remote_version(remote_version, conn)
                        remote_versions = [remote_version]
                    empty_conflicts = Conflicts(remote_version_uid=remote_versions[-1].uid, files=[], dirs=[])
                    await self._pull(remote_versions, migration, empty_conflicts, conn)
                else:
                    migration = remote_map.migrate_to(state)
                    version = self._create_version(state)
                    await self._push(version, migration, conn)
                return
            
            if len(remote_versions) > 1:
                # Remote changed by other contributer
                remote_migration = remote_versions[0].filemap.migrate_to(remote_versions[-1].filemap)
                
                if not remote_migration.empty():
                    conflicts = remote_migration.conflicts(remote_versions[-1], local_migration)
                    await self._pull(remote_versions[1:], remote_migration, conflicts, conn)
                    if not conflicts.empty():
                        return
                    prevstate = await self._load_or_create_prevstate()
                    state = await self.filewatcher.get_filemap(reference_map=prevstate)
                    local_migration = prevstate.migrate_to(state)

            if not self.pull_only and not local_migration.empty():
                # Push changes
                version = self._create_version(state)
                if len(remote_versions) > 0 and version.dt <= remote_versions[-1].dt:
                    raise ValueError(f'Push version timestamp less than last version timestamp on remote ({version.dt} < {remote_versions[-1].dt})')
                await self._push(version, local_migration, conn)

            console.print('Compare remote and local projects...', style='yellow')
            remote_reference = remote_versions[-1].filemap if len(remote_versions) > 0 else local_version.filemap if local_version else None
            local_map, remote_map = await asyncio.gather(
                self.filewatcher.get_filemap(reference_map=state),
                self._get_remote_map(conn, reference_map=remote_reference),
            )
            migration = local_map.migrate_to(remote_map)
            clear_n_console_rows(1)
            
            if not migration.empty():
                console.print('Remote and local projects has differences!!!', style='red')
                migration.print()
                empty_conflicts = Conflicts(remote_version_uid=remote_versions[-1].uid, files=[], dirs=[])

                sync_action = self._choose(
                    'Projects differ after sync. Choose resolution.',
                    ['Pull', 'Push'],
                    'Sync resolution is required',
                )
                if sync_action == 'Push':
                    await self._push(self._create_version(local_map), remote_map.migrate_to(local_map), conn)
                else:
                    await self._pull(remote_versions, migration, empty_conflicts, conn, ignore_version_exists=True)
                
            console.print('Projects are equal', style='green')
            
        # Restart container
        if self.restart_container is not None:
            if self._confirm('Restart docker container?', 'Restart container choice is required'):
                clear_n_console_rows(1)
                console.print('Connecting...', style='yellow')
                async with asyncssh.connect(**self._get_restart_container_connect_kwargs()) as conn:
                    clear_n_console_rows(1)
                    console.print('Restarting...', style='yellow')
                    await conn.run(self._build_restart_container_docker_cmd('restart'))
                    clear_n_console_rows(1)
                    console.print('Container restarted', style='green')
            else:
                clear_n_console_rows(1)
                    
            
    async def force_pull(self, require_confirm: bool = True):
        self._refresh_ignore_file_path()
        if not await self._validate_conflicts():
            return
        if require_confirm:
            if not self._confirm(
                'WARNING! This operation cleans all your changes. Overwrite local files from remote?',
                'Force pull cancelled by user',
            ):
                return
        console.print('Connect to remote...', style='yellow')
        async with asyncssh.connect(**self._build_connect_kwargs(
            host=self.host,
            port=self.port,
            username=self.username,
            auth_kwargs=self.auth_kwargs,
        )) as conn:
            await self._sync_ignore_file_before_transfer(conn)
            clear_n_console_rows(1)
            console.print('Get local and remote versions...', style='yellow')
            local_versions, remote_versions = await asyncio.gather(self._get_local_versions_stack(), self._get_remote_versions_stack(conn))
            remote_versions_set = {rv.filename() for rv in remote_versions}
            clear_n_console_rows(1)
            console.print('Get local and remote states...', style='yellow')
            prevstate = await self._load_prevstate()
            remote_reference = remote_versions[-1].filemap if len(remote_versions) > 0 else None
            local_map, remote_map = await asyncio.gather(
                self.filewatcher.get_filemap(reference_map=prevstate),
                self._get_remote_map(conn, reference_map=remote_reference),
            )
            clear_n_console_rows(1)
            console.print('Remove local versions...', style='yellow')
            count = 0
            for local_version in local_versions:
                if local_version.filename() not in remote_versions_set:
                    os.remove(os.path.join(self.versions_directory, local_version.filename()))
                    count += 1
            clear_n_console_rows(1)
            if count > 0:
                console.print(f'Removed {count} versions', style='yellow')
            
            migration = remote_map.migrate_to(local_map)
            await self._pull(remote_versions, migration, None, conn, ignore_version_exists=True, require_confirm=require_confirm)
            await self._save_prevstate(remote_map)
            
    
    async def _load_conflicts(self) -> typing.Optional[Conflicts]:
        if not os.path.exists(self.conflicts_file):
            return
        async with aiofiles.open(self.conflicts_file, 'r') as f:
            s = await f.read()
        return Conflicts.loads(s)
    
    async def _resolve_conflicts(self, conflicts: Conflicts):
        copied: list[str] = []
        not_found: list[str] = []
        async def copy(src: str, dst: str):
            try:
                await aioshutil.copy(src, dst)
                copied.append(dst)
            except FileNotFoundError:
                conflicts.remove(src)
                not_found.append(src)
                
        for file in conflicts.files:
            p = pathlib.Path(file)
            dst = p.with_stem(p.stem + conflicts.local_suffix)
            await copy(file, dst.as_posix())
        
        console.print('Conflicts', style='orange_red1 bold')
        if len(copied) > 0:
            console.print('  Copied:', style='yellow bold')
            print('\n'.join([' '*4 + f for f in copied]))
        if len(not_found) > 0:
            console.print('  Not found files:', style='yellow bold')
            print('\n'.join([' '*4 + f for f in not_found]))
        
        assert isinstance(conflicts, Conflicts)
        await write_text_file_atomic_async(self.conflicts_file, conflicts.dumps())
            
    async def _validate_conflicts(self):
        conflicts = await self._load_conflicts()
        if conflicts is None:
            return True
        local_files = []
        for file in conflicts.files:
            p = pathlib.Path(file)
            local_file = p.with_stem(p.stem + conflicts.local_suffix)
            if os.path.exists(local_file):
                local_files.append(local_file.as_posix())
        if len(local_files) > 0:
            print('Please resolve conflicts and remove files:')
            print('\n'.join(['  ' + f for f in local_files]))
            return False
        os.remove(self.conflicts_file)
        return True

    async def _pull(self, remote_versions: list[DirVersion], migration: Migration, conflicts: Conflicts | None, conn: SSHClientConnection,
                    ignore_version_exists=False, require_confirm: bool = True):
        self._render_sync_plan(
            'Pull',
            'These changes will be applied to the local project.',
            migration,
            conflicts=conflicts,
            border_style='green',
        )
        
        if require_confirm:
            if not self._confirm('Apply pull changes?', 'Pull cancelled by user'):
                raise UserAbort('Pull cancelled by user')
        
        await self._run_commands(self.commands.before_pull, migration, conn)
            
        if conflicts is not None and not conflicts.empty():
            await self._resolve_conflicts(conflicts)
            console.print('Pull continues after preserving conflicted local files', style='green')
        
        for directory in migration.dirs.created:
            pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
            console.print('  remote > create directory', style='green')
        for directory in migration.dirs.deleted:
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass
            console.print('  remote > delete directory', directory, style='red')
                
        for file in migration.files.deleted:
            if os.path.exists(file):
                os.remove(file)
            console.print('  remote > delete', file, style='red')
        
        await self._download_files(conn, migration.files.created, '  remote > create', style='green')
        await self._download_files(conn, migration.files.changed, '  remote > update', style='violet')
        
        tasks = [self._save_version(version, ignore_error=ignore_version_exists) for version in remote_versions]
        await asyncio.gather(*tasks)
        await self._save_prevstate(remote_versions[-1].filemap)
        
        await self._run_commands(self.commands.after_pull, migration, conn)
        
    async def _push(self, local_version: DirVersion, migration: Migration, conn: SSHClientConnection):
        self._render_sync_plan(
            'Push',
            'These changes will be applied to the remote project.',
            migration,
            border_style='cyan',
        )
        
        if not self._confirm('Apply push changes?', 'Push cancelled by user'):
            raise UserAbort('Push cancelled by user')

        local_version.message = self._prompt_version_message()
        
        await self._remote_create_downgrade(conn, migration, local_version)
        
        await self._run_commands(self.commands.before_push, migration, conn)
        
        await asyncio.gather(*[self._remote_mk_dir(conn, dir, '  remote < make directory') for dir in migration.dirs.created])
        await self._delete_directories(conn, migration.dirs.deleted, '  remote < delete directory')
            
        await self._upload_files(conn, migration.files.changed, '  remote < update', style='violet')
        await self._upload_files(conn, migration.files.created, '  remote < create', style='green')
        await self._delete_files(conn, migration.files.deleted, '  remote < delete')
        
        await self._set_remote_version(local_version, conn)
        await self._save_version(local_version)
        await self._save_prevstate(local_version.filemap)
        
        await self._run_commands(self.commands.after_push, migration, conn)
        
    @staticmethod
    def _parse_cmd_config(cmd_config: dict) -> CmdConfig:
        def parse_command_by_type(runtype) -> list[Command]:
            return [Command(**item) for item in cmd_config.get(runtype, [])]
        
        data = {runtype: parse_command_by_type(runtype) for runtype in ['before_pull', 'after_pull', 'before_push', 'after_push']}
        return CmdConfig(**data)
    
    async def _get_last_remote_version(self, conn: SSHClientConnection) -> typing.Optional[DirVersion]:
        result = await conn.run(f'ls {self.remotedir}{self.versions_directory}')
        if type(result.stdout) == str and result.stdout.strip() == '':
            return None
        versions = result.stdout.strip().split('\n')
        version = sorted(versions, reverse=True)[-1]
        s = (await conn.run(f'cat {self.remotedir}{self.versions_directory}/{version}')).stdout
        return DirVersion.loads(s)
    
    async def _get_remote_version_filenames(self, conn: SSHClientConnection) -> list[str]:
        result = await conn.run(self._build_remote_version_listing_command(), check=False)
        if result.exit_status != 0 or result.stdout.strip() == '':
            return []
        return result.stdout.strip().split('\n')

    def _build_remote_version_listing_command(self) -> str:
        versions_path = shlex.quote(self._remote_get_abs_path(self.versions_directory))
        return f"find {versions_path} -maxdepth 1 -type f -printf '%f\\n' | sort"

    async def _get_remote_version_count(self, conn: SSHClientConnection, start_index: int = 0) -> int:
        listing_command = self._build_remote_version_listing_command()
        if start_index > 0:
            listing_command = f"{listing_command} | sed -n '{start_index + 1},$p'"
        result = await conn.run(f"{listing_command} | wc -l", check=False)
        if result.exit_status != 0 or result.stdout.strip() == '':
            return 0
        return int(result.stdout.strip())

    async def _get_remote_version_filenames_in_range(self, conn: SSHClientConnection, start_index: int, end_index: int) -> list[str]:
        if end_index < start_index:
            return []
        listing_command = self._build_remote_version_listing_command()
        result = await conn.run(
            f"{listing_command} | sed -n '{start_index + 1},{end_index + 1}p'",
            check=False,
        )
        if result.exit_status != 0 or result.stdout.strip() == '':
            return []
        return result.stdout.strip().split('\n')

    async def _get_remote_version_page_filenames(
        self,
        conn: SSHClientConnection,
        page: int,
        page_size: int,
        start_index: int = 0,
    ) -> tuple[list[str], int, int, int]:
        total_versions = await self._get_remote_version_count(conn, start_index=start_index)
        if total_versions == 0:
            return [], 0, 0, 0

        normalized_page = max(0, page)
        end = total_versions - (normalized_page * page_size)
        start = max(0, end - page_size)
        page_filenames = await self._get_remote_version_filenames_in_range(
            conn,
            start_index + start,
            start_index + end - 1,
        )
        return page_filenames, total_versions, start, end

    async def _get_remote_version_index_by_filename(self, conn: SSHClientConnection, filename: str) -> int | None:
        version_filenames = await self._get_remote_version_filenames(conn)
        try:
            return version_filenames.index(filename)
        except ValueError:
            return None

    @staticmethod
    def _find_remote_version_filename(version_filenames: list[str], version_uid: str) -> str | None:
        suffix = f'_{version_uid}.json'
        for filename in version_filenames:
            if filename.endswith(suffix):
                return filename
        return None

    async def _load_remote_versions_by_filenames(self, conn: SSHClientConnection, version_filenames: list[str]) -> list[DirVersion]:
        if len(version_filenames) == 0:
            return []

        sem = asyncio.Semaphore(1)
        async def sem_coro(c: SSHClientConnection, cmd: str):
            async with sem:
                return await c.run(cmd)

        tasks = [sem_coro(conn, f'cat {self.remotedir}{self.versions_directory}/{filename}') for filename in version_filenames]
        results = await asyncio.gather(*tasks)
        versions = []
        for i, result in enumerate(results):
            try:
                versions.append(DirVersion.loads(result.stdout))
            except:
                print(f'WARNING. On remote version file invalid format, {version_filenames[i]}')
        return versions

    async def _get_remote_versions_stack(self, conn: SSHClientConnection, start_version: DirVersion | None = None, last_n: int | None = None) -> list[DirVersion]:
        version_filenames = await self._get_remote_version_filenames(conn)
        if len(version_filenames) == 0:
            return []
        if start_version is not None:
            assert last_n is None
            
        if start_version is not None:
            try:
                start_index = version_filenames.index(start_version.filename())
            except ValueError as e:
                raise ErrorLocalVersion("Local version not exists on remote")
            assert start_index > -1
            version_filenames = version_filenames[start_index:]  
        elif last_n is not None:
            version_filenames = version_filenames[-last_n:]

        return await self._load_remote_versions_by_filenames(conn, version_filenames)
    
    async def _get_remote_migration_changes(self, conn: SSHClientConnection, version: DirVersion) -> MigrationChanges:
        res = await conn.run(f'cat {self.remotedir}{self.migrations_directory}/{version.name()}/_migration.json')
        return MigrationChanges.model_validate_json(res.stdout)
    
    async def _get_local_versions_stack(self) -> list[DirVersion]:
        versions = sorted(os.listdir(self.versions_directory))
        res = []
        for version in versions:
            async with aiofiles.open(os.path.join(self.versions_directory, version), 'r') as f:
                s = await f.read()
            res.append(DirVersion.loads(s))
        return res
    
    async def _get_local_version(self) -> typing.Optional[DirVersion]:
        versions = sorted(os.listdir(self.versions_directory), reverse=True)
        if len(versions) == 0:
            return
        async with aiofiles.open(os.path.join(self.versions_directory, versions[0]), 'r') as f:
            s = await f.read()
        return DirVersion.loads(s)
    
    def _create_version(self, filemap: FileMap):
        return DirVersion(dt=datetime.datetime.now(datetime.timezone.utc), author=self.author, message='update', filemap=filemap)
    
    async def _save_version(self, v: DirVersion, ignore_error=False) -> str:
        path = os.path.join(self.versions_directory, v.filename())
        if os.path.exists(path):
            if ignore_error:
                return path
            raise VersionAlreadyExists(f'This version already exists, {v.filename()}')
        await write_text_file_atomic_async(path, v.dumps())
        return path
            
    async def _set_remote_version(self, v: DirVersion, conn: SSHClientConnection) -> str:
        tmp_path = '.sshmirror/remote.version.tmp'
        remote_path = f'{self.versions_directory}/{v.filename()}'
        async with aiofiles.open(tmp_path, 'w') as f:
            await f.write(v.dumps())
        
        try:
            async with conn.start_sftp_client() as sftp:
                await self._upload_file(sftp, tmp_path, remote_path=self.remotedir + remote_path)
        except Exception as e:
            print(e)
        finally:
            os.remove(tmp_path)
        
        return remote_path

    async def _run_commands(self, commands: list[Command], migration: Migration, conn: SSHClientConnection):
        async def run(command):
            if command.ask:
                if not self._confirm(
                    f'Run command "{command.name or command.local_command or command.remote_command}"?',
                    f'Command confirmation required for {command.name or command.local_command or command.remote_command}',
                ):
                    return
            if command.name:
                print(f'Run "{command.name}"')
            
            await self._run_command_by_type(command, 'local', conn)
            await self._run_command_by_type(command, 'remote', conn)
            
        for command in commands:
            if command.on_directory_change is not None:
                dir_p = re.compile(command.on_directory_change + '(/.*)?')
                for path in migration.files.all() + migration.dirs.all():
                    if dir_p.match(path):
                        await run(command)
                        break
            else:
                await run(command)
                
    @staticmethod
    async def _run_command_by_type(command: Command, type: str, conn: SSHClientConnection):
        commands = getattr(command, f'{type}_command')
        if commands is None:
            return

        for cmd in commands:
            if command.name is None:
                print(f'CMD({type}):', cmd)
            if type == 'remote':
                await conn.run(cmd)
            if type == 'local':
                os.system(cmd)
    
        
    @staticmethod
    def _normalize_remote_mtime(value: str) -> int:
        return int(float(value.strip()) * 1_000_000_000)

    def _build_remote_find_commands(self, ignore_list) -> tuple[str, str]:
        remote_root = self.remotedir.rstrip('/') or '/'
        prune_terms: list[str] = []
        file_excludes: list[str] = []

        for ignore_rule in ignore_list.prunable_component_rules:
            prune_terms.append(f'-name {shlex.quote(ignore_rule.normalized)}')

        for ignore_rule in ignore_list.prunable_path_rules:
            remote_path = os.path.join(remote_root, ignore_rule.normalized).replace('\\', '/')
            prune_terms.append(f'-path {shlex.quote(remote_path)}')

        for ignore_rule in ignore_list.component_rules:
            if ignore_rule.directory_only:
                continue
            file_excludes.append(f'! -name {shlex.quote(ignore_rule.normalized)}')

        for ignore_rule in ignore_list.slash_rules:
            if ignore_rule.directory_only:
                continue
            remote_path = os.path.join(remote_root, ignore_rule.normalized).replace('\\', '/')
            file_excludes.append(f'! -path {shlex.quote(remote_path)}')
            file_excludes.append(f'! -path {shlex.quote(remote_path + "/*")}')

        prune_prefix = ''
        if prune_terms:
            prune_prefix = f"\\( -type d \\( {' -o '.join(prune_terms)} \\) -prune \\) -o "

        file_filters = '' if not file_excludes else ' ' + ' '.join(file_excludes)
        root_arg = shlex.quote(remote_root)
        cmd_files = f"find {root_arg} {prune_prefix}-type f{file_filters} -printf '%P\\t%s\\t%T@\\n'"
        cmd_dirs = f"find {root_arg} {prune_prefix}-type d -printf '%P\\n'"
        return cmd_files, cmd_dirs

    async def _get_remote_file_hashes(self, conn: SSHClientConnection, paths: list[str]) -> dict[str, str]:
        sem = asyncio.Semaphore(4)

        async def calculate(path: str):
            async with sem:
                remote_path = self._remote_get_abs_path(path)
                result = await conn.run(f'md5sum {shlex.quote(remote_path)}')
                md5 = result.stdout.split(' ', 1)[0].strip()
                return path, md5

        pairs = await asyncio.gather(*[calculate(path) for path in paths])
        return {path: md5 for path, md5 in pairs}

    async def _get_remote_map(self, conn: SSHClientConnection, reference_map: FileMap | None = None) -> FileMap:
        ignore_list = compile_ignore_rules(parse_ignore_file(self.ignore_file_path))
        cmd_files, cmd_dirs = self._build_remote_find_commands(ignore_list)
        result_files, result_dirs = await asyncio.gather(conn.run(cmd_files), conn.run(cmd_dirs))
        filemap = FileMap()
        pending_hash_paths: list[str] = []
        pending_stats: dict[str, tuple[int, int]] = {}
        for line in result_files.stdout.split('\n'):
            if line.strip() == '':
                continue
            path, size, mtime = line.split('\t', 2)
            path = path.strip()
            if len(path) == 0:
                continue
            if check_path_is_ignored(path, ignore_list, is_dir=False):
                continue
            normalized_size = int(size)
            normalized_mtime = self._normalize_remote_mtime(mtime)
            reference_entry = reference_map.get_file(path) if reference_map is not None else None
            if reference_entry is not None and reference_entry.stat_matches(normalized_size, normalized_mtime):
                filemap.add(path, reference_entry.md5, size=normalized_size, mtime=normalized_mtime)
                continue
            pending_hash_paths.append(path)
            pending_stats[path] = (normalized_size, normalized_mtime)

        if len(pending_hash_paths) > 0:
            remote_hashes = await self._get_remote_file_hashes(conn, pending_hash_paths)
            for path, md5 in remote_hashes.items():
                size, mtime = pending_stats[path]
                filemap.add(path, md5, size=size, mtime=mtime)
        
        for path in result_dirs.stdout.split('\n'):
            path = path.strip()
            if len(path) == 0:
                continue
            if not check_path_is_ignored(path, ignore_list, is_dir=True):
                filemap.add_directory(path)
    
        return filemap
    
    async def _download_files(self, conn: SSHClientConnection, paths, event_type: str | None = None, style: str | None = None):
        async with conn.start_sftp_client() as sftp:
            tasks = [self._download_file(sftp, path, event_type, style) for path in paths]
            while len(tasks) > 0:
                await asyncio.gather(*tasks[:10])
                tasks = tasks[10:]
            
    async def _download_file(self, sftp: SFTPClient, path, event_type: str | None = None, style: str | None = None):
        remote_path = os.path.join(self.remotedir, path).replace('\\', '/')
        try:
            await sftp.get(remote_path, path)
            if event_type:
                console.print(event_type, path, style=style)
        except Exception as e:
            print('ERROR remote > download', path)
    
    async def _upload_files(self, conn: SSHClientConnection, paths, event_type: str | None = None, style: str | None = None):
        sem = asyncio.Semaphore(10)
        async def sem_coro(sftp, path, event_type, style):
            async with sem:
                return await self._upload_file(sftp, path, event_type, style)
        async with conn.start_sftp_client() as sftp:
            await asyncio.gather(*[sem_coro(sftp, path, event_type, style) for path in paths])
    
    async def _upload_file(self, sftp: SFTPClient, path, event_type: str | None = None, style: str | None = None, remote_path: str | None = None):
        remote_path = os.path.join(self.remotedir, remote_path or path).replace('\\', '/')
        try:  
            await sftp.put(path, remote_path, preserve=True)
            if event_type:
                console.print(event_type, path, style=style)
        except Exception as e:
            print('ERROR remote < upload', e)
            
    async def _remote_create_downgrade(self, conn: SSHClientConnection, migration: Migration, version: DirVersion):
        directory = f'{self.migrations_directory}/{version.name()}'
        abs_directory = self._remote_get_abs_path(directory)
        await conn.run(f'mkdir -p {abs_directory}/downgrade')
        
        cmds: list[str] = []
        cp_paths: list[CopyPath] = []
        
        for file in migration.files.created:
            cmds.append(f'rm "{self._remote_get_abs_path(file)}"')
        
        for d in migration.dirs.deleted:
            cmds.append(f'mkdir -p "{self._remote_get_abs_path(d)}"')
        for d in migration.dirs.created:
            cmds.append(f'rm -r "{self._remote_get_abs_path(d)}"')
        
        for file in migration.files.changed + migration.files.deleted:
            dest = f'{directory}/downgrade/{file}'
            cp_paths.append(CopyPath(origin=file, destination=dest))
            cmds.append(f'cp "{self._remote_get_abs_path(dest)}" "{self._remote_get_abs_path(file)}"')
            
        cmds.append(f'rm "{self._remote_get_abs_path(self.versions_directory)}/{version.filename()}"')
        cmds.append(f'rm -r "{abs_directory}"')
            
        await self._remote_cp_files(conn, cp_paths)
        await conn.run(f"echo '{'\n'.join(cmds)}' > {abs_directory}/_downgrade.sh")
        await conn.run(f"echo '{version.dumps()}' > {abs_directory}/_version.json")
        await conn.run(f"echo '{migration.changes().model_dump_json(indent=4)}' > {abs_directory}/_migration.json")
            
    def _remote_get_abs_path(self, path: str):
        assert not path.startswith(self.remotedir)
        return os.path.join(self.remotedir, path).replace('\\', '/')
    
    async def _remote_cp_files(self, conn: SSHClientConnection, paths: list[CopyPath]):
        sem = asyncio.Semaphore(10)
        async def sem_coro(conn, path):
            async with sem:
                return await self._remote_cp_file(conn, path)
        await asyncio.gather(*[sem_coro(conn, path) for path in paths])
            
    async def _remote_cp_file(self, conn: SSHClientConnection, path: CopyPath):
        origin = os.path.join(self.remotedir, path.origin).replace('\\', '/')
        destination = os.path.join(self.remotedir, path.destination).replace('\\', '/')
        cmd = f'mkdir -p "{os.path.dirname(destination)}" && cp "{origin}" "{destination}"'
        await conn.run(cmd)
        # print("!@$!", f'cp "{origin}" "{destination}"', result.stderr.strip())
    
    async def _delete_files(self, conn: SSHClientConnection, paths: list[str], event_type: str | None = None):
        sem = asyncio.Semaphore(10)
        async def sem_coro(conn, path, event_type):
            async with sem:
                return await self._delete_file(conn, path, event_type)
        await asyncio.gather(*[sem_coro(conn, path, event_type) for path in paths])
    
    async def _delete_file(self, conn: SSHClientConnection, path: str, event_type: str | None = None):
        remote_path = os.path.join(self.remotedir, path).replace('\\', '/')
        check = await conn.run(f'test -f "{remote_path}" && echo "File exists" || echo "File not exists"')
        if check.stdout.strip() == 'File not exists':
            return
        result = await conn.run(f'rm -f {remote_path}')
        if result.exit_status == 0:
            if event_type:
                console.print(event_type, path, style='red')
        else:
            print('ERROR remote < delete', path)
            print(result.stderr)
            
    async def _delete_directories(self, conn: SSHClientConnection, directories: list[str], event_type: str | None = None):
        sem = asyncio.Semaphore(10)
        async def sem_coro(conn, directory, event_type):
            async with sem:
                return await self._delete_directory(conn, directory, event_type)
        await asyncio.gather(*[sem_coro(conn, directory, event_type) for directory in directories])
        
    async def _delete_directory(self, conn: SSHClientConnection, directory: str, event_type: str | None = None):
        remote_path = os.path.join(self.remotedir, directory).replace('\\', '/')
        check = await conn.run(f'test -d "{remote_path}" && echo "Directory exists" || echo "Directory not exists"')
        if check.stdout.strip() == 'Directory not exists':
            return
        result = await conn.run(f'rm -rf {remote_path}')
        if result.exit_status == 0:
            if event_type:
                console.print(event_type, directory, style='red')
        else:
            print('ERROR remote < delete directory', directory)
            print(result.stderr)
            
    async def _remote_mk_dir(self, conn: SSHClientConnection, directory: str, event_type: str | None = None):
        remote_path = os.path.join(self.remotedir, directory).replace('\\', '/')
        await conn.run(f'mkdir -p {remote_path}')
        if event_type and directory != '':
            console.print(event_type, directory, style='green')
            
if __name__ == '__main__':
    try:
        from .cli import main
    except ImportError:
        from cli import main

    raise SystemExit(main())