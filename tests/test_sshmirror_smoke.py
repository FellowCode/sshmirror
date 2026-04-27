import argparse
import os
import re
import subprocess
import sys
import tempfile
import asyncio
import datetime
import json
import time
import unittest
from asyncssh import SSHClientConnection
from contextlib import contextmanager
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from unittest.mock import AsyncMock, Mock, call, patch

from sshmirror import SSHMirror, SSHMirrorCallbacks, SSHMirrorConfig, UserAbort, __version__
from sshmirror._version import DIR_VERSION_FORMAT
from sshmirror.cli import _build_interactive_menu_items, _build_styled_version_choice, _build_version_choice_map, _build_version_page_choices, _choose_version_interactively, _configure_interactive_args, _create_default_config, _format_version_choice_display_label, _format_version_choice_label, _format_version_page_prompt, _get_version_page_for_index, _is_silent_user_abort, _print_interactive_version, _render_version_page, _show_current_changes_cli, _show_version_changes_cli, _show_version_history_cli, build_parser, main
from sshmirror.core.filemap import DirVersion, Migration
from sshmirror.core.filemap import FileMap
from sshmirror.core.filewatcher import Filewatcher
from sshmirror.core.schemas import DiffDetail, DiffFileChange, DiffVersionInfo, Difference, MigrationChanges
from sshmirror.core.utils import check_path_is_ignored, compile_ignore_rules, parse_ignore_file
from sshmirror.prompts import _PROMPT_FALLBACK, _questionary_available, consume_confirm_retry_extra_lines, prompt_choice, prompt_confirm
from sshmirror.sshmirror import RemoteRollbackError, RemoteSyncError
from sshmirror.core.exceptions import IncompatibleVersionFormat
from sshmirror.core.exceptions import RemoteSyncLockError


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

    def test_created_version_records_sshmirror_version_metadata(self):
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

                version = mirror._create_version(FileMap())

                self.assertEqual(version.created_by_sshmirror_version, __version__)
                self.assertEqual(version.version_format, DIR_VERSION_FORMAT)

    def test_dirversion_rejects_newer_incompatible_metadata_format(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )
                version_payload = {
                    'dt': '2026-04-10T10:00:00+00:00',
                    'uid': 'future-version',
                    'author': 'tester',
                    'message': 'future metadata',
                    'created_by_sshmirror_version': '9.9.9',
                    'version_format': DIR_VERSION_FORMAT + 1,
                    'filemap': FileMap().asdict(),
                }

                with self.assertRaisesRegex(IncompatibleVersionFormat, 'Update sshmirror'):
                    DirVersion.loads(json.dumps(version_payload))

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

    def test_force_pull_constructs_migration_from_local_to_remote(self):
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

                local_map = FileMap()
                local_map.add('local.txt', 'md5-local', size=5, mtime=1)
                remote_map = FileMap()
                remote_map.add('remote.txt', 'md5-remote', size=6, mtime=2)

                remote_version = mirror._create_version(remote_map)

                class DummyConnectContext:
                    def __init__(self, connection):
                        self.connection = connection

                    async def __aenter__(self):
                        return self.connection

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                conn = Mock(spec=SSHClientConnection)

                with patch('sshmirror.sshmirror.asyncssh.connect', new=Mock(return_value=DummyConnectContext(conn))), \
                     patch.object(mirror, '_validate_conflicts', AsyncMock(return_value=True)), \
                     patch.object(mirror, '_confirm', return_value=True), \
                     patch.object(mirror, '_sync_ignore_file_before_transfer', AsyncMock()), \
                     patch.object(mirror, '_get_local_versions_stack', AsyncMock(return_value=[])), \
                     patch.object(mirror, '_get_remote_versions_stack', AsyncMock(return_value=[remote_version])), \
                     patch.object(mirror, '_load_prevstate', AsyncMock(return_value=None)), \
                     patch.object(mirror, '_scan_project_maps_with_progress', AsyncMock(return_value=(local_map, remote_map))), \
                     patch.object(mirror, '_build_before_sync_lock_callback', return_value=AsyncMock()), \
                     patch.object(mirror, '_save_prevstate', AsyncMock()), \
                     patch.object(mirror, '_release_remote_sync_lock', AsyncMock()), \
                     patch.object(mirror, '_pull', AsyncMock()) as pull_mock:
                    import asyncio
                    asyncio.run(mirror.force_pull(require_confirm=False))

                self.assertEqual(pull_mock.await_count, 1)
                migration = pull_mock.call_args.args[1]
                self.assertEqual(sorted(migration.files.created), ['remote.txt'])
                self.assertEqual(sorted(migration.files.deleted), ['local.txt'])

    def test_push_preserves_preview_panel_after_confirm(self):
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

                origin = FileMap()
                target = FileMap()
                target.add('remote.txt', 'md5-remote', size=6, mtime=1)
                migration = origin.migrate_to(target)
                version = mirror._create_version(target)
                conn = Mock(spec=SSHClientConnection)

                class DummyLive:
                    def __init__(self, *args, **kwargs):
                        pass

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                    def update(self, *args, **kwargs):
                        pass

                with patch.object(mirror, '_confirm', return_value=True), \
                     patch.object(mirror, '_prompt_version_message', return_value='atomic push'), \
                     patch.object(mirror, '_remote_create_downgrade', AsyncMock()), \
                     patch.object(mirror, '_run_commands', AsyncMock()), \
                     patch.object(mirror, '_remote_mk_dir', AsyncMock()), \
                     patch.object(mirror, '_delete_directories', AsyncMock()), \
                     patch.object(mirror, '_upload_files', AsyncMock()), \
                     patch.object(mirror, '_delete_files', AsyncMock()), \
                     patch.object(mirror, '_set_remote_version', AsyncMock()), \
                     patch.object(mirror, '_save_version', AsyncMock()), \
                     patch.object(mirror, '_save_prevstate', AsyncMock()), \
                     patch('sshmirror.sshmirror.Live', DummyLive), \
                     patch('sshmirror.sshmirror.clear_n_console_rows') as clear_rows:
                    import asyncio
                    asyncio.run(mirror._push(version, migration, conn))

                clear_rows.assert_called_once_with(1)

    def test_push_rolls_back_remote_changes_when_file_sync_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                local_file = tmp_path / 'created.txt'
                local_file.write_text('payload', encoding='utf-8')

                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                origin = FileMap()
                target = FileMap()
                target.add('created.txt', 'md5-created', size=7, mtime=1)
                migration = origin.migrate_to(target)
                version = mirror._create_version(target)
                conn = Mock(spec=SSHClientConnection)

                with patch.object(mirror, '_confirm', return_value=True), \
                     patch.object(mirror, '_prompt_version_message', return_value='atomic push'), \
                     patch.object(mirror, '_remote_create_downgrade', AsyncMock()), \
                     patch.object(mirror, '_run_commands', AsyncMock()), \
                     patch.object(mirror, '_remote_mk_dir', AsyncMock()), \
                     patch.object(mirror, '_delete_directories', AsyncMock()), \
                     patch.object(mirror, '_upload_files', AsyncMock(side_effect=[None, RemoteSyncError('upload failed')])), \
                     patch.object(mirror, '_delete_files', AsyncMock()), \
                     patch.object(mirror, '_rollback_remote_push', AsyncMock()) as rollback_mock, \
                     patch.object(mirror, '_set_remote_version', AsyncMock()), \
                     patch.object(mirror, '_save_version', AsyncMock()) as save_version_mock, \
                     patch.object(mirror, '_save_prevstate', AsyncMock()) as save_prevstate_mock:
                    with self.assertRaisesRegex(RemoteSyncError, 'rolled back'):
                        asyncio.run(mirror._push(version, migration, conn))

                rollback_mock.assert_awaited_once_with(conn, version)
                save_version_mock.assert_not_awaited()
                save_prevstate_mock.assert_not_awaited()

    def test_push_reports_when_remote_rollback_also_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                local_file = tmp_path / 'created.txt'
                local_file.write_text('payload', encoding='utf-8')

                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                origin = FileMap()
                target = FileMap()
                target.add('created.txt', 'md5-created', size=7, mtime=1)
                migration = origin.migrate_to(target)
                version = mirror._create_version(target)
                conn = Mock(spec=SSHClientConnection)

                with patch.object(mirror, '_confirm', return_value=True), \
                     patch.object(mirror, '_prompt_version_message', return_value='atomic push'), \
                     patch.object(mirror, '_remote_create_downgrade', AsyncMock()), \
                     patch.object(mirror, '_run_commands', AsyncMock()), \
                     patch.object(mirror, '_remote_mk_dir', AsyncMock()), \
                     patch.object(mirror, '_delete_directories', AsyncMock()), \
                     patch.object(mirror, '_upload_files', AsyncMock(side_effect=[None, RemoteSyncError('upload failed')])), \
                     patch.object(mirror, '_delete_files', AsyncMock()), \
                     patch.object(mirror, '_rollback_remote_push', AsyncMock(side_effect=RemoteRollbackError('rollback failed'))):
                    with self.assertRaisesRegex(RemoteRollbackError, 'rollback also failed'):
                        asyncio.run(mirror._push(version, migration, conn))

    def test_remote_downgrade_script_uses_relative_paths(self):
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

                origin = FileMap()
                origin.add('changed.txt', 'md5-old', size=3, mtime=1)
                origin.add('deleted.txt', 'md5-del', size=3, mtime=1)
                origin.add_directory('deleted-dir')

                target = FileMap()
                target.add('changed.txt', 'md5-new', size=4, mtime=2)
                target.add('created.txt', 'md5-created', size=7, mtime=3)
                target.add_directory('created-dir')

                migration = origin.migrate_to(target)
                version = mirror._create_version(target)
                conn = Mock(spec=SSHClientConnection)

                with patch.object(mirror, '_remote_cp_files', AsyncMock()) as cp_mock, \
                     patch.object(mirror, '_write_remote_text_file', AsyncMock()) as write_remote_text_file_mock, \
                     patch.object(mirror, '_run_remote_checked', AsyncMock()):
                    asyncio.run(mirror._remote_create_downgrade(conn, migration, version))

                cp_mock.assert_awaited_once()

                downgrade_call = next(
                    call for call in write_remote_text_file_mock.await_args_list
                    if call.args[1].endswith('/_downgrade.sh')
                )
                downgrade_script = downgrade_call.args[2]

                self.assertIn('rm created.txt', downgrade_script)
                self.assertIn('mkdir -p deleted-dir', downgrade_script)
                self.assertIn('rm -r created-dir', downgrade_script)
                self.assertIn('cp .sshmirror/migrations/', downgrade_script)
                self.assertNotIn('/app/', downgrade_script)

    def test_remote_downgrade_script_quotes_paths_with_spaces(self):
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

                origin = FileMap()
                origin.add('changed file.txt', 'md5-old', size=3, mtime=1)
                origin.add('deleted file.txt', 'md5-del', size=3, mtime=1)
                origin.add_directory('deleted dir')

                target = FileMap()
                target.add('changed file.txt', 'md5-new', size=4, mtime=2)
                target.add('created file.txt', 'md5-created', size=7, mtime=3)
                target.add_directory('created dir')

                migration = origin.migrate_to(target)
                version = mirror._create_version(target)
                conn = Mock(spec=SSHClientConnection)

                with patch.object(mirror, '_remote_cp_files', AsyncMock()) as cp_mock, \
                     patch.object(mirror, '_write_remote_text_file', AsyncMock()) as write_remote_text_file_mock, \
                     patch.object(mirror, '_run_remote_checked', AsyncMock()):
                    asyncio.run(mirror._remote_create_downgrade(conn, migration, version))

                cp_mock.assert_awaited_once()

                downgrade_call = next(
                    call for call in write_remote_text_file_mock.await_args_list
                    if call.args[1].endswith('/_downgrade.sh')
                )
                downgrade_script = downgrade_call.args[2]

                self.assertIn("rm 'created file.txt'", downgrade_script)
                self.assertIn("mkdir -p 'deleted dir'", downgrade_script)
                self.assertIn("rm -r 'created dir'", downgrade_script)
                self.assertIn("cp '.sshmirror/migrations/", downgrade_script)
                self.assertIn("/downgrade/changed file.txt' 'changed file.txt'", downgrade_script)
                self.assertIn("/downgrade/deleted file.txt' 'deleted file.txt'", downgrade_script)

    def test_run_remote_script_from_project_root_quotes_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app dir',
                    )
                )

                conn = Mock(spec=SSHClientConnection)

                with patch.object(mirror, '_run_remote_checked', AsyncMock()) as run_remote_checked_mock:
                    asyncio.run(mirror._run_remote_script_from_project_root(
                        conn,
                        '.sshmirror/migrations/version dir/_downgrade.sh',
                        'Failed to run downgrade script',
                    ))

                run_remote_checked_mock.assert_awaited_once_with(
                    conn,
                    "cd '/app dir' && sh '.sshmirror/migrations/version dir/_downgrade.sh'",
                    'Failed to run downgrade script',
                )

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

    def test_version_label_uses_readable_utc_timestamp(self):
        version = DirVersion(
            dt=datetime.datetime(2026, 4, 9, 12, 34, 56, tzinfo=datetime.timezone.utc),
            uid='1234567890abcdef',
            author='alice',
            message='deploy api',
            filemap=FileMap(),
        )

        label = SSHMirror._format_version_label(version)

        self.assertEqual(label, '2026-04-09 12:34:56 UTC | 12345678 | alice | deploy api')

    def test_build_version_page_choices_reports_navigation_flags(self):
        has_newer, has_older, total_pages = _build_version_page_choices(total_versions=45, page=0)

        self.assertFalse(has_newer)
        self.assertTrue(has_older)
        self.assertEqual(total_pages, 3)

    def test_format_version_page_prompt_includes_range_summary(self):
        prompt = _format_version_page_prompt('Choose base version', page=0, total_versions=45)

        self.assertEqual(prompt, 'Choose base version (page 1/3, showing 26-45 of 45, newest first)')

    def test_get_version_page_for_index_uses_newest_first_order(self):
        self.assertEqual(_get_version_page_for_index(39, total_versions=40), 0)
        self.assertEqual(_get_version_page_for_index(20, total_versions=40), 0)
        self.assertEqual(_get_version_page_for_index(19, total_versions=40), 1)
        self.assertEqual(_get_version_page_for_index(0, total_versions=40), 1)

    def test_choose_version_interactively_supports_pagination(self):
        versions_page_1 = [
            DiffVersionInfo(uid=str(index), label=f'2026-04-09 12:{index:02d}:00 UTC | {index:08d} | user | msg-{index}', dt=f'dt-{index}', index=index)
            for index in range(5, 25)
        ]
        versions_page_2 = [
            DiffVersionInfo(uid=str(index), label=f'2026-04-09 11:{index:02d}:00 UTC | {index:08d} | user | msg-{index}', dt=f'dt-{index}', index=index)
            for index in range(0, 5)
        ]
        mirror = Mock()
        mirror.list_remote_versions_page = AsyncMock(side_effect=[
            (versions_page_1, 25),
            (versions_page_2, 25),
        ])

        with patch('sshmirror.cli.prompt_choice', side_effect=['Older versions', _format_version_choice_label(versions_page_2[1])]) as prompt_mock:
            selected = asyncio.run(_choose_version_interactively(mirror, 'Choose base version'))

        self.assertIsNotNone(selected)
        self.assertEqual(selected.uid, '1')
        self.assertEqual(mirror.list_remote_versions_page.await_count, 2)
        self.assertIn('showing 6-25 of 25', prompt_mock.call_args_list[0].args[0])
        self.assertEqual(prompt_mock.call_args_list[0].args[1][0], 'Older versions')
        self.assertEqual(prompt_mock.call_args_list[0].args[1][-1], 'Back')
        self.assertNotIn('Newer versions', prompt_mock.call_args_list[0].args[1])
        self.assertEqual(prompt_mock.call_args_list[1].args[1][-2], 'Newer versions')
        self.assertEqual(prompt_mock.call_args_list[0].kwargs['default'], _format_version_choice_label(versions_page_1[-1]))

    def test_version_choice_label_uses_five_digit_global_number(self):
        version = DiffVersionInfo(
            uid='1234567890abcdef',
            label='2026-04-09 12:34:56 UTC | 12345678 | user | msg',
            dt='2026-04-09 12:34:56 UTC',
            index=12344,
        )

        self.assertEqual(
            _format_version_choice_label(version),
            '12345 | 2026-04-09 12:34:56 UTC | -            | 12345678 | update                                  ',
        )

    def test_show_version_changes_cli_uses_paginated_version_selection(self):
        versions_first_page = [
            DiffVersionInfo(uid=str(index), label=f'2026-04-09 12:{index:02d}:00 UTC | {index:08d} | user | msg-{index}', dt=f'dt-{index}', index=index, filename=f'version-{index}.json')
            for index in range(5, 25)
        ]
        versions_target_page = [
            DiffVersionInfo(uid='10', label='2026-04-09 12:10:00 UTC | 00000010 | user | msg-10', dt='dt-10', index=10, filename='version-10.json'),
            DiffVersionInfo(uid='11', label='2026-04-09 12:11:00 UTC | 00000011 | user | msg-11', dt='dt-11', index=11, filename='version-11.json'),
            DiffVersionInfo(uid='12', label='2026-04-09 12:12:00 UTC | 00000012 | user | msg-12', dt='dt-12', index=12, filename='version-12.json'),
            DiffVersionInfo(uid='13', label='2026-04-09 12:13:00 UTC | 00000013 | user | msg-13', dt='dt-13', index=13, filename='version-13.json'),
            DiffVersionInfo(uid='14', label='2026-04-09 12:14:00 UTC | 00000014 | user | msg-14', dt='dt-14', index=14, filename='version-14.json'),
            DiffVersionInfo(uid='15', label='2026-04-09 12:15:00 UTC | 00000015 | user | msg-15', dt='dt-15', index=15, filename='version-15.json'),
            DiffVersionInfo(uid='16', label='2026-04-09 12:16:00 UTC | 00000016 | user | msg-16', dt='dt-16', index=16, filename='version-16.json'),
            DiffVersionInfo(uid='17', label='2026-04-09 12:17:00 UTC | 00000017 | user | msg-17', dt='dt-17', index=17, filename='version-17.json'),
            DiffVersionInfo(uid='18', label='2026-04-09 12:18:00 UTC | 00000018 | user | msg-18', dt='dt-18', index=18, filename='version-18.json'),
            DiffVersionInfo(uid='19', label='2026-04-09 12:19:00 UTC | 00000019 | user | msg-19', dt='dt-19', index=19, filename='version-19.json'),
            DiffVersionInfo(uid='20', label='2026-04-09 12:20:00 UTC | 00000020 | user | msg-20', dt='dt-20', index=20, filename='version-20.json'),
            DiffVersionInfo(uid='21', label='2026-04-09 12:21:00 UTC | 00000021 | user | msg-21', dt='dt-21', index=21, filename='version-21.json'),
            DiffVersionInfo(uid='22', label='2026-04-09 12:22:00 UTC | 00000022 | user | msg-22', dt='dt-22', index=22, filename='version-22.json'),
            DiffVersionInfo(uid='23', label='2026-04-09 12:23:00 UTC | 00000023 | user | msg-23', dt='dt-23', index=23, filename='version-23.json'),
            DiffVersionInfo(uid='24', label='2026-04-09 12:24:00 UTC | 00000024 | user | msg-24', dt='dt-24', index=24, filename='version-24.json'),
        ]
        file_changes = [
            DiffFileChange(action='change', path='src/app.py'),
            DiffFileChange(action='create', path='src/new.py'),
            DiffFileChange(action='delete', path='src/old.py'),
        ]
        mirror = Mock()
        mirror.list_remote_versions_page = AsyncMock(side_effect=[
            (versions_first_page, 25),
            (versions_first_page, 25),
            (versions_target_page, 15),
            (versions_first_page, 25),
        ])
        mirror.get_current_synced_version_info = AsyncMock(return_value=None)
        mirror.list_version_changes_by_filenames = AsyncMock(return_value=file_changes)
        mirror.get_version_change_detail_by_range = AsyncMock(
            return_value=DiffDetail(
                path='src/app.py',
                action='change',
                before_label='before',
                after_label='after',
                before_text='old',
                after_text='new',
            )
        )
        mirror.render_diff_detail = Mock()

        with patch(
            'sshmirror.cli.prompt_choice',
            side_effect=[
                _format_version_choice_label(versions_first_page[4]),
                _format_version_choice_label(versions_target_page[5]),
                'src/app.py',
                'Back',
                'Back',
                'Back',
            ],
        ), patch('sshmirror.cli.clear_n_console_rows') as clear_rows_mock:
            asyncio.run(_show_version_changes_cli(mirror))

        clear_rows_mock.assert_called_once_with(1)
        mirror.list_version_changes_by_filenames.assert_awaited_once_with('version-9.json', 'version-15.json')
        mirror.get_version_change_detail_by_range.assert_awaited_once_with('version-9.json', 'version-15.json', 9, 15, 'src/app.py')

    def test_show_version_changes_cli_returns_to_base_selection_after_back(self):
        base_version = DiffVersionInfo(
            uid='25',
            label='2026-04-09 12:25:00 UTC | 00000025 | user | msg-25',
            dt='dt-25',
            index=24,
            filename='version-25.json',
        )
        target_version = DiffVersionInfo(
            uid='30',
            label='2026-04-09 12:30:00 UTC | 00000030 | user | msg-30',
            dt='dt-30',
            index=29,
            filename='version-30.json',
        )
        mirror = Mock()
        mirror.list_remote_versions_page = AsyncMock(return_value=([base_version], 40))
        mirror.get_current_synced_version_info = AsyncMock(return_value=None)

        choose_mock = AsyncMock(side_effect=[base_version, target_version, base_version, target_version, None])
        inspect_mock = AsyncMock()

        with patch('sshmirror.cli._choose_version_interactively', choose_mock), \
             patch('sshmirror.cli._inspect_version_range_cli', inspect_mock), \
             patch('sshmirror.cli.clear_n_console_rows'):
            asyncio.run(_show_version_changes_cli(mirror))

        self.assertEqual(choose_mock.await_count, 5)
        self.assertEqual(choose_mock.await_args_list[0].kwargs.get('initial_page'), 0)
        self.assertEqual(choose_mock.await_args_list[1].kwargs.get('initial_page'), 0)
        self.assertEqual(choose_mock.await_args_list[2].kwargs.get('initial_page'), 0)
        self.assertEqual(choose_mock.await_args_list[3].kwargs.get('initial_page'), 0)
        inspect_mock.assert_has_awaits([
            call(mirror, base_version, target_version),
            call(mirror, base_version, target_version),
        ])

    def test_show_version_changes_cli_reselects_base_after_target_back(self):
        base_version = DiffVersionInfo(
            uid='25',
            label='2026-04-09 12:25:00 UTC | 00000025 | user | msg-25',
            dt='dt-25',
            index=24,
            filename='version-25.json',
        )
        target_version = DiffVersionInfo(
            uid='30',
            label='2026-04-09 12:30:00 UTC | 00000030 | user | msg-30',
            dt='dt-30',
            index=29,
            filename='version-30.json',
        )
        mirror = Mock()
        mirror.list_remote_versions_page = AsyncMock(return_value=([base_version], 40))
        mirror.get_current_synced_version_info = AsyncMock(return_value=None)

        choose_mock = AsyncMock(side_effect=[base_version, None, base_version, target_version, None])
        inspect_mock = AsyncMock()

        with patch('sshmirror.cli._choose_version_interactively', choose_mock), \
             patch('sshmirror.cli._inspect_version_range_cli', inspect_mock), \
             patch('sshmirror.cli.clear_n_console_rows'):
            asyncio.run(_show_version_changes_cli(mirror))

        self.assertEqual(choose_mock.await_count, 5)
        self.assertEqual(choose_mock.await_args_list[0].kwargs.get('initial_page'), 0)
        self.assertEqual(choose_mock.await_args_list[1].kwargs.get('initial_page'), 0)
        self.assertEqual(choose_mock.await_args_list[2].kwargs.get('initial_page'), 0)
        self.assertEqual(choose_mock.await_args_list[3].kwargs.get('initial_page'), 0)
        inspect_mock.assert_awaited_once_with(mirror, base_version, target_version)

    def test_show_version_history_cli_compares_selected_version_with_previous(self):
        versions_page = [
            DiffVersionInfo(uid='1', label='2026-04-09 12:01:00 UTC | 00000001 | user | msg-1', dt='dt-1', index=1, filename='version-1.json'),
            DiffVersionInfo(uid='2', label='2026-04-09 12:02:00 UTC | 00000002 | user | msg-2', dt='dt-2', index=2, filename='version-2.json'),
        ]
        previous_version = DiffVersionInfo(
            uid='1',
            label='2026-04-09 12:01:00 UTC | 00000001 | user | msg-1',
            dt='dt-1',
            index=1,
            filename='version-1.json',
        )
        file_changes = [
            DiffFileChange(action='change', path='src/app.py'),
            DiffFileChange(action='create', path='src/new.py'),
        ]
        mirror = Mock()
        mirror.list_remote_versions_page = AsyncMock(return_value=(versions_page, 3))
        mirror.get_current_synced_version_info = AsyncMock(return_value=None)
        mirror.get_remote_version_info_by_index = AsyncMock(return_value=previous_version)
        mirror.list_version_changes_by_filenames = AsyncMock(return_value=file_changes)
        mirror.get_version_change_detail_by_range = AsyncMock(return_value=DiffDetail(
            path='src/app.py',
            action='change',
            before_label='before',
            after_label='after',
            before_text='old',
            after_text='new',
        ))
        mirror.render_diff_detail = Mock()

        with patch('sshmirror.cli.prompt_choice', side_effect=[_format_version_choice_label(versions_page[1]), 'src/app.py', 'Back', 'Back', 'Back']), \
             patch('sshmirror.cli.clear_n_console_rows') as clear_rows_mock:
            asyncio.run(_show_version_history_cli(mirror))

        clear_rows_mock.assert_called_once_with(1)
        mirror.get_remote_version_info_by_index.assert_awaited_once_with(1)
        mirror.list_version_changes_by_filenames.assert_awaited_once_with('version-1.json', 'version-2.json')
        mirror.get_version_change_detail_by_range.assert_awaited_once_with('version-1.json', 'version-2.json', 1, 2, 'src/app.py')

    def test_show_version_history_cli_returns_to_version_list_after_back(self):
        target_version = DiffVersionInfo(
            uid='2',
            label='2026-04-09 12:02:00 UTC | 00000002 | user | msg-2',
            dt='dt-2',
            index=2,
            filename='version-2.json',
        )
        previous_version = DiffVersionInfo(
            uid='1',
            label='2026-04-09 12:01:00 UTC | 00000001 | user | msg-1',
            dt='dt-1',
            index=1,
            filename='version-1.json',
        )
        mirror = Mock()
        mirror.list_remote_versions_page = AsyncMock(return_value=([target_version], 3))
        mirror.get_current_synced_version_info = AsyncMock(return_value=None)
        mirror.get_remote_version_info_by_index = AsyncMock(return_value=previous_version)

        choose_mock = AsyncMock(side_effect=[target_version, None])
        inspect_mock = AsyncMock()

        with patch('sshmirror.cli._choose_version_interactively', choose_mock), \
             patch('sshmirror.cli._inspect_version_range_cli', inspect_mock), \
             patch('sshmirror.cli.clear_n_console_rows'):
            asyncio.run(_show_version_history_cli(mirror))

        self.assertEqual(choose_mock.await_count, 2)
        inspect_mock.assert_awaited_once_with(mirror, previous_version, target_version)

    def test_show_version_history_cli_returns_to_same_page_after_back(self):
        target_version = DiffVersionInfo(
            uid='26',
            label='2026-04-09 12:26:00 UTC | 00000026 | user | msg-26',
            dt='dt-26',
            index=25,
            filename='version-26.json',
        )
        previous_version = DiffVersionInfo(
            uid='25',
            label='2026-04-09 12:25:00 UTC | 00000025 | user | msg-25',
            dt='dt-25',
            index=24,
            filename='version-25.json',
        )
        mirror = Mock()
        mirror.list_remote_versions_page = AsyncMock(return_value=([target_version], 40))
        mirror.get_current_synced_version_info = AsyncMock(return_value=None)
        mirror.get_remote_version_info_by_index = AsyncMock(return_value=previous_version)

        choose_mock = AsyncMock(side_effect=[target_version, None])
        inspect_mock = AsyncMock()

        with patch('sshmirror.cli._choose_version_interactively', choose_mock), \
             patch('sshmirror.cli._inspect_version_range_cli', inspect_mock), \
             patch('sshmirror.cli.clear_n_console_rows'):
            asyncio.run(_show_version_history_cli(mirror))

        self.assertEqual(choose_mock.await_count, 2)
        self.assertEqual(choose_mock.await_args_list[0].kwargs.get('initial_page'), 0)
        self.assertEqual(choose_mock.await_args_list[1].kwargs.get('initial_page'), 0)
        inspect_mock.assert_awaited_once_with(mirror, previous_version, target_version)

    def test_file_inspection_choice_shows_created_and_deleted_as_non_selectable(self):
        base_versions_page = [
            DiffVersionInfo(uid='1', label='2026-04-09 12:01:00 UTC | 00000001 | user | msg-1', dt='dt-1', index=1, filename='version-1.json'),
            DiffVersionInfo(uid='2', label='2026-04-09 12:02:00 UTC | 00000002 | user | msg-2', dt='dt-2', index=2, filename='version-2.json'),
        ]
        target_versions_page = [
            DiffVersionInfo(uid='2', label='2026-04-09 12:02:00 UTC | 00000002 | user | msg-2', dt='dt-2', index=2, filename='version-2.json'),
        ]
        mirror = Mock()
        mirror.list_remote_versions_page = AsyncMock(side_effect=[
            (base_versions_page, 3),
            (base_versions_page, 3),
            (target_versions_page, 1),
            (base_versions_page, 3),
        ])
        mirror.get_current_synced_version_info = AsyncMock(return_value=None)
        mirror.list_version_changes_by_filenames = AsyncMock(return_value=[
            DiffFileChange(action='change', path='src/app.py'),
            DiffFileChange(action='create', path='src/new.py'),
        ])
        mirror.get_version_change_detail_by_range = AsyncMock(return_value=DiffDetail(
            path='src/app.py',
            action='change',
            before_label='before',
            after_label='after',
            before_text='old',
            after_text='new',
        ))
        mirror.render_diff_detail = Mock()

        with patch('sshmirror.cli.prompt_choice', side_effect=[_format_version_choice_label(base_versions_page[0]), _format_version_choice_label(target_versions_page[0]), 'src/app.py', 'Back', 'Back', 'Back']) as prompt_mock, \
             patch('sshmirror.cli.console.clear') as clear_mock:
            asyncio.run(_show_version_changes_cli(mirror))

        file_prompt_call = next(call for call in prompt_mock.call_args_list if call.args[0] == 'Choose file change to inspect')
        self.assertEqual(file_prompt_call.args[1], ['src/app.py', 'Back'])
        styled_choices = file_prompt_call.kwargs['styled_choices']
        self.assertEqual([choice.value for choice in styled_choices], ['src/app.py', 'src/new.py', 'Back'])
        self.assertFalse(bool(styled_choices[0].disabled))
        self.assertTrue(bool(styled_choices[1].disabled))
        self.assertIn(('class:file_change_changed', ' ~ CHANGED '), list(styled_choices[0].title))
        self.assertIn(('class:file_change_path_changed', 'src/app.py'), list(styled_choices[0].title))
        self.assertIn(('class:file_change_created', ' + CREATED '), list(styled_choices[1].title))
        self.assertIn(('class:file_change_note', '[view only]'), list(styled_choices[1].title))
        clear_mock.assert_called_once()

    def test_file_inspection_choice_falls_back_to_prompt_listing_for_non_selectable_items(self):
        base_versions_page = [
            DiffVersionInfo(uid='1', label='2026-04-09 12:01:00 UTC | 00000001 | user | msg-1', dt='dt-1', index=1, filename='version-1.json'),
            DiffVersionInfo(uid='2', label='2026-04-09 12:02:00 UTC | 00000002 | user | msg-2', dt='dt-2', index=2, filename='version-2.json'),
        ]
        target_versions_page = [
            DiffVersionInfo(uid='2', label='2026-04-09 12:02:00 UTC | 00000002 | user | msg-2', dt='dt-2', index=2, filename='version-2.json'),
        ]
        mirror = Mock()
        mirror.list_remote_versions_page = AsyncMock(side_effect=[
            (base_versions_page, 3),
            (base_versions_page, 3),
            (target_versions_page, 1),
            (base_versions_page, 3),
        ])
        mirror.get_current_synced_version_info = AsyncMock(return_value=None)
        mirror.list_version_changes_by_filenames = AsyncMock(return_value=[
            DiffFileChange(action='create', path='src/new.py'),
            DiffFileChange(action='delete', path='src/old.py'),
        ])
        mirror.get_version_change_detail_by_range = AsyncMock()
        mirror.render_diff_detail = Mock()

        with patch('sshmirror.cli._build_styled_file_change_choice', return_value=None), \
               patch('sshmirror.cli.prompt_choice', side_effect=[_format_version_choice_label(base_versions_page[0]), _format_version_choice_label(target_versions_page[0]), 'Back', 'Back']) as prompt_mock:
            asyncio.run(_show_version_changes_cli(mirror))

        file_prompt_call = next(call for call in prompt_mock.call_args_list if call.args[1] == ['Back'])
        self.assertIn('created | src/new.py (not selectable)', file_prompt_call.args[0])
        self.assertIn('deleted | src/old.py (not selectable)', file_prompt_call.args[0])
        mirror.get_version_change_detail_by_range.assert_not_called()

    def test_current_changes_cli_matches_version_change_display(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                mirror = Mock()
                mirror.list_current_changes = AsyncMock(return_value=[
                    DiffFileChange(action='change', path='src/large.bin', inspectable=False),
                    DiffFileChange(action='change', path='src/app.py', inspectable=True),
                    DiffFileChange(action='delete', path='src/new.py', inspectable=False),
                ])
                mirror.get_current_change_detail = AsyncMock(return_value=DiffDetail(
                    path='src/app.py',
                    action='change',
                    before_label='before',
                    after_label='after',
                    before_text='old',
                    after_text='new',
                ))
                mirror.render_diff_detail = Mock()

                with patch('sshmirror.cli._build_styled_file_change_choice', return_value=None), \
                     patch('sshmirror.cli.prompt_choice', side_effect=['src/app.py', 'Back', 'Back']) as prompt_mock, \
                     patch('sshmirror.cli.console.clear') as clear_mock:
                    asyncio.run(_show_current_changes_cli(mirror))

                file_prompt_call = next(call for call in prompt_mock.call_args_list if call.args[1] == ['src/app.py', 'Back'])
                self.assertIn('changed | src/large.bin (not selectable)', file_prompt_call.args[0])
                self.assertIn('changed | src/app.py', file_prompt_call.args[0])
                self.assertIn('deleted | src/new.py (not selectable)', file_prompt_call.args[0])
                self.assertEqual(file_prompt_call.args[1], ['src/app.py', 'Back'])
                clear_mock.assert_called_once()

    def test_scan_project_maps_with_progress_shows_live_updates(self):
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

                local_map = FileMap()
                local_map.add('src/app.py', 'local-md5')
                remote_map = FileMap()
                remote_map.add('src/app.py', 'remote-md5')
                dummy_conn = object.__new__(SSHClientConnection)
                events: list[str] = []

                class FakeLive:
                    def __init__(self, *args, **kwargs):
                        events.append('live_init')

                    def __enter__(self):
                        events.append('live_enter')
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        events.append('live_exit')
                        return False

                    def update(self, *args, **kwargs):
                        events.append('live_update')

                async def load_local(*args, **kwargs):
                    await asyncio.sleep(0)
                    return local_map

                async def load_remote(*args, **kwargs):
                    await asyncio.sleep(0)
                    return remote_map

                with patch('sshmirror.sshmirror.Live', FakeLive), \
                     patch.object(mirror.filewatcher, 'get_filemap', new=AsyncMock(side_effect=load_local)), \
                     patch.object(mirror, '_get_remote_map', new=AsyncMock(side_effect=load_remote)):
                    scanned_local, scanned_remote = asyncio.run(
                        mirror._scan_project_maps_with_progress(
                            dummy_conn,
                            local_reference_map=None,
                            remote_reference_map=None,
                            title='Verify',
                            subtitle='Scanning projects',
                        )
                    )

                self.assertIs(scanned_local, local_map)
                self.assertIs(scanned_remote, remote_map)
                self.assertEqual(events[:2], ['live_init', 'live_enter'])
                self.assertIn('live_update', events)
                self.assertEqual(events[-1], 'live_exit')

    def test_build_version_choice_map_uses_numeric_shortcuts(self):
        page_versions = [
            DiffVersionInfo(uid='a', label='2026-04-09 12:01:00 UTC | abcdef12 | alice | first', dt='dt-a', index=122, author='alice', message='first'),
            DiffVersionInfo(uid='b', label='2026-04-09 12:02:00 UTC | bcdef123 | bob | second', dt='dt-b', index=123, author='bob', message='second'),
        ]

        choice_map = _build_version_choice_map(page_versions)

        self.assertEqual(list(choice_map.keys())[0], '  123 | 2026-04-09 12:01:00 UTC | alice        | a        | first                                   ')
        self.assertEqual(list(choice_map.keys())[1], '  124 | 2026-04-09 12:02:00 UTC | bob          | b        | second                                  ')

    def test_build_version_choice_map_marks_current_synced_version(self):
        current_version = DiffVersionInfo(uid='b', label='2026-04-09 12:02:00 UTC | bcdef123 | bob | second', dt='dt-b', index=123, author='bob', message='second')
        page_versions = [
            DiffVersionInfo(uid='a', label='2026-04-09 12:01:00 UTC | abcdef12 | alice | first', dt='dt-a', index=122, author='alice', message='first'),
            current_version,
        ]

        choice_map = _build_version_choice_map(page_versions, current_version=current_version)

        self.assertEqual(list(choice_map.keys())[0], '  123 | 2026-04-09 12:01:00 UTC | alice        | a        | first                                   ')
        self.assertEqual(list(choice_map.keys())[1], '  124 | 2026-04-09 12:02:00 UTC | bob          | b        | second                                    ◀ current')

    def test_version_choice_label_uses_fixed_width_for_all_columns(self):
        version = DiffVersionInfo(
            uid='1234567890abcdef',
            label='2026-04-09 12:34:56 UTC | 12345678 | extraordinarily-long-author | message that is much longer than the fixed width',
            dt='2026-04-09 12:34:56 UTC',
            index=7,
            author='extraordinarily-long-author',
            message='message that is much longer than the fixed width',
        )

        self.assertEqual(
            _format_version_choice_label(version),
            '    8 | 2026-04-09 12:34:56 UTC | extraordi... | 12345678 | message that is much longer than the ...',
        )

    def test_styled_version_choice_uses_fixed_author_column_width(self):
        short_author = DiffVersionInfo(
            uid='abc12345',
            label='2026-04-09 12:01:00 UTC | abc12345 | bob | first',
            dt='dt-a',
            index=1,
            author='bob',
            message='first',
        )
        long_author = DiffVersionInfo(
            uid='def67890',
            label='2026-04-09 12:02:00 UTC | def67890 | verylongusername | second',
            dt='dt-b',
            index=2,
            author='verylongusername',
            message='second',
        )

        short_choice = _build_styled_version_choice(short_author)
        long_choice = _build_styled_version_choice(long_author)

        self.assertIsNotNone(short_choice)
        self.assertIsNotNone(long_choice)
        self.assertIn(('class:magenta', 'bob         '), list(short_choice.title))
        self.assertIn(('class:magenta', 'verylongu...'), list(long_choice.title))
        self.assertIn(('class:cyan', 'abc12345'), list(short_choice.title))
        self.assertIn(('', 'first                                   '), list(short_choice.title))

    def test_styled_version_choice_marks_current_synced_version(self):
        version = DiffVersionInfo(
            uid='abc12345',
            label='2026-04-09 12:01:00 UTC | abc12345 | bob | first',
            dt='dt-a',
            index=1,
            author='bob',
            message='first',
        )

        choice = _build_styled_version_choice(version, is_current=True)

        self.assertIsNotNone(choice)
        self.assertIn(('class:current_marker', '  ● current'), list(choice.title))
        self.assertEqual(choice.value, _format_version_choice_display_label(version, current_version=version))

    def test_render_version_page_returns_choice_map(self):
        page_versions = [
            DiffVersionInfo(
                uid='abcdef12',
                label='2026-04-09 12:34:56 UTC | abcdef12 | alice | deploy api',
                dt='2026-04-09T12:34:56+00:00',
                author='alice',
                message='deploy api',
            )
        ]

        choice_map = _render_version_page(page_versions, 'Choose base version')

        self.assertEqual(len(choice_map), 1)
        key = list(choice_map.keys())[0]
        self.assertIn('alice', key)
        self.assertIn('abcdef12', key)

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

    def test_restart_container_local_mode_rejects_ssh_connection_fields(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                with self.assertRaisesRegex(ValueError, 'restart_container.local'):
                    SSHMirror(
                        config=SSHMirrorConfig(
                            host='127.0.0.1',
                            port=22,
                            username='root',
                            localdir='.',
                            remotedir='/app',
                            restart_container={
                                'local': True,
                                'host': '192.168.1.10',
                                'container_name': 'app',
                            },
                        )
                    )

    def test_restart_container_local_mode_does_not_use_ssh_connection_settings(self):
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
                            'local': True,
                            'container_name': 'app',
                        },
                    )
                )

                self.assertTrue(mirror._restart_container_is_local())
                with self.assertRaisesRegex(ValueError, 'does not use SSH connection settings'):
                    mirror._get_restart_container_connect_kwargs()

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

    def test_test_connection_runs_local_restart_container_check_when_configured(self):
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
                            'local': True,
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
                restart_test_mock.assert_awaited_once()

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

    def test_maybe_restart_container_uses_local_docker_when_local_mode_is_enabled(self):
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
                            'local': True,
                            'container_name': 'app',
                        },
                    )
                )

                with patch.object(mirror, '_confirm', return_value=True), \
                     patch.object(mirror, '_run_local_checked', new=AsyncMock()) as local_run_mock, \
                     patch('sshmirror.sshmirror.clear_n_console_rows'), \
                     patch('sshmirror.sshmirror.asyncssh.connect', side_effect=AssertionError('local restart must not use SSH')):
                    asyncio.run(mirror._maybe_restart_container())

                local_run_mock.assert_awaited_once_with(
                    'docker restart app',
                    'restart_container restart failed for app',
                )

    def test_cli_help_mentions_docker_host_for_restart_connection(self):
        help_text = build_parser().format_help()
        normalized_help_text = re.sub(r'\x1b\[[0-9;]*m', '', help_text)
        normalized_help_text = ' '.join(normalized_help_text.split())

        self.assertIn('configured Docker host', normalized_help_text)

    def test_windows_can_use_interactive_questionary_menu(self):
        with patch('sshmirror.prompts.questionary', object()), \
             patch('sshmirror.prompts.os.name', 'nt'), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            self.assertTrue(_questionary_available())

    def test_questionary_remains_available_during_running_event_loop(self):
        with patch('sshmirror.prompts.questionary', object()), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True), \
             patch('sshmirror.prompts.asyncio.get_running_loop', return_value=object()):
            self.assertTrue(_questionary_available())

    def test_prompt_choice_treats_questionary_none_as_user_abort(self):
        question = Mock()
        question.ask.return_value = None
        questionary_mock = Mock()
        questionary_mock.select.return_value = question

        with patch('sshmirror.prompts.questionary', questionary_mock), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True), \
             patch('builtins.input') as input_mock:
            with self.assertRaises(UserAbort):
                prompt_choice('Choose', ['one', 'two'])

        input_mock.assert_not_called()

    def test_fallback_confirm_ctrl_c_raises_user_abort(self):
        with patch('sshmirror.prompts.questionary', None), \
             patch('builtins.input', side_effect=KeyboardInterrupt):
            with self.assertRaises(UserAbort):
                prompt_confirm('Proceed?')

    def test_fallback_confirm_accepts_russian_yes(self):
        with patch('sshmirror.prompts.questionary', None), \
             patch('builtins.input', return_value='да'):
            self.assertTrue(prompt_confirm('Proceed?'))

    def test_fallback_confirm_accepts_wrong_layout_no(self):
        with patch('sshmirror.prompts.questionary', None), \
             patch('builtins.input', return_value='ytn'):
            self.assertFalse(prompt_confirm('Proceed?'))

    def test_fallback_confirm_tracks_extra_lines_after_retry(self):
        with patch('sshmirror.prompts._questionary_ask', return_value=_PROMPT_FALLBACK), \
             patch('sshmirror.prompts._read_plain_input', side_effect=['maybe', 'yes']):
            self.assertTrue(prompt_confirm('Proceed?'))

        self.assertEqual(consume_confirm_retry_extra_lines(), 2)
        self.assertEqual(consume_confirm_retry_extra_lines(), 0)

    def test_questionary_confirm_accepts_wrong_layout_yes(self):
        question = Mock()
        question.ask.return_value = 'lf'
        questionary_mock = Mock()
        questionary_mock.text.return_value = question

        with patch('sshmirror.prompts.questionary', questionary_mock), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True):
            self.assertTrue(prompt_confirm('Proceed?'))

    def test_interactive_menu_uses_plain_labels(self):
        menu_items = _build_interactive_menu_items(
            has_config=True,
            has_ignore=True,
            initialized=True,
            has_stash=False,
        )
        labels = [label for label, _action in menu_items]

        self.assertIn('Sync local and remote', labels)
        self.assertIn('Versions...', labels)
        self.assertNotIn('View version changes', labels)
        self.assertIn('Test connection', labels)
        self.assertIn('Discard...', labels)
        self.assertNotIn('Discard all local changes', labels)
        self.assertNotIn('Discard selected files', labels)
        self.assertNotIn('Force pull', labels)
        self.assertIn('Exit', labels)

    def test_interactive_versions_menu_can_open_history(self):
        args = build_parser().parse_args([])

        with patch('sshmirror.cli._find_default_cli_path', return_value='sshmirror.config.yml'), \
             patch('sshmirror.cli._is_sshmirror_initialized', return_value=True), \
             patch('sshmirror.cli._has_stashed_changes', return_value=False), \
             patch('sshmirror.cli.prompt_choice', side_effect=['Versions...', 'History']):
            configured_args = _configure_interactive_args(args)

        self.assertTrue(configured_args.version_history)
        self.assertFalse(configured_args.version_diff)

    def test_interactive_discard_menu_can_open_discard_selected_files(self):
        args = build_parser().parse_args([])

        with patch('sshmirror.cli._find_default_cli_path', return_value='sshmirror.config.yml'), \
             patch('sshmirror.cli._is_sshmirror_initialized', return_value=True), \
             patch('sshmirror.cli._has_stashed_changes', return_value=False), \
             patch('sshmirror.cli.prompt_choice', side_effect=['Discard...', 'Discard selected files']), \
             patch('sshmirror.cli.prompt_discard_files', return_value=['src/app.py']):
            configured_args = _configure_interactive_args(args)

        self.assertEqual(configured_args.discard_files, ['src/app.py'])
        self.assertFalse(configured_args.discard)

    def test_interactive_menu_exit_is_graceful(self):
        args = build_parser().parse_args([])

        with patch('sshmirror.cli._find_default_cli_path', return_value='sshmirror.config.yml'), \
             patch('sshmirror.cli._is_sshmirror_initialized', return_value=True), \
             patch('sshmirror.cli._has_stashed_changes', return_value=False), \
             patch('sshmirror.cli.prompt_choice', return_value='Exit'):
            configured_args = _configure_interactive_args(args)

        self.assertTrue(configured_args.exit_requested)

    def test_interactive_mode_prints_version_subtly(self):
        with patch('sshmirror.cli.console.print') as print_mock:
            _print_interactive_version()

        print_mock.assert_called_once_with(f'SSHMirror v{__version__}', style='dim')

    def test_main_ctrl_c_exits_cleanly_without_error(self):
        captured_handler = {}
        configured_args = argparse.Namespace(exit_requested=True)

        def capture_signal(_sig, handler):
            captured_handler['handler'] = handler

        with patch('sshmirror.cli.signal.signal', side_effect=capture_signal), \
             patch('sshmirror.cli._configure_interactive_args', return_value=configured_args), \
             patch('sshmirror.cli.console.print') as print_mock, \
             patch('sshmirror.cli.sys.argv', ['sshmirror']):
            result = main()

        self.assertEqual(result, 0)
        with self.assertRaises(SystemExit) as exit_info:
            captured_handler['handler']()
        self.assertEqual(exit_info.exception.code, 0)
        print_mock.assert_not_called()

    def test_main_interactive_userabort_cancellation_exits_cleanly_without_error(self):
        with patch('sshmirror.cli._configure_interactive_args', side_effect=UserAbort('Cancelled by user')), \
             patch('sshmirror.cli.console.print') as print_mock, \
             patch('sshmirror.cli.sys.argv', ['sshmirror']):
            result = main()

        self.assertEqual(result, 0)
        print_mock.assert_not_called()

    def test_silent_user_abort_detects_cancel_message(self):
        self.assertTrue(_is_silent_user_abort(UserAbort('Cancelled by user')))
        self.assertTrue(_is_silent_user_abort(UserAbort('')))
        self.assertFalse(_is_silent_user_abort(UserAbort('Push cancelled by user')))

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

    def test_status_renders_overview_and_section_panels(self):
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

                prevstate = FileMap()
                prevstate.add('tracked.txt', 'md5-old', size=3, mtime=1)
                local_state = FileMap()
                local_state.add('tracked.txt', 'md5-local', size=4, mtime=2)
                remote_map = FileMap()
                remote_map.add('tracked.txt', 'md5-remote', size=5, mtime=3)

                local_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='localversion123456',
                    author='tester',
                    message='baseline',
                    filemap=prevstate,
                )
                remote_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 11, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='remoteversion12345',
                    author='tester',
                    message='remote update',
                    filemap=remote_map,
                )

                class DummyConn:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                with patch.object(mirror, '_load_prevstate', AsyncMock(return_value=prevstate)), \
                     patch.object(mirror, '_get_local_version', AsyncMock(return_value=local_version)), \
                     patch.object(mirror, '_get_remote_versions_stack', AsyncMock(return_value=[local_version, remote_version])), \
                     patch.object(mirror, '_scan_project_maps_with_progress', AsyncMock(return_value=(local_state, remote_map))), \
                     patch('sshmirror.sshmirror.asyncssh.connect', return_value=DummyConn()), \
                     patch('sshmirror.sshmirror.console.print') as print_mock:
                    asyncio.run(mirror.status())

                panels = [call.args[0] for call in print_mock.call_args_list if call.args and isinstance(call.args[0], Panel)]
                self.assertEqual([panel.title for panel in panels], ['Status', 'Local changes', 'Remote changes', 'Live local vs remote'])

    def test_status_passes_remote_verify_reference_to_scan(self):
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

                prevstate = FileMap()
                local_state = FileMap()
                remote_map = FileMap()
                local_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='localversion123456',
                    author='tester',
                    message='baseline',
                    filemap=prevstate,
                )
                remote_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 11, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='remoteversion12345',
                    author='tester',
                    message='remote update',
                    filemap=remote_map,
                )

                class DummyConn:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                scan_mock = AsyncMock(return_value=(local_state, remote_map))
                with patch.object(mirror, '_load_prevstate', AsyncMock(return_value=prevstate)), \
                     patch.object(mirror, '_get_local_version', AsyncMock(return_value=local_version)), \
                     patch.object(mirror, '_get_remote_versions_stack', AsyncMock(return_value=[local_version, remote_version])), \
                     patch.object(mirror, '_scan_project_maps_with_progress', scan_mock), \
                     patch('sshmirror.sshmirror.asyncssh.connect', return_value=DummyConn()), \
                     patch('sshmirror.sshmirror.console.print'):
                    asyncio.run(mirror.status())

                self.assertTrue(scan_mock.await_count == 1)
                self.assertTrue(scan_mock.call_args.kwargs.get('remote_verify_reference', False))

    def test_status_overview_shows_remote_snapshot_mismatch(self):
        mirror = SSHMirror(
            config=SSHMirrorConfig(
                host='127.0.0.1',
                port=22,
                username='root',
                localdir='.',
                remotedir='/app',
            )
        )

        prevstate = FileMap()
        local_version = None
        remote_versions = []
        live_diff = Migration(FileMap(), FileMap())
        mismatch_map = FileMap()
        mismatch_map.add('changed.txt', 'md5-changed', size=6, mtime=1)
        migration = Migration(remote_versions[0].filemap if remote_versions else FileMap(), mismatch_map) if remote_versions else Migration(FileMap(), mismatch_map)

        panel = mirror._render_status_overview(
            initialized=True,
            has_stash=False,
            has_conflicts=False,
            prevstate_exists=False,
            local_version=local_version,
            remote_versions=[
                DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 11, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='remoteversion12345',
                    author='tester',
                    message='remote update',
                    filemap=FileMap(),
                )
            ],
            live_diff=live_diff,
            local_version_missing_remote=False,
            remote_snapshot_matches=False,
            remote_snapshot_migration=Migration(FileMap(), mismatch_map),
        )

        console = Console(record=True)
        console.print(panel)
        output = console.export_text()
        self.assertIn('Remote snapshot', output)
        self.assertIn('drift detected', output)

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
            self.assertIn('# local: true', content)
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
        self.assertIn('--discard', result.stdout)
        self.assertIn('--test-connection', result.stdout)
        self.assertNotIn('--force-pull', result.stdout)

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

    def test_push_renders_preview_before_confirmation_and_live_updates(self):
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

                origin = FileMap()
                target = FileMap()
                target.add('src/app.py', 'abc')
                migration = Migration(origin, target)
                version = mirror._create_version(target)
                dummy_conn = object.__new__(SSHClientConnection)
                events: list[str] = []

                class FakeLive:
                    def __init__(self, *args, **kwargs):
                        events.append('live_init')

                    def __enter__(self):
                        events.append('live_enter')
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        events.append('live_exit')
                        return False

                    def update(self, *args, **kwargs):
                        pass

                with patch('sshmirror.sshmirror.console.print', side_effect=lambda *args, **kwargs: events.append('print')), \
                     patch('sshmirror.sshmirror.clear_n_console_rows', side_effect=lambda *args, **kwargs: events.append('clear')), \
                     patch.object(mirror, '_get_renderable_line_count', return_value=5), \
                     patch('sshmirror.sshmirror.Live', FakeLive), \
                     patch.object(mirror, '_confirm', side_effect=lambda *args, **kwargs: events.append('confirm') or True), \
                     patch.object(mirror, '_prompt_version_message', side_effect=lambda: events.append('message') or 'feature sync'), \
                     patch.object(mirror, '_remote_create_downgrade', new=AsyncMock()), \
                     patch.object(mirror, '_run_commands', new=AsyncMock()), \
                     patch.object(mirror, '_remote_mk_dir', new=AsyncMock()), \
                     patch.object(mirror, '_delete_directories', new=AsyncMock()), \
                     patch.object(mirror, '_upload_files', new=AsyncMock()), \
                     patch.object(mirror, '_delete_files', new=AsyncMock()), \
                     patch.object(mirror, '_set_remote_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_prevstate', new=AsyncMock()):
                    asyncio.run(mirror._push(version, migration, dummy_conn))

                self.assertEqual(
                    events[:6],
                    ['print', 'confirm', 'clear', 'message', 'live_init', 'live_enter'],
                )

    def test_push_clears_preview_after_confirm_retry_lines(self):
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

                origin = FileMap()
                target = FileMap()
                target.add('src/app.py', 'abc')
                migration = Migration(origin, target)
                version = mirror._create_version(target)
                dummy_conn = object.__new__(SSHClientConnection)

                class FakeLive:
                    def __init__(self, *args, **kwargs):
                        pass

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                    def update(self, *args, **kwargs):
                        pass

                def confirm_with_retry_lines(*_args, **_kwargs):
                    mirror._last_confirm_retry_extra_lines = 2
                    return True

                with patch('sshmirror.sshmirror.console.print'), \
                     patch('sshmirror.sshmirror.clear_n_console_rows') as clear_rows_mock, \
                     patch.object(mirror, '_get_renderable_line_count', return_value=5), \
                     patch('sshmirror.sshmirror.Live', FakeLive), \
                     patch.object(mirror, '_confirm', side_effect=confirm_with_retry_lines), \
                     patch.object(mirror, '_prompt_version_message', return_value='feature sync'), \
                     patch.object(mirror, '_remote_create_downgrade', new=AsyncMock()), \
                     patch.object(mirror, '_run_commands', new=AsyncMock()), \
                     patch.object(mirror, '_remote_mk_dir', new=AsyncMock()), \
                     patch.object(mirror, '_delete_directories', new=AsyncMock()), \
                     patch.object(mirror, '_upload_files', new=AsyncMock()), \
                     patch.object(mirror, '_delete_files', new=AsyncMock()), \
                     patch.object(mirror, '_set_remote_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_prevstate', new=AsyncMock()):
                    asyncio.run(mirror._push(version, migration, dummy_conn))

                clear_rows_mock.assert_called_once_with(3)

    def test_pull_renders_preview_before_confirmation_and_live_updates(self):
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

                origin = FileMap()
                target = FileMap()
                target.add('src/app.py', 'abc')
                migration = Migration(origin, target)
                version = mirror._create_version(target)
                dummy_conn = object.__new__(SSHClientConnection)
                events: list[str] = []

                class FakeLive:
                    def __init__(self, *args, **kwargs):
                        events.append('live_init')

                    def __enter__(self):
                        events.append('live_enter')
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                    def update(self, *args, **kwargs):
                        pass

                with patch('sshmirror.sshmirror.console.print', side_effect=lambda *args, **kwargs: events.append('print')), \
                     patch('sshmirror.sshmirror.clear_n_console_rows', side_effect=lambda *args, **kwargs: events.append('clear')), \
                     patch.object(mirror, '_get_renderable_line_count', return_value=5), \
                     patch('sshmirror.sshmirror.Live', FakeLive), \
                     patch.object(mirror, '_confirm', side_effect=lambda *args, **kwargs: events.append('confirm') or True), \
                     patch.object(mirror, '_run_commands', new=AsyncMock()), \
                     patch.object(mirror, '_download_files', new=AsyncMock()), \
                     patch.object(mirror, '_save_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_prevstate', new=AsyncMock()):
                    asyncio.run(mirror._pull([version], migration, None, dummy_conn))

                self.assertEqual(
                    events[:5],
                    ['print', 'confirm', 'clear', 'live_init', 'live_enter'],
                )

    def test_pull_clears_preview_after_confirm_retry_lines(self):
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

                origin = FileMap()
                target = FileMap()
                target.add('src/app.py', 'abc')
                migration = Migration(origin, target)
                version = mirror._create_version(target)
                dummy_conn = object.__new__(SSHClientConnection)

                class FakeLive:
                    def __init__(self, *args, **kwargs):
                        pass

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                    def update(self, *args, **kwargs):
                        pass

                def confirm_with_retry_lines(*_args, **_kwargs):
                    mirror._last_confirm_retry_extra_lines = 2
                    return True

                with patch('sshmirror.sshmirror.console.print'), \
                     patch('sshmirror.sshmirror.clear_n_console_rows') as clear_rows_mock, \
                     patch.object(mirror, '_get_renderable_line_count', return_value=5), \
                     patch('sshmirror.sshmirror.Live', FakeLive), \
                     patch.object(mirror, '_confirm', side_effect=confirm_with_retry_lines), \
                     patch.object(mirror, '_run_commands', new=AsyncMock()), \
                     patch.object(mirror, '_download_files', new=AsyncMock()), \
                     patch.object(mirror, '_save_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_prevstate', new=AsyncMock()):
                    asyncio.run(mirror._pull([version], migration, None, dummy_conn))

                clear_rows_mock.assert_called_once_with(3)

    def test_downgrade_renders_preview_before_confirmation_and_live_updates(self):
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

                current_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 1, tzinfo=datetime.timezone.utc),
                    uid='current-version',
                    filemap=FileMap(),
                )
                target_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='target-version',
                    filemap=FileMap(),
                )
                migration_changes = MigrationChanges(
                    directories=Difference(changed=[], created=[], deleted=['old-dir']),
                    files=Difference(changed=['src/app.py'], created=[], deleted=['src/old.py']),
                )
                dummy_conn = object.__new__(SSHClientConnection)
                events: list[str] = []

                class FakeLive:
                    def __init__(self, *args, **kwargs):
                        events.append('live_init')

                    def __enter__(self):
                        events.append('live_enter')
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                    def update(self, *args, **kwargs):
                        pass

                with patch('sshmirror.sshmirror.console.print', side_effect=lambda *args, **kwargs: events.append('print')), \
                     patch('sshmirror.sshmirror.clear_n_console_rows', side_effect=lambda *args, **kwargs: events.append('clear')), \
                     patch.object(mirror, '_get_renderable_line_count', return_value=5), \
                     patch('sshmirror.sshmirror.Live', FakeLive), \
                     patch.object(mirror, '_confirm', side_effect=lambda *args, **kwargs: events.append('confirm') or True), \
                     patch.object(mirror, '_run_remote_script_from_project_root', new=AsyncMock()), \
                     patch.object(mirror, 'force_pull', new=AsyncMock()) as force_pull_mock, \
                     patch.object(mirror, '_maybe_restart_container', new=AsyncMock()) as restart_mock:
                    asyncio.run(mirror._downgrade_remote(dummy_conn, current_version, target_version, migration_changes))

                self.assertEqual(
                    events[:5],
                    ['print', 'confirm', 'clear', 'live_init', 'live_enter'],
                )
                force_pull_mock.assert_awaited_once_with(require_confirm=False)
                restart_mock.assert_awaited_once()

    def test_run_skips_full_project_compare_after_push_without_sync_commands(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                (tmp_path / 'existing.txt').write_text('before\n', encoding='utf-8')
                (tmp_path / 'new.txt').write_text('after\n', encoding='utf-8')

                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                prevstate = FileMap()
                prevstate.add('existing.txt', 'old-md5')
                state = FileMap()
                state.add('existing.txt', 'old-md5')
                state.add('new.txt', 'new-md5')

                local_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='local-version',
                    filemap=prevstate,
                )
                pushed_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 1, tzinfo=datetime.timezone.utc),
                    uid='pushed-version',
                    filemap=state,
                )

                class DummyConn:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                filemap_mock = AsyncMock(return_value=state)

                async def push_with_lock(*args, **kwargs):
                    await kwargs['before_sync']()

                with patch('sshmirror.sshmirror.asyncssh.connect', return_value=DummyConn()), \
                     patch('sshmirror.sshmirror.clear_n_console_rows'), \
                     patch.object(mirror, '_validate_conflicts', new=AsyncMock(return_value=True)), \
                     patch.object(mirror, '_sync_ignore_file_before_transfer', new=AsyncMock()), \
                     patch.object(mirror, '_load_or_create_prevstate', new=AsyncMock(return_value=prevstate)), \
                     patch.object(mirror.filewatcher, 'get_filemap', new=filemap_mock), \
                     patch.object(mirror, '_remote_mk_dir', new=AsyncMock()), \
                     patch.object(mirror, '_get_local_version', new=AsyncMock(return_value=local_version)), \
                     patch.object(mirror, '_get_remote_versions_stack', new=AsyncMock(return_value=[local_version])), \
                     patch.object(mirror, '_create_version', return_value=pushed_version), \
                     patch.object(mirror, '_push', new=AsyncMock()) as push_mock, \
                     patch.object(mirror, '_get_remote_map', new=AsyncMock(side_effect=AssertionError('full compare should be skipped'))), \
                     patch.object(mirror, '_confirm', return_value=False):
                    asyncio.run(mirror.run())

                push_mock.assert_awaited_once()
                self.assertEqual(filemap_mock.await_count, 1)

    def test_run_uses_downgrade_preview_flow(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                (tmp_path / 'existing.txt').write_text('before\n', encoding='utf-8')

                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                        downgrade=True,
                    )
                )

                prevstate = FileMap()
                prevstate.add('existing.txt', 'old-md5')
                state = FileMap()
                state.add('existing.txt', 'old-md5')

                previous_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='previous-version',
                    filemap=prevstate,
                )
                current_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 1, tzinfo=datetime.timezone.utc),
                    uid='current-version',
                    filemap=state,
                )
                migration_changes = MigrationChanges(
                    directories=Difference(changed=[], created=['new-dir'], deleted=[]),
                    files=Difference(changed=['existing.txt'], created=[], deleted=[]),
                )

                class DummyConn:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                filemap_mock = AsyncMock(return_value=state)

                with patch('sshmirror.sshmirror.asyncssh.connect', return_value=DummyConn()), \
                     patch('sshmirror.sshmirror.clear_n_console_rows'), \
                     patch.object(mirror, '_validate_conflicts', new=AsyncMock(return_value=True)), \
                     patch.object(mirror, '_sync_ignore_file_before_transfer', new=AsyncMock()), \
                     patch.object(mirror, '_load_or_create_prevstate', new=AsyncMock(return_value=prevstate)), \
                     patch.object(mirror.filewatcher, 'get_filemap', new=filemap_mock), \
                     patch.object(mirror, '_remote_mk_dir', new=AsyncMock()), \
                     patch.object(mirror, '_get_local_version', new=AsyncMock(return_value=current_version)), \
                     patch.object(mirror, '_get_remote_versions_stack', new=AsyncMock(return_value=[previous_version, current_version])), \
                     patch.object(mirror, '_get_remote_migration_changes', new=AsyncMock(return_value=migration_changes)), \
                     patch.object(mirror, '_downgrade_remote', new=AsyncMock()) as downgrade_mock:
                    asyncio.run(mirror.run())

                downgrade_mock.assert_awaited_once()
                self.assertIs(downgrade_mock.await_args.args[1], current_version)
                self.assertIs(downgrade_mock.await_args.args[2], previous_version)
                self.assertEqual(
                    downgrade_mock.await_args.args[3].model_dump(),
                    migration_changes.inversed().model_dump(),
                )

    def test_run_prompts_restart_container_after_push_when_full_compare_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                (tmp_path / 'existing.txt').write_text('before\n', encoding='utf-8')
                (tmp_path / 'new.txt').write_text('after\n', encoding='utf-8')

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

                prevstate = FileMap()
                prevstate.add('existing.txt', 'old-md5')
                state = FileMap()
                state.add('existing.txt', 'old-md5')
                state.add('new.txt', 'new-md5')

                local_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='local-version',
                    filemap=prevstate,
                )
                pushed_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 1, tzinfo=datetime.timezone.utc),
                    uid='pushed-version',
                    filemap=state,
                )

                class DummyConn:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                filemap_mock = AsyncMock(return_value=state)

                with patch('sshmirror.sshmirror.asyncssh.connect', return_value=DummyConn()), \
                     patch('sshmirror.sshmirror.clear_n_console_rows'), \
                     patch.object(mirror, '_validate_conflicts', new=AsyncMock(return_value=True)), \
                     patch.object(mirror, '_sync_ignore_file_before_transfer', new=AsyncMock()), \
                     patch.object(mirror, '_load_or_create_prevstate', new=AsyncMock(return_value=prevstate)), \
                     patch.object(mirror.filewatcher, 'get_filemap', new=filemap_mock), \
                     patch.object(mirror, '_remote_mk_dir', new=AsyncMock()), \
                     patch.object(mirror, '_get_local_version', new=AsyncMock(return_value=local_version)), \
                     patch.object(mirror, '_get_remote_versions_stack', new=AsyncMock(return_value=[local_version])), \
                     patch.object(mirror, '_create_version', return_value=pushed_version), \
                     patch.object(mirror, '_push', new=AsyncMock()) as push_mock, \
                     patch.object(mirror, '_get_remote_map', new=AsyncMock(side_effect=AssertionError('full compare should be skipped'))), \
                     patch.object(mirror, '_confirm', side_effect=[False]) as confirm_mock:
                    asyncio.run(mirror.run())

                push_mock.assert_awaited_once()
                confirm_mock.assert_called_once_with('Restart docker container?', 'Restart container choice is required')

    def test_version_message_accepts_exactly_50_characters(self):
        message = 'x' * 50

        normalized = SSHMirror._normalize_version_message(message)

        self.assertEqual(normalized, message)

    def test_version_message_rejects_empty_value(self):
        with self.assertRaisesRegex(ValueError, 'Version description is required'):
            SSHMirror._normalize_version_message('   ')

    def test_version_message_rejects_more_than_50_characters(self):
        with self.assertRaisesRegex(ValueError, 'at most 50 characters'):
            SSHMirror._normalize_version_message('x' * 51)

    def test_prompt_version_message_uses_max_length_hint(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_mock = Mock(return_value='short message')
            callbacks = SSHMirrorCallbacks(text=prompt_mock)
            with working_directory(tmp_path):
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

                message = mirror._prompt_version_message()

            self.assertEqual(message, 'short message')

    def test_acquire_remote_sync_lock_rejects_existing_active_lock(self):
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

                class Result:
                    def __init__(self, exit_status: int = 0, stdout: str = '', stderr: str = ''):
                        self.exit_status = exit_status
                        self.stdout = stdout
                        self.stderr = stderr

                dummy_conn = Mock(spec=SSHClientConnection)
                dummy_conn.run = AsyncMock(side_effect=[
                    Result(exit_status=1, stderr='exists'),
                    Result(stdout=str(int(time.time()))),
                    Result(stdout=json.dumps({
                        'author': 'teammate',
                        'client_host': 'other-pc',
                        'username': 'deploy',
                        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    })),
                ])

                with patch.object(mirror, '_cleanup_stale_remote_sync_lock', new=AsyncMock(return_value=False)), \
                     patch.object(mirror, '_run_remote_checked', new=AsyncMock()), \
                     patch.object(mirror, '_write_remote_text_file', new=AsyncMock()):
                    with self.assertRaisesRegex(RemoteSyncLockError, 'Another client is already synchronizing this project'):
                        asyncio.run(mirror._acquire_remote_sync_lock(dummy_conn))

                self.assertFalse(mirror._remote_sync_lock_active)

    def test_acquire_remote_sync_lock_removes_stale_lock_and_starts_heartbeat(self):
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

                class Result:
                    def __init__(self, exit_status: int = 0, stdout: str = '', stderr: str = ''):
                        self.exit_status = exit_status
                        self.stdout = stdout
                        self.stderr = stderr

                stale_seconds = int(time.time()) - SSHMirror.REMOTE_SYNC_LOCK_TTL_SECONDS - 5
                dummy_conn = Mock(spec=SSHClientConnection)
                dummy_conn.run = AsyncMock(side_effect=[
                    Result(stdout=str(stale_seconds)),
                    Result(stdout=json.dumps({'author': 'stale-user'})),
                    Result(),
                    Result(),
                ])

                with patch.object(mirror, '_run_remote_checked', new=AsyncMock()), \
                     patch.object(mirror, '_write_remote_text_file', new=AsyncMock()) as write_remote_text_file_mock, \
                     patch('sshmirror.sshmirror.console.print') as console_print_mock:
                    asyncio.run(mirror._acquire_remote_sync_lock(dummy_conn))
                    self.assertTrue(mirror._remote_sync_lock_active)
                    write_remote_text_file_mock.assert_awaited_once()
                    console_print_mock.assert_called_once()
                    asyncio.run(mirror._release_remote_sync_lock(dummy_conn))

                self.assertFalse(mirror._remote_sync_lock_active)

    def test_push_acquires_sync_lock_after_version_message(self):
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

                origin = FileMap()
                target = FileMap()
                target.add('src/app.py', 'abc')
                migration = Migration(origin, target)
                version = mirror._create_version(target)
                dummy_conn = object.__new__(SSHClientConnection)
                events: list[str] = []

                async def before_sync():
                    events.append('lock')

                with patch.object(mirror, '_confirm', side_effect=lambda *args, **kwargs: events.append('confirm') or True), \
                     patch.object(mirror, '_prompt_version_message', side_effect=lambda: events.append('message') or 'feature sync'), \
                     patch.object(mirror, '_remote_create_downgrade', new=AsyncMock(side_effect=lambda *args, **kwargs: events.append('sync-start'))), \
                     patch.object(mirror, '_run_commands', new=AsyncMock()), \
                     patch.object(mirror, '_remote_mk_dir', new=AsyncMock()), \
                     patch.object(mirror, '_delete_directories', new=AsyncMock()), \
                     patch.object(mirror, '_upload_files', new=AsyncMock()), \
                     patch.object(mirror, '_delete_files', new=AsyncMock()), \
                     patch.object(mirror, '_set_remote_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_version', new=AsyncMock()), \
                     patch.object(mirror, '_save_prevstate', new=AsyncMock()):
                    asyncio.run(mirror._push(version, migration, dummy_conn, before_sync=before_sync))

                self.assertEqual(events[:4], ['confirm', 'message', 'lock', 'sync-start'])

    def test_run_releases_sync_lock_after_sync(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with working_directory(tmp_path):
                (tmp_path / 'existing.txt').write_text('before\n', encoding='utf-8')
                (tmp_path / 'new.txt').write_text('after\n', encoding='utf-8')

                mirror = SSHMirror(
                    config=SSHMirrorConfig(
                        host='127.0.0.1',
                        port=22,
                        username='root',
                        localdir='.',
                        remotedir='/app',
                    )
                )

                prevstate = FileMap()
                prevstate.add('existing.txt', 'old-md5')
                state = FileMap()
                state.add('existing.txt', 'old-md5')
                state.add('new.txt', 'new-md5')

                local_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 0, tzinfo=datetime.timezone.utc),
                    uid='local-version',
                    filemap=prevstate,
                )
                pushed_version = DirVersion(
                    dt=datetime.datetime(2026, 4, 10, 10, 0, 1, tzinfo=datetime.timezone.utc),
                    uid='pushed-version',
                    filemap=state,
                )

                class DummyConn:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                    async def run(self, *args, **kwargs):
                        return Mock(exit_status=0, stdout='')

                filemap_mock = AsyncMock(return_value=state)

                async def push_with_lock(*args, **kwargs):
                    await kwargs['before_sync']()

                with patch('sshmirror.sshmirror.asyncssh.connect', return_value=DummyConn()), \
                     patch('sshmirror.sshmirror.clear_n_console_rows'), \
                     patch.object(mirror, '_validate_conflicts', new=AsyncMock(return_value=True)), \
                     patch.object(mirror, '_sync_ignore_file_before_transfer', new=AsyncMock()), \
                     patch.object(mirror, '_load_or_create_prevstate', new=AsyncMock(return_value=prevstate)), \
                     patch.object(mirror.filewatcher, 'get_filemap', new=filemap_mock), \
                     patch.object(mirror, '_remote_mk_dir', new=AsyncMock()), \
                     patch.object(mirror, '_get_local_version', new=AsyncMock(return_value=local_version)), \
                     patch.object(mirror, '_get_remote_versions_stack', new=AsyncMock(return_value=[local_version])), \
                     patch.object(mirror, '_create_version', return_value=pushed_version), \
                     patch.object(mirror, '_acquire_remote_sync_lock', new=AsyncMock()) as acquire_lock_mock, \
                     patch.object(mirror, '_release_remote_sync_lock', new=AsyncMock()) as release_lock_mock, \
                     patch.object(mirror, '_push', new=AsyncMock(side_effect=push_with_lock)) as push_mock, \
                     patch.object(mirror, '_get_remote_map', new=AsyncMock(side_effect=AssertionError('full compare should be skipped'))), \
                     patch.object(mirror, '_confirm', return_value=False):
                    asyncio.run(mirror.run())

                push_mock.assert_awaited_once()
                acquire_lock_mock.assert_awaited_once()
                release_lock_mock.assert_awaited_once()

    def test_prompt_version_message_requires_text_callback(self):
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
                    ),
                )

                with self.assertRaisesRegex(UserAbort, 'Version description is required'):
                    mirror._prompt_version_message()


if __name__ == '__main__':
    unittest.main()