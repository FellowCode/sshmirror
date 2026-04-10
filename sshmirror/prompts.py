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
_RU_TO_EN_KEYBOARD = str.maketrans({
    'й': 'q', 'ц': 'w', 'у': 'e', 'к': 'r', 'е': 't', 'н': 'y', 'г': 'u', 'ш': 'i', 'щ': 'o', 'з': 'p',
    'х': '[', 'ъ': ']', 'ф': 'a', 'ы': 's', 'в': 'd', 'а': 'f', 'п': 'g', 'р': 'h', 'о': 'j', 'л': 'k',
    'д': 'l', 'ж': ';', 'э': "'", 'я': 'z', 'ч': 'x', 'с': 'c', 'м': 'v', 'и': 'b', 'т': 'n', 'ь': 'm',
    'б': ',', 'ю': '.',
})
_EN_TO_RU_KEYBOARD = str.maketrans({value: key for key, value in _RU_TO_EN_KEYBOARD.items()})


def _questionary_available() -> bool:
    import sys

    return questionary is not None and sys.stdin.isatty() and sys.stdout.isatty()


def _questionary_ask(question_factory: typing.Callable[[], typing.Any], fallback_message: str) -> typing.Any:
    if not _questionary_available():
        return _PROMPT_FALLBACK

    try:
        question = question_factory()

        try:
            asyncio.get_running_loop()
            has_running_loop = True
        except RuntimeError:
            has_running_loop = False

        if has_running_loop:
            # questionary.ask() calls asyncio.run() internally, which fails
            # inside an existing event loop. Run in a separate thread instead.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(question.ask).result()
        else:
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


def _normalize_confirm_value(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized == '':
        return None

    variants = {
        normalized,
        normalized.translate(_RU_TO_EN_KEYBOARD),
        normalized.translate(_EN_TO_RU_KEYBOARD),
    }
    yes_values = {'yes', 'y', 'да', 'д', 'lf', 'l', 'нуу', 'н'}
    no_values = {'no', 'n', 'нет', 'не', 'тщ', 'т'}

    if any(item in yes_values for item in variants):
        return True
    if any(item in no_values for item in variants):
        return False
    return None


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
        value = _normalize_confirm_value(_read_plain_input(f'{prompt} (yes/no): '))
        if value is not None:
            return value
        console.print('Please answer yes or no', style='yellow')


def prompt_choice(prompt: str, choices: list[str], default: str | None = None, styled_choices: list | None = None, style: typing.Any = None) -> str:
    if styled_choices is not None:
        result = _questionary_ask(
            lambda: questionary.select(prompt, choices=styled_choices, default=default, style=style),
            'Interactive questionary prompt is unavailable, fallback to plain input',
        )
    else:
        result = _questionary_ask(
            lambda: questionary.select(prompt, choices=choices, default=default),
            'Interactive questionary prompt is unavailable, fallback to plain input',
        )
    if result is not _PROMPT_FALLBACK:
        return result

    return _fallback_choice_prompt(prompt, choices, default=default)


def prompt_confirm(prompt: str) -> bool:
    while True:
        result = _questionary_ask(
            lambda: questionary.text(f'{prompt} (yes/no)'),
            'Interactive questionary confirm is unavailable, fallback to plain input',
        )
        if result is _PROMPT_FALLBACK:
            return _fallback_confirm_prompt(prompt)

        normalized = _normalize_confirm_value(result)
        if normalized is not None:
            return normalized
        console.print('Please answer yes or no', style='yellow')


def prompt_text(prompt: str, default: str | None = 'update') -> str:
    while True:
        result = _questionary_ask(
            lambda: questionary.text(prompt, default=default or ''),
            'Interactive questionary input is unavailable, fallback to plain input',
        )
        if result is not _PROMPT_FALLBACK:
            value = result.strip()
        else:
            suffix = f' [{default}]' if default else ''
            value = _read_plain_input(f'{prompt}{suffix}: ').strip()

        if value != '':
            return value
        if default is not None:
            return default

        console.print('Value is required', style='yellow')


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