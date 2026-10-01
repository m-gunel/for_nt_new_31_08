from typing import BinaryIO

from .base import Parser, parser
from .doc_model import Block, ParsedDoc


@parser("txt")
class TxtParser(Parser):
    """Парсер для обычных текстовых файлов (.txt)."""

    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        """Строка — блок: чанк знает свои строки в исходном файле."""
        text = file.read().decode("utf-8", errors="replace")
        # Только \n и \r: splitlines() режет ещё по \x0b, \x0c, \x1c–\x1e, U+2028 —
        # и номера строк разошлись бы с редактором.
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        blocks = [Block(line, line=n) for n, line in enumerate(lines, 1)]
        return ParsedDoc(blocks, {"encoding": "utf-8"})
