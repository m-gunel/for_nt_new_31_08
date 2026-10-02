import io
import logging
from typing import BinaryIO

import pymupdf

from .base import Parser, SkipFile, parser
from .doc_model import Block, ParsedDoc
from .metadata import pdf_properties
from .ocr import needs_ocr, recognize

logging.getLogger("pypdf._reader").setLevel(logging.ERROR)


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
            texts = [page.get_text() for page in doc]  # type: ignore
            scans = [n for n, t in enumerate(texts, 1) if needs_ocr(doc[n - 1], t)]
            ocr = recognize(file, scans) if scans else {}
            blocks = [
                Block(ocr[n], kind="ocr", page=n) if n in ocr else Block(t, page=n)
                for n, t in enumerate(texts, 1)
            ]
            if blocks and not any(b.text.strip() for b in blocks):
                # Скан без OCR или сбой извлечения: не помечать готовым навсегда —
                # следующий проход повторит; причину — в журнал.
                warnings = pymupdf.TOOLS.mupdf_warnings()
                raise SkipFile(
                    f"в PDF нет текстового слоя ({doc.page_count} стр., из них "
                    f"сканов {len(scans)}, распознано OCR {len(ocr)}; предупреждения "
                    f"MuPDF: {warnings[-300:] or 'нет'})",
                    permanent=False,
                )
            meta, titles = pdf_properties(doc)
            try:
                toc = [(level, title, page) for level, title, page, *_ in doc.get_toc()]
            except Exception:  # битое оглавление — без закладок
                toc = []
        return ParsedDoc(blocks, meta, titles, pdf=True, toc=toc)
