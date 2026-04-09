import asyncio
import getpass
import os
import typing

from rich.console import Console

try:
    from .core.exceptions import UserAbort
except ImportError:
    from core.exceptions import UserAbort

try:
    import questionary
except Exception:
    questionary = None


console = Console()
_PROMPT_FALLBACK = object()


def _questionary_available() -> bool:
    import sys

    return questionary is not None and sys.stdin.isatty() and sys.stdout.isatty()


def _questionary_ask(question_factory: typing.Callable[[], typing.Any], fallback_message: str) -> typing.Any:
    if not _questionary_available():
        return _PROMPT_FALLBACK

    try:
        question = question_factory()
        result = question.ask()
        if result is None:
            raise UserAbort('Cancelled by user')
        return result
    except UserAbort:
        raise
    except (KeyboardInterrupt, EOFError) as exc:
        raise UserAbort('Cancelled by user') from exc
    except Exception as exc:
        console.print(f'{fallback_message}: {exc}', style='yellow')
        return _PROMPT_FALLBACK


def _read_plain_input(prompt: str) -> str:
    try:
        return input(prompt)
    except (KeyboardInterrupt, EOFError) as exc:
        raise UserAbort('Cancelled by user') from exc


def _read_hidden_input(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except (KeyboardInterrupt, EOFError) as exc:
        raise UserAbort('Cancelled by user') from exc


def _fallback_choice_prompt(prompt: str, choices: list[str], default: str | None = None) -> str:
    print(prompt)
    for index, choice in enumerate(choices, start=1):
        print(f'  {index}. {choice}')

    while True:
        value = _read_plain_input('Choose action: ').strip()
        if value == '' and default in choices:
            return default
        if value.isdigit():
            selected_index = int(value) - 1
            if 0 <= selected_index < len(choices):
                return choices[selected_index]

        if value in choices:
            return value

        console.print('Invalid choice, try again', style='yellow')


def _fallback_confirm_prompt(prompt: str) -> bool:
    while True:
        value = _read_plain_input(f'{prompt} (yes/no): ').strip().lower()
        if value in ['yes', 'y']:
            return True
        if value in ['no', 'n']:
            return False
        console.print('Please answer yes or no', style='yellow')


def prompt_choice(prompt: str, choices: list[str], default: str | None = None) -> str:
    result = _questionary_ask(
        lambda: questionary.select(prompt, choices=choices, default=default),
        'Interactive questionary prompt is unavailable, fallback to plain input',
    )
    if result is not _PROMPT_FALLBACK:
        return result

    return _fallback_choice_prompt(prompt, choices, default=default)


def prompt_confirm(prompt: str) -> bool:
    result = _questionary_ask(
        lambda: questionary.confirm(prompt, default=False),
        'Interactive questionary confirm is unavailable, fallback to plain input',
    )
    if result is not _PROMPT_FALLBACK:
        return bool(result)

    return _fallback_confirm_prompt(prompt)


def prompt_text(prompt: str, default: str = 'update') -> str:
    result = _questionary_ask(
        lambda: questionary.text(prompt, default=default),
        'Interactive questionary input is unavailable, fallback to plain input',
    )
    if result is not _PROMPT_FALLBACK:
        value = result.strip()
        return value or default

    value = _read_plain_input(f'{prompt} [{default}]: ').strip()
    return value or default


def prompt_secret(prompt: str) -> str:
    result = _questionary_ask(
        lambda: questionary.password(prompt),
        'Interactive questionary password input is unavailable, fallback to hidden input',
    )
    if result is not _PROMPT_FALLBACK:
        return result

    return _read_hidden_input(f'{prompt}: ').strip()


def prompt_discard_files() -> list[str]:
    while True:
        if _questionary_available():
            result = _questionary_ask(
                lambda: questionary.text('Files to reload from remote (comma separated)'),
                'Interactive questionary input is unavailable, fallback to plain input',
            )
            if result is not _PROMPT_FALLBACK:
                value = result.strip()
            else:
                value = _read_plain_input('Files to reload from remote (comma separated): ').strip()
        else:
            value = _read_plain_input('Files to reload from remote (comma separated): ').strip()

        items = [item.strip() for item in value.split(',') if item.strip()]
        if items:
            return items
        console.print('Specify at least one file', style='yellow')


def prompt_initialization_source() -> str:
    return prompt_choice(
        'Which version is newer?',
        ['My local version', 'Remote server version'],
    )