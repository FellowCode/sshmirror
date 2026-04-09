import os
import re
import subprocess
import sys
import tempfile
import asyncio
import unittest
from asyncssh import SSHClientConnection
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sshmirror import SSHMirror, SSHMirrorCallbacks, SSHMirrorConfig, UserAbort, __version__
from sshmirror.cli import _build_interactive_menu_items, _configure_interactive_args, _create_default_config, build_parser
from sshmirror.core.filemap import DirVersion, Migration
from sshmirror.core.filemap import FileMap
from sshmirror.core.filewatcher import Filewatcher
from sshmirror.core.schemas import DiffDetail
from sshmirror.core.utils import check_path_is_ignored, compile_ignore_rules, parse_ignore_file
from sshmirror.prompts import _questionary_available


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class SSHMirrorSmokeTests(unittest.TestCase):
    def test_public_api_exports(self):
        self.assertEqual(SSHMirror.__name__, 'SSHMirror')
        self.assertEqual(SSHMirrorConfig.__name__, 'SSHMirrorConfig')
        self.assertEqual(SSHMirrorCallbacks.__name__, 'SSHMirrorCallbacks')
        self.assertTrue(__version__)

    def test_can_construct_from_config_object(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                self.assertEqual(mirror.host, '127.0.0.1')
                self.assertEqual(mirror.port, 22)
                self.assertEqual(mirror.remotedir, '/app/')
                self.assertEqual(mirror._create_version(FileMap()).message, 'update')
                self.assertTrue((tmp_path / '.sshmirror' / 'versions').exists())

    def test_invalid_config_requires_main_connection_fields(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                with self.assertRaisesRegex(ValueError, "'host'"):
                    SSHMirror(
                        config=SSHMirrorConfig(
                            host='',
                            port=22,
                            username='root',
                            localdir='.',
                            remotedir='/app',
                        )
                    )

    def test_invalid_config_rejects_partial_restart_container_connection(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                with self.assertRaisesRegex(ValueError, 'host.*,.*port.*,.*username|host.*port.*username'):
                    SSHMirror(
                        config=SSHMirrorConfig(
                            host='127.0.0.1',
                            port=22,
                            username='root',
                            localdir='.',
                            remotedir='/app',
                            restart_container={
                                'host': 'docker-host',
                                'container_name': 'app',
                            },
                        )
                    )

    def test_invalid_config_rejects_deprecated_restart_container_user_field(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                with self.assertRaisesRegex(ValueError, "restart_container.user.*no longer supported"):
                    SSHMirror(
                        config=SSHMirrorConfig(
                            host='127.0.0.1',
                            port=22,
                            username='root',
                            localdir='.',
                            remotedir='/app',
                            restart_container={
                                'host': 'docker-host',
                                'port': 22,
                                'user': 'root',
                                'container_name': 'app',
                            },
                        )
                    )

    def test_force_pull_without_callbacks_aborts_before_network(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                with self.assertRaises(UserAbort):
                    import asyncio

                    asyncio.run(mirror.force_pull())

    def test_render_diff_detail_accepts_structured_detail(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                detail = DiffDetail(
                    path='example.txt',
                    action='change',
                    before_label='before',
                    after_label='after',
                    before_text='alpha\n',
                    after_text='beta\n',
                )

                mirror.render_diff_detail(detail)

    def test_restart_container_connection_defaults_to_main_connection(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                        restart_container={
                            'container_name': 'app',
                            'sudo': True,
                        },
                    )
                )

                restart_connect_kwargs = mirror._get_restart_container_connect_kwargs()

                self.assertEqual(restart_connect_kwargs['host'], '127.0.0.1')
                self.assertEqual(restart_connect_kwargs['port'], 22)
                self.assertEqual(restart_connect_kwargs['username'], 'root')
                self.assertTrue(mirror._restart_container_uses_main_connection())

    def test_restart_container_username_is_used_for_separate_host(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                        restart_container={
                            'host': '192.168.1.10',
                            'port': 2222,
                            'username': 'deploy',
                            'container_name': 'app',
                        },
                    )
                )

                restart_connect_kwargs = mirror._get_restart_container_connect_kwargs()

                self.assertEqual(restart_connect_kwargs['host'], '192.168.1.10')
                self.assertEqual(restart_connect_kwargs['port'], 2222)
                self.assertEqual(restart_connect_kwargs['username'], 'deploy')

    def test_test_connection_skips_duplicate_docker_host_check(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                        restart_container={
                            'container_name': 'app',
                        },
                    )
                )

                class DummyConn:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                    async def run(self, _cmd, check=False):
                        class Result:
                            exit_status = 0
                            stderr = ''
                            stdout = ''
                        return Result()

                with patch('sshmirror.sshmirror.asyncssh.connect', return_value=DummyConn()) as connect_mock, \
                     patch.object(mirror, '_test_restart_container', new=AsyncMock()) as restart_test_mock, \
                     patch('sshmirror.sshmirror.clear_n_console_rows'):
                    asyncio.run(mirror.test_connection())

                self.assertEqual(connect_mock.call_count, 1)
                restart_test_mock.assert_not_awaited()

    def test_restart_container_uses_configured_sudo_password(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                callbacks = SSHMirrorCallbacks(secret=lambda _prompt: 'ignored-secret')
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                        restart_container={
                            'container_name': 'app',
                            'sudo': True,
                            'sudo_password': 'configured-secret',
                        },
                    ),
                    callbacks=callbacks,
                )

                command = mirror._build_restart_container_docker_cmd('restart')

                self.assertIn("printf '%s\\n' configured-secret | sudo -S -k -p '' -- docker restart app", command)

    def test_restart_container_prompts_for_sudo_password_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                secret_mock = Mock(return_value='prompted-secret')
                callbacks = SSHMirrorCallbacks(secret=secret_mock)
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                        restart_container={
                            'container_name': 'app',
                            'sudo': True,
                        },
                    ),
                    callbacks=callbacks,
                )

                first_command = mirror._build_restart_container_docker_cmd('restart')
                second_command = mirror._build_restart_container_docker_cmd('inspect --type container')

                self.assertIn("printf '%s\\n' prompted-secret | sudo -S -k -p '' -- docker restart app", first_command)
                self.assertIn(
                    "printf '%s\\n' prompted-secret | sudo -S -k -p '' -- docker inspect --type container app",
                    second_command,
                )
                secret_mock.assert_called_once_with('Sudo password for Docker host')

    def test_restart_container_preserves_leading_and_trailing_spaces_in_prompted_sudo_password(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                secret_mock = Mock(return_value='  padded-secret  ')
                callbacks = SSHMirrorCallbacks(secret=secret_mock)
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                        restart_container={
                            'container_name': 'app',
                            'sudo': True,
                        },
                    ),
                    callbacks=callbacks,
                )

                command = mirror._build_restart_container_docker_cmd('restart')

                self.assertIn("printf '%s\\n' '  padded-secret  ' | sudo -S -k -p '' -- docker restart app", command)

    def test_restart_container_sudo_check_reports_rejected_password(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                callbacks = SSHMirrorCallbacks(secret=lambda _prompt: 'bad-secret')
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                        restart_container={
                            'container_name': 'app',
                            'sudo': True,
                        },
                    ),
                    callbacks=callbacks,
                )

                class Result:
                    def __init__(self, exit_status, stderr='', stdout=''):
                        self.exit_status = exit_status
                        self.stderr = stderr
                        self.stdout = stdout

                dummy_conn = object.__new__(SSHClientConnection)
                dummy_conn.run = AsyncMock(side_effect=[
                    Result(0),
                    Result(1, stderr='Sorry, try again\nsudo: no password was provided'),
                ])

                with self.assertRaisesRegex(RuntimeError, 'sudo password was rejected or not accepted by sudo'):
                    asyncio.run(mirror._run_restart_container_diagnostics(dummy_conn))

    def test_restart_container_diagnostics_reports_missing_docker_binary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                        restart_container={
                            'container_name': 'app',
                        },
                    )
                )

                class Result:
                    def __init__(self, exit_status, stderr='', stdout=''):
                        self.exit_status = exit_status
                        self.stderr = stderr
                        self.stdout = stdout

                dummy_conn = object.__new__(SSHClientConnection)
                dummy_conn.run = AsyncMock(return_value=Result(1))

                with self.assertRaisesRegex(RuntimeError, 'docker is not installed or is not available in PATH'):
                    asyncio.run(mirror._run_restart_container_diagnostics(dummy_conn))

    def test_cli_help_mentions_docker_host_for_restart_connection(self):
        help_text = build_parser().format_help()
        normalized_help_text = re.sub(r'\x1b\[[0-9;]*m', '', help_text)
        normalized_help_text = ' '.join(normalized_help_text.split())

        self.assertIn('configured Docker host', normalized_help_text)

    def test_windows_can_use_interactive_questionary_menu(self):
        with patch('sshmirror.prompts.questionary', object()), \
             patch('sshmirror.prompts.os.name', 'nt'), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True), \
             patch('sshmirror.prompts.asyncio.get_running_loop', side_effect=RuntimeError):
            self.assertTrue(_questionary_available())

    def test_interactive_menu_uses_plain_labels(self):
        menu_items = _build_interactive_menu_items(
            has_config=True,
            has_ignore=True,
            initialized=True,
            has_stash=False,
        )
        labels = [label for label, _action in menu_items]

        self.assertIn('Pull & Push', labels)
        self.assertIn('Test connection', labels)
        self.assertIn('Exit', labels)

    def test_interactive_menu_exit_is_graceful(self):
        args = build_parser().parse_args([])

        with patch('sshmirror.cli._find_default_cli_path', return_value='sshmirror.config.yml'), \
             patch('sshmirror.cli._is_sshmirror_initialized', return_value=True), \
             patch('sshmirror.cli._has_stashed_changes', return_value=False), \
             patch('sshmirror.cli.prompt_choice', return_value='Exit'):
            configured_args = _configure_interactive_args(args)

        self.assertTrue(configured_args.exit_requested)

    def test_interactive_create_config_exits_after_creation(self):
        args = build_parser().parse_args([])

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path), \
                 patch('sshmirror.cli._find_default_cli_path', return_value=None), \
                 patch('sshmirror.cli._is_sshmirror_initialized', return_value=False), \
                 patch('sshmirror.cli._has_stashed_changes', return_value=False), \
                 patch('sshmirror.cli.prompt_choice', return_value='Create sshmirror.config.yml'):
                configured_args = _configure_interactive_args(args)

            self.assertTrue(configured_args.exit_requested)
            self.assertTrue((tmp_path / 'sshmirror.config.yml').exists())

    def test_fallback_created_config_contains_field_descriptions(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                created = _create_default_config()

                self.assertTrue(created)
                content = (tmp_path / 'sshmirror.config.yml').read_text(encoding='utf-8')

            self.assertIn('# SSH host or IP address used for the main sync connection.', content)
            self.assertIn('# Local project directory that will be synchronized.', content)
            self.assertIn('# Optional. If set, SSHMirror can restart a container after sync.', content)
            self.assertIn('restart_container:', content)
            self.assertIn('# Docker container name that should be restarted after sync.', content)

    def test_cli_entrypoint_help(self):
        result = subprocess.run(
            [sys.executable, '-m', 'sshmirror', '--help'],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('--status', result.stdout)
        self.assertIn('--test-connection', result.stdout)

    def test_ignore_rules_match_nested_directories_and_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ignore_path = tmp_path / 'sshmirror.ignore.txt'
            ignore_path.write_text('node_modules/\n*.log\ncache/tmp\n', encoding='utf-8')

            ignore_rules = parse_ignore_file(str(ignore_path))

            self.assertTrue(check_path_is_ignored('node_modules/pkg/index.js', ignore_rules))
            self.assertTrue(check_path_is_ignored('src/node_modules/pkg/index.js', ignore_rules))
            self.assertTrue(check_path_is_ignored('debug.log', ignore_rules))
            self.assertTrue(check_path_is_ignored('logs/debug.log', ignore_rules))
            self.assertTrue(check_path_is_ignored('cache/tmp', ignore_rules))
            self.assertTrue(check_path_is_ignored('cache/tmp/data.json', ignore_rules))
            self.assertTrue(check_path_is_ignored('node_modules', ignore_rules, is_dir=True))
            self.assertFalse(check_path_is_ignored('src/app.py', ignore_rules))

    def test_filewatcher_skips_ignored_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ignore_path = tmp_path / 'sshmirror.ignore.txt'
            ignore_path.write_text('ignored/\n*.tmp\n', encoding='utf-8')

            included_file = tmp_path / 'src' / 'main.py'
            ignored_dir_file = tmp_path / 'ignored' / 'secret.txt'
            ignored_extension_file = tmp_path / 'src' / 'draft.tmp'
            included_file.parent.mkdir(parents=True, exist_ok=True)
            ignored_dir_file.parent.mkdir(parents=True, exist_ok=True)
            included_file.write_text('print(1)\n', encoding='utf-8')
            ignored_dir_file.write_text('skip\n', encoding='utf-8')
            ignored_extension_file.write_text('skip\n', encoding='utf-8')

            with working_directory(tmp_path):
                FileMap.init(ignore_file_path=str(ignore_path))
                filemap = asyncio.run(Filewatcher('.', str(ignore_path)).get_filemap())

            self.assertIn('src/main.py', filemap.path_list())
            self.assertNotIn('ignored/secret.txt', filemap.path_list())
            self.assertNotIn('src/draft.tmp', filemap.path_list())

    def test_filewatcher_does_not_scan_ignored_directories(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ignore_path = tmp_path / 'sshmirror.ignore.txt'
            ignore_path.write_text('ignored/\n', encoding='utf-8')

            ignored_dir = tmp_path / 'ignored'
            included_dir = tmp_path / 'src'
            ignored_dir.mkdir(parents=True, exist_ok=True)
            included_dir.mkdir(parents=True, exist_ok=True)
            (ignored_dir / 'secret.txt').write_text('skip\n', encoding='utf-8')
            (included_dir / 'main.py').write_text('print(1)\n', encoding='utf-8')

            original_scandir = os.scandir
            scanned_paths: list[str] = []

            def tracking_scandir(path):
                scanned_paths.append(str(path).replace('\\', '/'))
                return original_scandir(path)

            with working_directory(tmp_path), patch('sshmirror.core.filewatcher.os.scandir', side_effect=tracking_scandir):
                asyncio.run(Filewatcher('.', str(ignore_path)).get_filemap())

            ignored_scans = [path for path in scanned_paths if Path(path).name == 'ignored' and path not in {'.', './'}]
            self.assertEqual(ignored_scans, [])

    def test_library_mode_auto_detects_ignore_file_from_localdir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ignore_path = tmp_path / 'sshmirror.ignore.txt'
            ignore_path.write_text('ignored/\n', encoding='utf-8')

            included_file = tmp_path / 'src' / 'main.py'
            ignored_file = tmp_path / 'ignored' / 'secret.txt'
            included_file.parent.mkdir(parents=True, exist_ok=True)
            ignored_file.parent.mkdir(parents=True, exist_ok=True)
            included_file.write_text('print(1)\n', encoding='utf-8')
            ignored_file.write_text('skip\n', encoding='utf-8')

            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                self.assertEqual(Path(mirror.ignore_file_path), ignore_path.resolve())

                filemap = asyncio.run(mirror.filewatcher.get_filemap())

            self.assertIn('src/main.py', filemap.path_list())
            self.assertNotIn('ignored/secret.txt', filemap.path_list())

    def test_library_mode_refreshes_ignore_file_before_sync(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ignored_file = tmp_path / 'ignored' / 'secret.txt'
            ignored_file.parent.mkdir(parents=True, exist_ok=True)
            ignored_file.write_text('skip\n', encoding='utf-8')

            ignore_path = tmp_path / 'sshmirror.ignore.txt'
            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                self.assertIsNone(mirror.ignore_file_path)

                ignore_path.write_text('ignored/\n', encoding='utf-8')

                mirror._refresh_ignore_file_path()

                self.assertEqual(Path(mirror.ignore_file_path), ignore_path.resolve())

    def test_remote_newer_ignore_file_is_downloaded_before_sync(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ignore_path = tmp_path / 'sshmirror.ignore.txt'
            ignore_path.write_text('ignored/\n', encoding='utf-8')

            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                prevstate = asyncio.run(mirror.filewatcher.get_filemap())
                asyncio.run(mirror._save_prevstate(prevstate))

                async def fake_download(_conn, remote_relative_path, local_path, mtime_ns=None):
                    self.assertEqual(remote_relative_path, 'sshmirror.ignore.txt')
                    Path(local_path).write_text('remote-only/\n', encoding='utf-8')
                    if mtime_ns is not None:
                        os.utime(local_path, ns=(mtime_ns, mtime_ns))

                dummy_conn = object.__new__(SSHClientConnection)

                with patch.object(mirror, '_get_remote_file_stat', new=AsyncMock(return_value=(ignore_path.stat().st_mtime_ns + 10_000_000_000, 12))), \
                     patch.object(mirror, '_download_remote_file_to_path', new=AsyncMock(side_effect=fake_download)):
                    asyncio.run(mirror._sync_ignore_file_before_transfer(dummy_conn))

                self.assertEqual(ignore_path.read_text(encoding='utf-8'), 'remote-only/\n')
                self.assertFalse((tmp_path / '.sshmirror' / 'conflicts.json').exists())

    def test_remote_newer_ignore_file_uses_conflict_mechanics(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ignore_path = tmp_path / 'sshmirror.ignore.txt'
            ignore_path.write_text('base-ignore/\n', encoding='utf-8')

            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                prevstate = asyncio.run(mirror.filewatcher.get_filemap())
                asyncio.run(mirror._save_prevstate(prevstate))
                ignore_path.write_text('local-change/\n', encoding='utf-8')

                async def fake_download(_conn, _remote_relative_path, local_path, mtime_ns=None):
                    Path(local_path).write_text('remote-change/\n', encoding='utf-8')
                    if mtime_ns is not None:
                        os.utime(local_path, ns=(mtime_ns, mtime_ns))

                dummy_conn = object.__new__(SSHClientConnection)

                with patch.object(mirror, '_get_remote_file_stat', new=AsyncMock(return_value=(ignore_path.stat().st_mtime_ns + 10_000_000_000, 13))), \
                     patch.object(mirror, '_download_remote_file_to_path', new=AsyncMock(side_effect=fake_download)):
                    with self.assertRaises(UserAbort):
                        asyncio.run(mirror._sync_ignore_file_before_transfer(dummy_conn))

                self.assertEqual(ignore_path.read_text(encoding='utf-8'), 'remote-change/\n')
                conflict_copy = tmp_path / 'sshmirror.ignore._local.txt'
                self.assertTrue(conflict_copy.exists())
                self.assertEqual(conflict_copy.read_text(encoding='utf-8'), 'local-change/\n')
                self.assertTrue((tmp_path / '.sshmirror' / 'conflicts.json').exists())

    def test_remote_scan_prunes_ignored_directories(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ignore_path = tmp_path / 'sshmirror.ignore.txt'
            ignore_path.write_text('node_modules/\ncache/tmp/\n*.log\n', encoding='utf-8')

            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                commands: list[str] = []

                async def fake_run(command, check=False):
                    commands.append(command)

                    class Result:
                        stdout = ''
                        stderr = ''
                        exit_status = 0

                    return Result()

                dummy_conn = object.__new__(SSHClientConnection)
                dummy_conn.run = AsyncMock(side_effect=fake_run)

                asyncio.run(mirror._get_remote_map(dummy_conn))

            self.assertEqual(len(commands), 2)
            self.assertIn('-prune', commands[0])
            self.assertIn("-name node_modules", commands[0])
            self.assertIn("-path /app/cache/tmp", commands[0])
            self.assertIn("! -name '*.log'", commands[0])

    def test_push_prompts_for_version_message_after_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                callbacks = SSHMirrorCallbacks(
                    confirm=lambda _message: True,
                    choose=None,
                    text=lambda prompt, default: 'feature sync',
                )
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    ),
                    callbacks=callbacks,
                )

                origin = FileMap()
                target = FileMap()
                target.add('src/app.py', 'abc')
                migration = Migration(origin, target)
                version = mirror._create_version(target)
                dummy_conn = object.__new__(SSHClientConnection)

                with patch.object(mirror, '_remote_create_downgrade', new=AsyncMock()) as remote_create_downgrade, \
                     patch.object(mirror, '_run_commands', new=AsyncMock()), \
                     patch.object(mirror, '_remote_mk_dir', new=AsyncMock()), \
                     patch.object(mirror, '_delete_directories', new=AsyncMock()), \
                     patch.object(mirror, '_upload_files', new=AsyncMock()), \
                     patch.object(mirror, '_delete_files', new=AsyncMock()), \
                     patch.object(mirror, '_set_remote_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_prevstate', new=AsyncMock()):
                    asyncio.run(mirror._push(version, migration, dummy_conn))

                self.assertEqual(version.message, 'feature sync')
                remote_create_downgrade.assert_awaited_once()
                self.assertEqual(remote_create_downgrade.await_args.args[2].message, 'feature sync')


if __name__ == '__main__':
    unittest.main()