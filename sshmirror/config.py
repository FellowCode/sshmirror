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
        return cls(
            host=data['host'],
            port=int(data['port']),
            username=data['username'],
            password=data.get('password', password),
            private_key=data.get('private_key', data.get('ssh_key', private_key)),
            private_key_passphrase=data.get(
                'private_key_passphrase',
                data.get('ssh_key_passphrase', private_key_passphrase),
            ),
            localdir=data['localdir'],
            remotedir=data['remotedir'],
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
        )