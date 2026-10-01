from typing import BinaryIO
from zipfile import BadZipFile

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn

from ._tables import Budget, split_parts, table_blocks
from .base import Parser, SkipFile, parser
from .doc_model import Block, ParsedDoc
from .docx_ooxml import letters, roman
from .metadata import ooxml_properties

BUDGET_TOKENS = 450  # чанк таблицы; «Документ: <имя>» сверху уложит его в 500


def _shapes(shapes):
    """Фигуры слайда, группы раскрыты."""
    for sh in shapes:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _shapes(sh.shapes)
        else:
            yield sh


def _autonum(n: int, kind: str) -> str:
    """Номер автонумерации PowerPoint: arabicPeriod → «3.», alphaLcParenR → «c)»."""
    if kind.startswith("alphaLc"):
        num = letters(n)
    elif kind.startswith("alphaUc"):
        num = letters(n).upper()
    elif kind.startswith("romanLc"):
        num = roman(n)
    elif kind.startswith("romanUc"):
        num = roman(n).upper()
    else:
        num = str(n)
    if kind.endswith("ParenBoth"):
        return f"({num})"
    if kind.endswith("ParenR"):
        return f"{num})"
    return num if kind.endswith("Plain") else f"{num}."


def _frame_text(frame) -> str:
    """Текст рамки по абзацам; номера автонумерации восстанавливаются."""
    lines, counters = [], {}
    for p in frame.paragraphs:
        text = p.text
        ppr = p._p.pPr
        auto = ppr.find(qn("a:buAutoNum")) if ppr is not None else None
        level = p.level
        for k in [k for k in counters if k > level]:
            del counters[k]
        if auto is not None and text.strip():
            n = counters.get(level, int(auto.get("startAt", "1")) - 1) + 1
            counters[level] = n
            text = f"{_autonum(n, auto.get('type', 'arabicPeriod'))} {text}"
        elif text.strip():
            counters.pop(level, None)
        lines.append(text)
    return "\n".join(x for x in lines if x.strip())


@parser("pptx")
class PptxParser(Parser):
    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        """Слайд — страница: заголовок слайда — раздел, текст фигур (группы раскрыты,
        автонумерация восстановлена), таблицы — сериями с шапкой, заметки — отдельно."""
        meta, titles = ooxml_properties(file, "pptx")
        try:
            prs = Presentation(file)
        except (BadZipFile, KeyError) as exc:
            raise SkipFile(f"pptx повреждён ({type(exc).__name__}: {exc})") from exc
        budget = Budget(BUDGET_TOKENS, self._config.ov_model.count_tokens)
        blocks: list[Block] = []
        for n, slide in enumerate(prs.slides, 1):
            head = slide.shapes.title
            title = (
                _frame_text(head.text_frame)
                if head is not None and head.has_text_frame
                else ""
            )
            if title:
                blocks.append(Block(title, page=n, heading=1))
                if n == 1:
                    titles.append(title)
            tables = []
            first_text = len(blocks)
            for sh in _shapes(slide.shapes):
                if head is not None and sh.shape_id == head.shape_id:
                    continue
                if getattr(sh, "has_table", False) and sh.has_table:
                    tables.append(sh.table)
                elif getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                    if text := _frame_text(sh.text_frame):
                        blocks.append(Block(text, page=n))
            # Нет фигуры-заголовка: короткая первая строка слайда и есть его заголовок.
            if not title and len(blocks) > first_text:
                lead = blocks[first_text]
                if "\n" not in lead.text and len(lead.text) <= 100:
                    lead.heading, title = 1, lead.text
            for k, table in enumerate(tables, 1):
                rows = [
                    [" ".join(c.text.split()) for c in row.cells] for row in table.rows
                ]
                header, body = (rows[0], rows[1:]) if len(rows) > 1 else ([], rows)
                first = 2 if header else 1
                blocks += table_blocks(
                    header,
                    [(r, i) for i, r in enumerate(body, first)],
                    budget,
                    title=title or None,
                    table_id=f"s{n}t{k}",
                    page=n,
                )
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame
                if notes is not None and (text := _frame_text(notes)):
                    # Отдельным чанком с заголовком слайда: у фрагмента свой вид.
                    prefix = f"{title}\n" if title else ""
                    for part in split_parts(prefix, [f"Заметки: {text}"], budget):
                        blocks.append(Block(part, kind="notes", page=n, whole=True))
        return ParsedDoc(blocks, meta, titles)
