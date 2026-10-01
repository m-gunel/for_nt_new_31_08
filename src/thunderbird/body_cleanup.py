"""Очистка тела письма от цитат и подписи.

Живёт отдельным модулем без зависимостей на src.parsers: раньше функция
лежала в read_messages.py, и цепочка parsers → get_body → read_messages →
parsers образовывала циклический импорт, который не падал только из-за
порядка строк в parsers/__init__.py.
"""

import re

# Строка классической цитаты ("> ...").
_QUOTE_LINE = re.compile(r"^\s*>")

# Явное начало цитируемого блока: "On ... wrote:", разделитель пересланного
# сообщения, строка целиком из дефисов.
_QUOTE_INTRO = re.compile(
    r"^\s*("
    r"On .{0,200}wrote:\s*$"
    r"|-{2,}\s*(Original|Forwarded) Message\s*-{2,}"
    r"|-{5,}\s*$"
    r")",
    re.IGNORECASE,
)

# Строка заголовка переписки. Одна такая строка в живом тексте — не граница
# (в письме легко встречается «Тема: отпуск» как обычная фраза); границей
# считается блок из двух и более подряд.
_HEADER_LINE = re.compile(
    r"^\s*(From|Sent|To|Cc|Subject|Date"
    r"|От|Кому|Копия|Тема|Дата|Отправлено)\s*:\s",
    re.IGNORECASE,
)

# Начало подписи: стандартный разделитель "-- " или типовые формулы.
_SIGNATURE = re.compile(
    r"^\s*(--\s*$"
    r"|С уважением\b"
    r"|Best regards\b"
    r"|Kind regards\b"
    r"|With regards\b"
    r")",
    re.IGNORECASE,
)


def _find_quote_start(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if _QUOTE_LINE.match(line) or _QUOTE_INTRO.match(line):
            return i
        if (
            _HEADER_LINE.match(line)
            and i + 1 < len(lines)
            and _HEADER_LINE.match(lines[i + 1])
        ):
            return i
    return len(lines)


def extract_unique_body(email_body: str, skip_signature: bool = True) -> str:
    """Новый текст письма без цитат; при skip_signature — и без подписи.

    Прежняя реализация резала тело по первому совпадению любого из 15
    паттернов, включая «С уважением», «-------» и «To: » в начале строки —
    письмо с такой строкой в живом тексте обрывалось на ней, и в индекс
    попадал огрызок.
    """
    lines = email_body.splitlines()
    lines = lines[: _find_quote_start(lines)]
    if skip_signature:
        for i, line in enumerate(lines):
            if _SIGNATURE.match(line):
                lines = lines[:i]
                break
    return "\n".join(lines)
