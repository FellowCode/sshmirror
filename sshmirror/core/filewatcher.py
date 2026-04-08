import os
from .filemap import FileMap
import inspect
import re
import typing
import asyncio
from .utils import check_path_is_ignored, parse_ignore_file



class Filewatcher:
    def __init__(self, directory, ignore_file_path=None):
        self.directory = directory
        self.ignore_file_path = ignore_file_path
        self.last_filemap = None
        
    @classmethod
    async def _scantree(cls, path: str, ignore_list: list[re.Pattern]):
        """Recursively yield DirEntry objects for given directory."""
        for entry in os.scandir(path):
            path = entry.path.replace('\\', '/')
            if path.startswith('./'):
                path = path[2:]
            if check_path_is_ignored(path, ignore_list):
                continue
            if entry.is_dir(follow_symlinks=False):
                yield entry
                if entry.is_dir():
                    await asyncio.sleep(0)
                    async for entry2 in cls._scantree(entry.path, ignore_list):
                        yield entry2
            else:
                yield entry
    
    @classmethod
    async def _create_map(cls, directory: str, ignore_file_path: str, reference_map: FileMap | None = None) -> FileMap:
        ignore_list = parse_ignore_file(ignore_file_path) if ignore_file_path is not None else []
        filemap = FileMap()
        async for entry in cls._scantree(directory, ignore_list):
            path = entry.path.replace('\\', '/')
            if path.startswith('./'):
                path = path[2:]
            if len(path) == 0:
                continue
            if entry.is_dir():
                filemap.add_directory(path)
            else:
                await filemap.add_file(path, reference_map=reference_map)
        return filemap
    
    async def get_filemap(self, reference_map: FileMap | None = None) -> FileMap:
        self.last_filemap = await self._create_map(
            self.directory,
            self.ignore_file_path,
            reference_map=reference_map or self.last_filemap,
        )
        return self.last_filemap
                
    async def look_changes(self, callback, blocking=True) -> typing.Optional[FileMap]:
        if blocking:
            await self._look_changes_blocking(callback)
        else:
            await self._look_changes_non_blocking(callback)
            
    async def _look_changes_blocking(self, callback):
        while True:
            await self._look_changes_non_blocking(callback)
            await asyncio.sleep(.5)
            
            
    async def _look_changes_non_blocking(self, callback) -> FileMap:
        new_filemap = await self._create_map(self.directory, self.ignore_file_path, reference_map=self.last_filemap)
        if self.last_filemap is None:
            self.last_filemap = new_filemap
            return self.last_filemap
        
        
        dirs_diff = new_filemap.dirs_diff(self.last_filemap)
        files_diff = new_filemap.files_diff(self.last_filemap)
        if not files_diff.empty() or not dirs_diff.empty():
            if inspect.iscoroutinefunction(callback):
                await callback(new_filemap, dirs_diff, files_diff)
            else:
                callback(new_filemap, dirs_diff, files_diff)
                
        self.last_filemap = new_filemap
        return self.last_filemap