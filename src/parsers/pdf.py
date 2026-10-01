import io
import logging
from typing import BinaryIO

import pymupdf

from .base import Parser, SkipFile, parser
from .doc_model import Block, ParsedDoc
from .metadata import pdf_properties

logging.getLogger("pypdf._reader").setLevel(logging.ERROR)


@parser("pdf")
class PdfParser(Parser):
    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        """Страница — блок: чанк знает свои страницы. Порядок чтения — как у get_text()."""
        with (
            pymupdf.open(stream=file)
            if not isinstance(file, io.BufferedReader)
            else pymupdf.open(file) as doc
        ):
            if doc.needs_pass:
                raise SkipFile("PDF зашифрован паролем")
            blocks = [Block(page.get_text(), page=n) for n, page in enumerate(doc, 1)]  # type: ignore
            if blocks and not any(b.text.strip() for b in blocks):
                # Скан без OCR или сбой извлечения: не помечать готовым навсегда —
                # следующий проход повторит; причину — в журнал.
                warnings = pymupdf.TOOLS.mupdf_warnings()
                raise SkipFile(
                    f"в PDF нет текстового слоя ({doc.page_count} стр.; предупреждения "
                    f"MuPDF: {warnings[-300:] or 'нет'})",
                    permanent=False,
                )
            meta, titles = pdf_properties(doc)
            try:
                toc = [(level, title, page) for level, title, page, *_ in doc.get_toc()]
            except Exception:  # битое оглавление — без закладок
                toc = []
        return ParsedDoc(blocks, meta, titles, pdf=True, toc=toc)
