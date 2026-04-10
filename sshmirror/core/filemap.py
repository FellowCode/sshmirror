from __future__ import annotations

from .._version import DIR_VERSION_FORMAT
from .schemas import Difference, MigrationChanges
from .exceptions import IncompatibleVersionFormat
import aiofiles
import hashlib
import json
from dataclasses import dataclass, field, asdict
import datetime
import os
import uuid
import typing
from .utils import check_path_is_ignored, compile_ignore_rules, parse_ignore_file
from rich.console import Console

console = Console()


@dataclass
class FileEntry:
    md5: str | None = None
    size: int | None = None
    mtime: int | None = None

    def asdict(self):
        return {
            'md5': self.md5,
            'size': self.size,
            'mtime': self.mtime,
        }

    @classmethod
    def from_dict(cls, data: str | dict[str, typing.Any]):
        if isinstance(data, str):
            return cls(md5=data)
        return cls(
            md5=data.get('md5'),
            size=data.get('size'),
            mtime=data.get('mtime'),
        )

    def stat_matches(self, size: int, mtime: int) -> bool:
        return self.md5 is not None and self.size == size and self.mtime == mtime

@dataclass
class DirVersion:
    dt: datetime.datetime
    filemap: 'FileMap'
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    author: typing.Optional[str] = None
    message: str = 'update'
    created_by_sshmirror_version: str | None = None
    version_format: int = DIR_VERSION_FORMAT
    
    def asdict(self):
        return {
            'dt': self.dt.isoformat(),
            'uid': self.uid,
            'author': self.author,
            'message': self.message,
            'created_by_sshmirror_version': self.created_by_sshmirror_version,
            'version_format': self.version_format,
            'filemap': self.filemap.asdict()
        }
    
    def dumps(self):
        return json.dumps(self.asdict(), indent=4)
    
    @classmethod
    def from_dict(cls, d: dict):
        data = dict(d)
        version_format = int(data.get('version_format', DIR_VERSION_FORMAT))
        if version_format > DIR_VERSION_FORMAT:
            created_by = data.get('created_by_sshmirror_version') or 'a newer sshmirror version'
            raise IncompatibleVersionFormat(
                f'Version metadata format {version_format} was created by {created_by}. Update sshmirror to open this project safely.'
            )

        data['dt'] = datetime.datetime.fromisoformat(data['dt']).replace(tzinfo=datetime.timezone.utc)
        data['filemap'] = FileMap.from_dict(data['filemap'])
        if 'message' not in data or not data['message']:
            data['message'] = 'update'
        data.setdefault('created_by_sshmirror_version', None)
        data['version_format'] = version_format

        allowed_keys = {
            'dt',
            'filemap',
            'uid',
            'author',
            'message',
            'created_by_sshmirror_version',
            'version_format',
        }
        return cls(**{key: value for key, value in data.items() if key in allowed_keys})
    
    @classmethod
    def loads(cls, s: str):
        return cls.from_dict(json.loads(s))
    
    def filename(self):
        return f'{self.name()}.json'
    
    def name(self):
        return f'{self.dt.strftime("%Y-%m-%d_%H-%M-%S.%f")}_{self.uid}'
        
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DirVersion):
            raise ValueError("Cannot compare DirVersion with other class")
        return self.uid == other.uid
    
    def __ne__(self, other: object) -> bool:
        if not isinstance(other, DirVersion):
            raise ValueError("Cannot compare DirVersion with other class")
        return self.uid != other.uid
    
    def __lt__(self, other) -> bool:
        if not isinstance(other, DirVersion):
            raise ValueError("Cannot compare DirVersion with other class")
        return self.dt < other.dt
    
    def __gt__(self, other) -> bool:
        if not isinstance(other, DirVersion):
            raise ValueError("Cannot compare DirVersion with other class")
        return self.dt > other.dt
    
