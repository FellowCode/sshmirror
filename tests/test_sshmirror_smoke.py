import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from sshmirror import SSHMirror, SSHMirrorCallbacks, SSHMirrorConfig, UserAbort, __version__
from sshmirror.cli import build_parser
from sshmirror.core.schemas import DiffDetail


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
                self.assertTrue((tmp_path / '.sshmirror' / 'versions').exists())

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

    def test_cli_help_mentions_docker_host_for_restart_connection(self):
        help_text = build_parser().format_help()
        normalized_help_text = re.sub(r'\x1b\[[0-9;]*m', '', help_text)
        normalized_help_text = ' '.join(normalized_help_text.split())

        self.assertIn('configured Docker host', normalized_help_text)

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


if __name__ == '__main__':
    unittest.main()