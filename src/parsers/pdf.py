import io
import logging
from typing import BinaryIO

import pymupdf

from .base import Parser, parser
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
            blocks = [Block(page.get_text(), page=n) for n, page in enumerate(doc, 1)]  # type: ignore
            meta, titles = pdf_properties(doc)
            try:
                toc = [(level, title, page) for level, title, page, *_ in doc.get_toc()]
            except Exception:  # битое оглавление — без закладок
                toc = []
        return ParsedDoc(blocks, meta, titles, pdf=True, toc=toc)
