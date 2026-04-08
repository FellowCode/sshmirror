import locale
import os
import re
import uuid

import aiofiles


def read_text_file(path: str) -> str:
    encodings = ['utf-8-sig', locale.getpreferredencoding(False), 'cp1251']
    attempted: list[str] = []

    for encoding in encodings:
        if encoding in attempted:
            continue
        attempted.append(encoding)
        try:
            with open(path, 'r', encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue

    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def write_text_file_atomic(path: str, content: str, encoding: str = 'utf-8'):
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(directory, f'.{os.path.basename(path)}.{uuid.uuid4().hex}.tmp')
    try:
        with open(tmp_path, 'w', encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def write_text_file_atomic_async(path: str, content: str, encoding: str = 'utf-8'):
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(directory, f'.{os.path.basename(path)}.{uuid.uuid4().hex}.tmp')
    try:
        async with aiofiles.open(tmp_path, 'w', encoding=encoding) as f:
            await f.write(content)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def check_path_is_ignored(path: str, ignore_list: list[re.Pattern]):
    
    for ignore_p in ignore_list:
        if ignore_p.match(path):
            # print(path, True)
            return True
    # print(path, False)
    return False

DEFAULT_IGNORE = [
    '.sshmirror'
]

def parse_ignore_file(path) -> list[re.Pattern]:
    ignore_list = []
    if path:
        for line in read_text_file(path).splitlines() + DEFAULT_IGNORE:
            line = line.strip()
            if len(line) == 0 or line.startswith('#'):
                continue
            line = line.replace('\n', '')
            line = line.replace('.', '\\.')
            line = line.replace('*', '.*')
            if line.endswith('/'):
                line = line[:-1]
            ignore_list.append(re.compile(line + '(/.*)?'))
                
    return ignore_list

def clear_n_console_rows(n):
    print(f'\033[{n}A', end='')
    print('\033[J', end='')
