"""Модель документа: блоки после разбора → полный текст → чанки с привязкой к месту.

Парсер формата отдаёт `ParsedDoc`: блоки (страница PDF, слайд, строка txt, заголовок,
готовая карточка строки таблицы) и поля метаданных из самого файла. Дальше для всех
форматов одинаково: нормализация, полный текст, нарезка тем же сплиттером, привязка
каждого чанка к страницам, строкам и разделу по его позиции в тексте, фильтр пустого
и повторов. Блоки `whole` — готовые чанки: их не режут и не склеивают с соседями.

Полный текст: блоки через перевод строки, а на границе страницы, слайда, готового
чанка и перед заголовком — через пустую строку. Это граница абзаца, по которой сплиттер режет в первую
очередь (как раньше: страница PDF кончалась переводом строки); без неё он перебирал
бы строки всего документа — вдвое больше вызовов токенизатора.
"""

import bisect
import re
from dataclasses import dataclass, field
from typing import Any

from .normalize import normalize_field, normalize_text

SECTION_SEP = " › "
# Чанк, где меньше трёх слов из букв («—», «1», «Стр. 2»), — не содержание.
_WORD = re.compile(r"[^\W\d_]{2,}")
# Страница из одного номера («12», «- 12 -», «Стр. 3») — не содержание.
_PAGE_NUMBER = re.compile(r"^\W*(стр\.?|с\.|page)?\s*\d{1,4}\W*$", re.I)


@dataclass(slots=True)
class Block:
    text: str
    kind: str = "text"  # text / table / table_row / notes / ocr
    page: int | None = None  # страница PDF, слайд, лист — с 1
    line: int | None = None  # строка исходного txt — с 1
    heading: int | None = None  # уровень заголовка, если блок — заголовок раздела
    section: str | None = None  # готовый путь раздела (лист › таблица)
    table_id: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    whole: bool = False  # готовый чанк: не резать, не склеивать с соседями


@dataclass
class ParsedDoc:
    blocks: list[Block]
    # Поля из самого файла: author, last_modified_by, subject, keywords, description,
    # category, doc_created, doc_modified, page_count, encoding.
    meta: dict[str, Any] = field(default_factory=dict)
    # Кандидаты в название по убыванию надёжности (свойства файла, закладка,
    # заголовок первого слайда); первая содержательная строка — последний запасной.
    titles: list[str] = field(default_factory=list)
    title_from_text: bool = True
    # PDF: управляющий символ между буквами — дефис; переносы и заголовки
    # чистит src/parsers/pdf_clean.py.
    pdf: bool = False
    toc: list[tuple[int, str, int]] = field(
        default_factory=list
    )  # закладки: уровень, название, страница


def prepare(doc: ParsedDoc) -> ParsedDoc:
    """Нормализовать блоки и поля; убрать пустые блоки и страницы из одного номера."""
    for b in doc.blocks:
        b.text = normalize_text(b.text, pdf=doc.pdf)
    source = doc.blocks
    if doc.pdf and source:
        from .pdf_clean import clean_pages  # pdf_clean сам импортирует Block отсюда

        source = clean_pages(source, doc.toc)
    blocks: list[Block] = []
    for b in source:
        if not b.text:
            # Пустая строка txt — граница абзаца для сплиттера; подряд идущие — одна.
            if b.line is not None and blocks and blocks[-1].text:
                blocks.append(b)
            continue
        if b.page is not None and not b.whole and _PAGE_NUMBER.match(b.text):
            continue
        b.section = normalize_field(b.section)
        blocks.append(b)
    while blocks and not blocks[-1].text:
        blocks.pop()
    doc.blocks = blocks
    for k, v in doc.meta.items():
        if isinstance(v, str):
            doc.meta[k] = normalize_field(v)
    doc.titles = [t for t in (normalize_field(t) for t in doc.titles) if t]
    return doc


