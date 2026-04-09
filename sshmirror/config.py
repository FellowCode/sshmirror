from __future__ import annotations

from dataclasses import dataclass, field
import typing

import yaml

try:
    from .core.schemas import CmdConfig, Command
    from .core.utils import read_text_file
except ImportError:
    from core.schemas import CmdConfig, Command
    from core.utils import read_text_file


@dataclass(slots=True)
class SSHMirrorCallbacks:
    confirm: typing.Callable[[str], bool] | None = None
    choose: typing.Callable[[str, list[str]], str] | None = None
    text: typing.Callable[[str, str], str] | None = None
    secret: typing.Callable[[str], str] | None = None


@dataclass(slots=True)
class SSHMirrorConfig:
    host: str | None = None
    port: int = 22
    username: str | None = None
    password: str | None = None
    private_key: str | None = None
    private_key_passphrase: str | None = None
    localdir: str | None = None
    remotedir: str | None = None
    ignore: str | None = None
    restart_container: dict[str, typing.Any] | None = None
    aliases: dict[str, typing.Any] = field(default_factory=dict)
    watch: bool = False
    no_sync: bool = False
    author: str | None = None
    pull_only: bool = False
    downgrade: bool = False
    discard_files: list[str] | None = None
    commands: CmdConfig = field(default_factory=lambda: CmdConfig(after_pull=[], before_pull=[], after_push=[], before_push=[]))

    @staticmethod
    def _require_non_empty_string(value: typing.Any, field_name: str) -> str:
        if not isinstance(value, str) or value.strip() == '':
            raise ValueError(f'Config field {field_name!r} must be a non-empty string')
        return value.strip()

    @staticmethod
    def _normalize_port(value: typing.Any, field_name: str) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'Config field {field_name!r} must be an integer port') from exc

        if port < 1 or port > 65535:
            raise ValueError(f'Config field {field_name!r} must be between 1 and 65535')
        return port

    def _validate_restart_container(self) -> dict[str, typing.Any] | None:
        if self.restart_container is None:
            return None
        if not isinstance(self.restart_container, dict):
            raise ValueError("Config field 'restart_container' must be a mapping")

        restart_container = dict(self.restart_container)
        if 'user' in restart_container:
            raise ValueError("Config field 'restart_container.user' is no longer supported. Use 'restart_container.username'")

        username_value = restart_container.get('username')
        if username_value is not None:
            restart_container['username'] = self._require_non_empty_string(username_value, 'restart_container.username')

        if 'container_name' not in restart_container:
            raise ValueError("Config field 'restart_container.container_name' is required")
        restart_container['container_name'] = self._require_non_empty_string(
            restart_container.get('container_name'),
            'restart_container.container_name',
        )

        connection_keys = {'host', 'port', 'username'}
        provided_connection_keys = {
            key for key in connection_keys
            if key in restart_container and restart_container.get(key) not in (None, '')
        }
        if provided_connection_keys and provided_connection_keys != connection_keys:
            raise ValueError(
                "If restart_container uses a separate Docker host, specify 'host', 'port', and 'username' together"
            )

        if 'host' in restart_container and restart_container.get('host') not in (None, ''):
            restart_container['host'] = self._require_non_empty_string(restart_container.get('host'), 'restart_container.host')
        if 'port' in restart_container and restart_container.get('port') not in (None, ''):
            restart_container['port'] = self._normalize_port(restart_container.get('port'), 'restart_container.port')

        if 'sudo' in restart_container and not isinstance(restart_container.get('sudo'), bool):
            raise ValueError("Config field 'restart_container.sudo' must be true or false")

        return restart_container

    def validate(self) -> SSHMirrorConfig:
        self.host = self._require_non_empty_string(self.host, 'host')
        self.port = self._normalize_port(self.port, 'port')
        self.username = self._require_non_empty_string(self.username, 'username')
        self.localdir = self._require_non_empty_string(self.localdir, 'localdir')
        self.remotedir = self._require_non_empty_string(self.remotedir, 'remotedir')

        if self.watch and self.no_sync:
            raise ValueError("Only one of 'watch' and 'no_sync' can be enabled")

        self.restart_container = self._validate_restart_container()
        return self

    @staticmethod
    def parse_cmd_config(cmd_config: dict[str, typing.Any]) -> CmdConfig:
        def parse_command_by_type(runtype: str) -> list[Command]:
            return [Command(**item) for item in cmd_config.get(runtype, [])]

        data = {
            runtype: parse_command_by_type(runtype)
            for runtype in ['before_pull', 'after_pull', 'before_push', 'after_push']
        }
        return CmdConfig(**data)

    @classmethod
    def from_file(
        cls,
        path: str,
        *,
        password: str | None = None,
        private_key: str | None = None,
        private_key_passphrase: str | None = None,
        ignore: str | None = None,
        author: str | None = None,
        pull_only: bool = False,
        downgrade: bool = False,
        discard_files: list[str] | None = None,
        aliases: dict[str, typing.Any] | None = None,
        watch: bool = False,
        no_sync: bool = False,
    ) -> SSHMirrorConfig:
        data: dict[str, typing.Any] = yaml.load(read_text_file(path), yaml.CLoader)
        if not isinstance(data, dict):
            raise ValueError('Config file must contain a YAML mapping at the top level')
        return cls(
            host=data.get('host'),
            port=data.get('port', 22),
            username=data.get('username'),
            password=data.get('password', password),
            private_key=data.get('private_key', data.get('ssh_key', private_key)),
            private_key_passphrase=data.get(
                'private_key_passphrase',
                data.get('ssh_key_passphrase', private_key_passphrase),
            ),
            localdir=data.get('localdir'),
            remotedir=data.get('remotedir'),
            ignore=data.get('ignore', ignore),
            restart_container=data.get('restart_container'),
            aliases=aliases or {},
            watch=watch,
            no_sync=no_sync,
            author=data.get('author', author),
            pull_only=pull_only,
            downgrade=downgrade,
            discard_files=discard_files,
            commands=cls.parse_cmd_config(data.get('commands', {})),
        ).validate()