import asyncio
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


def _questionary_available() -> bool:
    import sys

    if questionary is None or not sys.stdin.isatty() or not sys.stdout.isatty():
        return False

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True

    return False


def _questionary_ask(question_factory: typing.Callable[[], typing.Any], fallback_message: str) -> typing.Any:
    if not _questionary_available():
        return None

    try:
        question = question_factory()
        return question.ask()
    except KeyboardInterrupt as exc:
        raise UserAbort('Cancelled by user') from exc
    except Exception as exc:
        console.print(f'{fallback_message}: {exc}', style='yellow')
        return None


def _fallback_choice_prompt(prompt: str, choices: list[str]) -> str:
    print(prompt)
    for index, choice in enumerate(choices, start=1):
        print(f'  {index}. {choice}')

    while True:
        value = input('Choose action: ').strip()
        if value.isdigit():
            selected_index = int(value) - 1
            if 0 <= selected_index < len(choices):
                return choices[selected_index]

        if value in choices:
            return value

        console.print('Invalid choice, try again', style='yellow')


def _fallback_confirm_prompt(prompt: str) -> bool:
    while True:
        value = input(f'{prompt} (yes/no): ').strip().lower()
        if value in ['yes', 'y']:
            return True
        if value in ['no', 'n']:
            return False
        console.print('Please answer yes or no', style='yellow')


def prompt_choice(prompt: str, choices: list[str]) -> str:
    result = _questionary_ask(
        lambda: questionary.select(prompt, choices=choices),
        'Interactive questionary prompt is unavailable, fallback to plain input',
    )
    if result is not None:
        return result

    return _fallback_choice_prompt(prompt, choices)


def prompt_confirm(prompt: str) -> bool:
    result = _questionary_ask(
        lambda: questionary.confirm(prompt, default=False),
        'Interactive questionary confirm is unavailable, fallback to plain input',
    )
    if result is not None:
        return bool(result)

    return _fallback_confirm_prompt(prompt)


def prompt_text(prompt: str, default: str = 'update') -> str:
    result = _questionary_ask(
        lambda: questionary.text(prompt, default=default),
        'Interactive questionary input is unavailable, fallback to plain input',
    )
    if result is not None:
        value = result.strip()
        return value or default

    value = input(f'{prompt} [{default}]: ').strip()
    return value or default


def prompt_discard_files() -> list[str]:
    while True:
        if _questionary_available():
            result = _questionary_ask(
                lambda: questionary.text('Files to reload from remote (comma separated)'),
                'Interactive questionary input is unavailable, fallback to plain input',
            )
            if result is not None:
                value = result.strip()
            else:
                value = input('Files to reload from remote (comma separated): ').strip()
        else:
            value = input('Files to reload from remote (comma separated): ').strip()

        items = [item.strip() for item in value.split(',') if item.strip()]
        if items:
            return items
        console.print('Specify at least one file', style='yellow')


def prompt_initialization_source() -> str:
    return prompt_choice(
        'Which version is newer?',
        ['My local version', 'Remote server version'],
    )