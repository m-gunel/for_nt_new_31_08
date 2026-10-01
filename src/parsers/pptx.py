from typing import BinaryIO
from zipfile import BadZipFile

from pptx import Presentation

from src.utils import get_logger

from .base import Parser, parser
from .doc_model import Block, ParsedDoc
from .metadata import ooxml_properties

logger = get_logger(__name__)


@parser("pptx")
class PptxParser(Parser):
    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        """Слайд — блок: чанк знает свои слайды. Заметки, таблицы и группы — шаг 4."""
        meta, titles = ooxml_properties(file, "pptx")
        try:
            prs = Presentation(file)
        except BadZipFile:
            logger.warning(
                "Failed to read PPTX file, it may not be a valid PPTX format."
            )
            return ParsedDoc([])
        blocks = []
        for n, slide in enumerate(prs.slides, 1):
            texts = [
                txt for shape in slide.shapes if (txt := getattr(shape, "text", None))
            ]
            if texts:
                blocks.append(Block("\n".join(texts), page=n))
            if n == 1 and slide.shapes.title is not None:
                titles.append(slide.shapes.title.text)
        return ParsedDoc(blocks, meta, titles)
