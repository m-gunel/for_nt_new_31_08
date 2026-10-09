"""Очистка тела письма от цитат и подписи.

Без зависимостей на src.parsers: get_body импортируется из parsers, и
зависимость отсюда замкнула бы циклический импорт.
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

    Отбрасывается всё с первой границы цитаты, а при skip_signature — и с
    первой строки подписи. Одиночная строка вида «Тема: …» границей не
    считается. Строка из пяти и более дефисов и строка, которая начинается с
    формулы подписи («С уважением», «Best regards» и т. п.), — граница в любом
    месте письма, поэтому разделитель текстовой таблицы или абзац «С уважением
    к …» тоже обрывают текст.
    """
    lines = email_body.splitlines()
    lines = lines[: _find_quote_start(lines)]
    if skip_signature:
        for i, line in enumerate(lines):
            if _SIGNATURE.match(line):
                lines = lines[:i]
                break
    return "\n".join(lines)
