from dataclasses import dataclass, field, asdict
import typing
import uuid
import typing
from pydantic import BaseModel
from rich.console import Console

console = Console()

class Difference(BaseModel):
    changed: list[str]
    created: list[str]
    deleted: list[str]
    
    def empty(self):
        return len(self.changed) == 0 and len(self.created) == 0 and len(self.deleted) == 0
    
    def all(self):
        return self.changed + self.created + self.deleted
    
    def intersect(self, other: 'Difference') -> list[str]:
        return [item for item in self.all() if item in other.all()]
    
    def inversed(self):
        return Difference(
            changed=self.changed,
            created=self.deleted,
            deleted=self.created
        )
        
    def print(self, indent=0):
        if len(self.changed) > 0:
            console.print('\n'.join([' '*indent + 'change ' + f for f in self.changed]), style='violet')
        if len(self.created) > 0:
            console.print('\n'.join([' '*indent + 'create ' + f for f in self.created]), style='green')
        if len(self.deleted) > 0:
            console.print('\n'.join([' '*indent + 'delete ' + f for f in self.deleted]), style='red')


class MigrationChanges(BaseModel):
    directories: Difference
    files: Difference
    
    def print(self):
        console.print('Migration:', style='blue')
        if len(self.files.all()) > 0:
            console.print('  files:', style='yellow')
        self.files.print(4)
            
        if len(self.directories.all()) > 0:
            console.print('  directories:', style='yellow')
        self.directories.print(4)
            
    def inversed(self):
        return MigrationChanges(
            directories=self.directories.inversed(),
            files=self.files.inversed()
        )


class DiffFileChange(BaseModel):
    action: str
    path: str


class DiffVersionInfo(BaseModel):
    uid: str
    label: str
    dt: str
    author: str | None = None


class DiffDetail(BaseModel):
    path: str
    action: str
    before_label: str
    after_label: str
    before_text: str | None = None
    after_text: str | None = None
    before_entry: dict[str, typing.Any] | None = None
    after_entry: dict[str, typing.Any] | None = None
    is_large: bool = False
    text_available: bool = True
    message: str | None = None
    
@dataclass
class Command:
    local_command: typing.Optional[str] = None
    remote_command: typing.Optional[str] = None
    on_directory_change: typing.Optional[str] = None
    ask: bool = False
    name: typing.Optional[str] = None
    
    
@dataclass
class CmdConfig:
    after_pull: list[Command]
    before_pull: list[Command]
    after_push: list[Command]
    before_push: list[Command]
    
    
@dataclass
class CopyPath:
    origin: str
    destination: str
