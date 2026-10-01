from collections.abc import Generator
from typing import Any, BinaryIO

from .base import Chunk, Parser, parser


@parser("txt")
class TxtParser(Parser):
    """Парсер для обычных текстовых файлов (.txt)."""

    def iter_chunks_io(self, file: BinaryIO) -> Generator[Chunk, Any, None]:
        idx = 0
        text = file.read().decode("utf-8", errors="replace")
        for split in self._config.embed_splitter.split_text(text):
            yield Chunk(idx=idx, text=split)
            idx += 1