def _layout(blocks: list[Block]) -> tuple[str, list[int]]:
    """Полный текст и позиция начала каждого блока в нём."""
    parts: list[str] = []
    starts: list[int] = []
    pos = 0
    for i, b in enumerate(blocks):
        if i:
            prev = blocks[i - 1]
            boundary = prev.whole or b.whole or prev.page != b.page or b.heading
            sep = "\n\n" if boundary else "\n"
            parts.append(sep)
            pos += len(sep)
        starts.append(pos)
        parts.append(b.text)
        pos += len(b.text)
    return "".join(parts), starts


def full_text(doc: ParsedDoc) -> str:
    return _layout(doc.blocks)[0]


def _sections(blocks: list[Block]) -> list[str | None]:
    """Путь раздела для каждого блока: заголовки выше по тексту, вложенные по уровню."""
    stack: list[str] = []
    out = []
    for b in blocks:
        if b.heading:
            stack = stack[: b.heading - 1] + [b.text.replace("\n", " ")]
        out.append(b.section or (SECTION_SEP.join(stack) if stack else None))
    return out


def chunk_document(doc: ParsedDoc, splitter) -> list[dict]:
    """Чанки документа с полями места: страницы, строки, раздел, вид, таблица, позиция."""
    blocks = doc.blocks
    if not blocks:
        return []
    text, starts = _layout(blocks)
    sections = _sections(blocks)
    raw: list[dict] = []

    def emit(chunk_text: str, start: int, end: int, first: int, last: int) -> None:
        span = blocks[first : last + 1]
        pages = [b.page for b in span if b.page is not None]
        lines = [b.line for b in span if b.line is not None]
        head = blocks[first]
        c: dict[str, Any] = {
            "text": chunk_text,
            "kind": head.kind,
            "char_start": start,
            "char_end": end,
        }
        if pages:
            c["page_start"], c["page_end"] = pages[0], pages[-1]
        if lines:
            c["line_start"], c["line_end"] = lines[0], lines[-1]
        if sections[first]:
            c["section"] = sections[first]
        if head.table_id:
            c["table_id"] = head.table_id
        if head.row_start is not None:
            c["row_start"] = head.row_start
            c["row_end"] = (
                blocks[last].row_end
                if blocks[last].row_end is not None
                else head.row_end
            )
        raw.append(c)

    def block_at(p: int) -> int:
        return bisect.bisect_right(starts, p) - 1

    i = 0
    while i < len(blocks):
        if blocks[i].whole:
            emit(blocks[i].text, starts[i], starts[i] + len(blocks[i].text), i, i)
            i += 1
            continue
        j = i
        while j + 1 < len(blocks) and not blocks[j + 1].whole:
            j += 1
        if j + 1 < len(blocks) and all(b.heading for b in blocks[i : j + 1]):
            # Одни заголовки перед таблицей: они уже в её названии и пути раздела.
            i = j + 1
            continue
        flow_start, flow_end = starts[i], starts[j] + len(blocks[j].text)
        flow = text[flow_start:flow_end]
        prev = -1
        for piece in splitter.split_text(flow):
            # Сплиттер отдаёт куски текста по порядку (с перекрытием) — ищем каждый
            # правее начала предыдущего.
            at = flow.find(piece, prev + 1)
            if at < 0:
                at = flow.find(piece)
            if (
                at < 0
            ):  # сплиттер переписал текст — место неизвестно, берём начало потока
                at = max(prev, 0)
            prev = at
            start, end = flow_start + at, flow_start + at + len(piece)
            emit(
                piece,
                start,
                end,
                max(block_at(start), i),
                min(block_at(max(start, end - 1)), j),
            )
        i = j + 1
    return _filter(raw)


def _filter(chunks: list[dict]) -> list[dict]:
    """Убрать чанки без содержания и точные повторы внутри документа; перенумеровать."""
    meaningful = [c for c in chunks if len(_WORD.findall(c["text"])) >= 3]
    # Документ, где весь текст — короткие куски, оставляем как есть: иначе он пропал бы.
    kept = meaningful if meaningful else chunks
    out, seen = [], set()
    for c in kept:
        key = " ".join(c["text"].lower().replace("ё", "е").split())
        if key in seen:
            continue
        seen.add(key)
        c["idx"] = len(out)
        out.append(c)
    return out
