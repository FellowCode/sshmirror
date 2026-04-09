import fnmatch
import locale
import os
import uuid
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    normalized: str
    directory_only: bool
    has_slash: bool


def _normalize_ignore_path(path: str) -> str:
    normalized = path.replace('\\', '/').strip()
    while normalized.startswith('./'):
        normalized = normalized[2:]
    return normalized.strip('/')


def _match_component_rule(path: str, rule: IgnoreRule) -> bool:
    parts = [part for part in path.split('/') if part]
    if rule.directory_only:
        return any(fnmatch.fnmatch(part, rule.normalized) for part in parts[:-1])
    return any(fnmatch.fnmatch(part, rule.normalized) for part in parts)


def check_path_is_ignored(path: str, ignore_list: list[IgnoreRule]):
    normalized_path = _normalize_ignore_path(path)
    if normalized_path == '':
        return False

    for ignore_rule in ignore_list:
        if ignore_rule.has_slash:
            if ignore_rule.directory_only:
                if normalized_path == ignore_rule.normalized or normalized_path.startswith(ignore_rule.normalized + '/'):
                    return True
                if fnmatch.fnmatch(normalized_path, ignore_rule.normalized + '/*'):
                    return True
            else:
                if normalized_path == ignore_rule.normalized or normalized_path.startswith(ignore_rule.normalized + '/'):
                    return True
                if fnmatch.fnmatch(normalized_path, ignore_rule.normalized):
                    return True
            continue

        if _match_component_rule(normalized_path, ignore_rule):
            return True

    return False

DEFAULT_IGNORE = [
    '.sshmirror'
]


def parse_ignore_file(path) -> list[IgnoreRule]:
    ignore_list: list[IgnoreRule] = []
    lines = DEFAULT_IGNORE.copy()
    if path and os.path.exists(path):
        lines = read_text_file(path).splitlines() + lines

    for line in lines:
        line = line.strip()
        if len(line) == 0 or line.startswith('#'):
            continue

        directory_only = line.endswith('/')
        normalized = _normalize_ignore_path(line[:-1] if directory_only else line)
        if normalized == '':
            continue

        ignore_list.append(
            IgnoreRule(
                normalized=normalized,
                directory_only=directory_only,
                has_slash='/' in normalized,
            )
        )

    return ignore_list

def clear_n_console_rows(n):
    print(f'\033[{n}A', end='')
    print('\033[J', end='')