@dataclass
class Conflicts:
    remote_version_uid: str
    files: list[str]
    dirs: list[str]
    local_suffix: str = '._local'
    
    def empty(self):
        return len(self.files + self.dirs) == 0
    
    def all(self):
        return self.files + self.dirs
    
    def remove(self, path: str):
        if path in self.files:
            self.files.remove(path)
        elif path in self.dirs:
            self.dirs.remove(path)
        else:
            raise ValueError(f'Error remove path from conflicts, "{path}" not found')
    
    def asdict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict):
        if 'local_suffix' not in data:
            data['local_suffix'] = '._local'
        return cls(**data)
    
    def dumps(self):
        return json.dumps(self.asdict(), indent=4)
    
    @classmethod
    def loads(self, s: str):
        return self.from_dict(json.loads(s))
    
    def __contains__(self, item: str):
        if type(item) != str:
            raise ValueError('Conflicts support in only for str')
        return item in self.files or item in self.dirs


class Migration:
    def __init__(self, origin: 'FileMap', to: 'FileMap'):
        self._origin = origin
        self._to = to
        self.files = to.files_diff(origin)
        self.dirs = to.dirs_diff(origin)
        
    @property
    def actions(self) -> list[tuple[str, str]]:
        """return list[(path, action)]"""
        actions = [(p, 'create directory ' + p) for p in self.dirs.created]
        actions += [(p, 'delete directory ' + p) for p in self.dirs.deleted]
        actions += [(p, 'delete ' + p) for p in self.files.deleted]
        actions += [(p, 'create ' + p) for p in self.files.created]
        actions += [(p, 'update ' + p) for p in self.files.changed]
        return actions
    
    def print_actions(self, conflicts: typing.Optional[Conflicts] = None, prefix=''):
        for path, action in self.actions:
            if conflicts and (path in conflicts):
                action += ' (conflict)'
            if action.startswith('create'):
                style = 'green'
            elif action.startswith('delete'):
                style = 'red'
            elif action.startswith('update'):
                style = 'violet'
            else:
                style = None
            console.print(prefix + action, style=style)
            
    def print(self):
        self.changes().print()
    
    def empty(self):
        return len(self.dirs.all() + self.files.all()) == 0
    
    def conflicts(self, remote_version: DirVersion, migration: 'Migration') -> Conflicts:
        files = self.files.intersect(migration.files)
        dirs = self.dirs.intersect(migration.dirs)
        return Conflicts(remote_version_uid=remote_version.uid, files=files, dirs=dirs)
    
    def changes(self) -> MigrationChanges:
        return MigrationChanges(directories=self.dirs, files=self.files)
        
    def asdict(self):
        return {
            'origin': self._origin.asdict(),
            'to': self._to.asdict(),
        }
        
    def dumps(self):
        return json.dumps(self.asdict(), indent=4, ensure_ascii=False)
    
    @classmethod
    def from_dict(cls, data) -> 'Migration':
        return cls(DirVersion.from_dict(data['origin']), DirVersion.from_dict(data['to']))
    
    @classmethod
    def loads(cls, s: str) -> 'Migration':
        return cls.from_dict(json.loads(s))
    
    def __str__(self):
        s = 'Migration:\n'
        if len(self.files.all()) > 0:
            s += '  files:\n' + '\n'.join([' '*4 + f for f in self.files.all()])
        if len(self.dirs.all()) > 0:
            s += '  directories:\n' + '\n'.join([' '*4 + f for f in self.dirs.all()])
        return s


