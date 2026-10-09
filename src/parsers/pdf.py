import io
import re
from typing import BinaryIO

import pymupdf

from .base import Parser, SkipFile, parser
from .doc_model import Block, ParsedDoc
from .metadata import pdf_properties
from .ocr import needs_ocr, recognize

# Частная область F0xx: Symbol кладёт туда буквы и знаки (их переводит
# normalize.py), а шрифты-пиктограммы — маркеры списков и галочки.
_PUA = re.compile("[\uf020-\uf0ff]")
_DINGBAT_FONTS = ("wingding", "webding", "dingbat")


def _dingbats_to_bullets(page: pymupdf.Page, text: str) -> str:
    """Знаки F0xx из шрифтов-пиктограмм → «•»: по таблице Symbol они стали бы буквами."""
    if not _PUA.search(text):
        return text
    marks: set[str] = set()
    other: set[str] = set()
    try:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", ()):
                for span in line["spans"]:
                    font = span["font"].lower()
                    dingbat = any(name in font for name in _DINGBAT_FONTS)
                    (marks if dingbat else other).update(_PUA.findall(span["text"]))
    except Exception:  # шрифты страницы не прочитались — знаки по таблице Symbol
        return text
    # Код, встреченный и в другом шрифте страницы, оставляем таблице Symbol.
    if not (marks := marks - other):
        return text
    return text.translate({ord(ch): "•" for ch in marks})


@parser("pdf")
class PdfParser(Parser):
    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        """Страница — блок: чанк знает свои страницы. Порядок чтения — как у get_text().

        Страница-картинка (скан) получает текст OCR — блок вида `ocr` (src/parsers/ocr.py).
        """
        with (
            pymupdf.open(stream=file)
            if not isinstance(file, io.BufferedReader)
            else pymupdf.open(file) as doc
        ):
            if doc.needs_pass:
                raise SkipFile("PDF зашифрован паролем")
            texts = [_dingbats_to_bullets(p, p.get_text()) for p in doc]  # type: ignore
            scans = [n for n, t in enumerate(texts, 1) if needs_ocr(doc[n - 1], t)]
            ocr = recognize(file, scans) if scans else {}
            blocks = [
                Block(ocr[n], kind="ocr", page=n) if n in ocr else Block(t, page=n)
                for n, t in enumerate(texts, 1)
            ]
            if blocks and not any(b.text.strip() for b in blocks):
                # Сканы есть, а текста нет (OCR выключен, без tessdata, не уложился) —
                # следующий проход повторит. Без сканов распознавать нечего: повтор
                # дал бы то же самое — пропуск до изменения файла.
                warnings = pymupdf.TOOLS.mupdf_warnings()
                raise SkipFile(
                    f"в PDF нет текстового слоя ({doc.page_count} стр., из них "
                    f"сканов {len(scans)}, распознано OCR {len(ocr)}; предупреждения "
                    f"MuPDF: {warnings[-300:] or 'нет'})",
                    permanent=not scans,
                )
            meta, titles = pdf_properties(doc)
            try:
                toc = [(level, title, page) for level, title, page, *_ in doc.get_toc()]
            except Exception:  # битое оглавление — без закладок
                toc = []
        return ParsedDoc(blocks, meta, titles, pdf=True, toc=toc)