class FileMap:
    initiated = False
    ignore_file_path: str | None = None
    
    def __init__(self):
        assert self.initiated
        self.path_entries = {}
        self.md5_path = {}
        self.directories = set()
        
    @classmethod
    def init(cls, ignore_file_path: str):
        cls.ignore_file_path = ignore_file_path
        cls.initiated = True
        
    async def add_file(self, path, reference_map: 'FileMap' | None = None):
        assert len(path) > 0
        try:
            stat_result = os.stat(path)
            size = int(stat_result.st_size)
            mtime = int(stat_result.st_mtime_ns)
            reference_entry = reference_map.get_file(path) if reference_map is not None else None
            if reference_entry is not None and reference_entry.stat_matches(size, mtime):
                self.add(path, reference_entry.md5, size=size, mtime=mtime)
                return
            async with aiofiles.open(path, 'rb') as f:
                md5 = hashlib.md5(await f.read()).hexdigest()
            self.add(path, md5, size=size, mtime=mtime)
        except OSError as e:
            if not str(e).startswith('[Errno 22]'):
                print(e)
            
    def add_directory(self, path):
        assert len(path) > 0
        self.directories.add(path)
        
    def add(self, path, md5: str | None, size: int | None = None, mtime: int | None = None):
        assert len(path) > 0
        entry = FileEntry(md5=md5, size=size, mtime=mtime)
        self.path_entries[path] = entry
        if md5 is not None:
            self.md5_path[md5] = path

    def get_file(self, path: str) -> FileEntry | None:
        return self.path_entries.get(path)
        
    def get_by_md5(self, md5):
        return self.md5_path.get(md5)
    
    def get_by_path(self, path):
        entry = self.path_entries.get(path)
        if entry is None:
            return None
        return entry.md5

    @staticmethod
    def _entries_equal(current: FileEntry, target: FileEntry) -> bool:
        if current.md5 is not None and target.md5 is not None:
            return current.md5 == target.md5
        if current.size is not None and target.size is not None and current.mtime is not None and target.mtime is not None:
            return current.size == target.size and current.mtime == target.mtime
        return False
    
    def files_diff(self, other_filemap: 'FileMap') -> Difference:
        ignore_list = compile_ignore_rules(parse_ignore_file(self.ignore_file_path))
        changed = []
        deleted = []
        created = []
        for path, entry in self.path_entries.items():
            if check_path_is_ignored(path, ignore_list):
                continue
            target_entry = other_filemap.get_file(path)
            if target_entry is None:
                created.append(path)
            elif not self._entries_equal(entry, target_entry):
                changed.append(path)
        
        for path in other_filemap.path_list():
            if check_path_is_ignored(path, ignore_list):
                continue
            if self.get_by_path(path) is None:
                deleted.append(path)
            
        return Difference(changed=changed, created=created, deleted=deleted)
    
    def dirs_diff(self, other_filemap: 'FileMap'):
        ignore_list = compile_ignore_rules(parse_ignore_file(self.ignore_file_path))
        created_dirs = []
        deleted_dirs = []
        for directory in self.directories:
            if check_path_is_ignored(directory, ignore_list, is_dir=True):
                continue
            if directory not in other_filemap.directories:
                created_dirs.append(directory)
        for directory in other_filemap.directories:
            if check_path_is_ignored(directory, ignore_list, is_dir=True):
                continue
            if directory not in self.directories:
                deleted_dirs.append(directory)
        return Difference(changed=[], created=created_dirs, deleted=deleted_dirs)
    
    def migrate_to(self, other_filemap: 'FileMap') -> Migration:
        """return dirs_diff, files_diff"""
        return Migration(self, other_filemap)
    
    def path_list(self, startswith=None):
        if startswith:
            return [path for path in self.path_entries.keys() if path.startswith(startswith)]
        return [path for path in self.path_entries.keys()]
    
    def hash(self):
        data = {'dirs': list(self.directories),
            'files': {path: entry.asdict() for path, entry in self.path_entries.items()}}
        j = json.dumps(data)
        return hashlib.sha1(j.encode()).hexdigest()
    
    def __str__(self):
        return f'FileMap(directories_count={len(self.directories)}, files_count={len(self.path_entries)})'
    
    def dumps(self) -> str:
        return json.dumps(self.asdict(), indent=4)
    
    def asdict(self) -> dict:
        return {
            'directories': list(self.directories),
            'files': {path: entry.asdict() for path, entry in self.path_entries.items()}
        }
    
    @classmethod
    def loads(cls, s: str) -> 'FileMap':
        data = json.loads(s)
        return cls.from_dict(data)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'FileMap':
        filemap = cls()
        for directory in data['directories']:
            filemap.add_directory(directory)
        for path, file_data in data['files'].items():
            entry = FileEntry.from_dict(file_data)
            filemap.add(path, entry.md5, size=entry.size, mtime=entry.mtime)
        return filemap
    
    def __eq__(self, other) -> bool:
        if not isinstance(other, self.__class__):
            raise ValueError(f"Cannot compare {self.__class__.__name__} with other class")
        return self.hash() == other.hash()
    
    def __ne__(self, other) -> bool:
        if not isinstance(other, self.__class__):
            raise ValueError(f"Cannot compare {self.__class__.__name__} with other class")
        return self.hash() != other.hash()