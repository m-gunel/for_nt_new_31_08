import io
import logging
from collections.abc import Generator
from typing import Any, BinaryIO

# import pymupdf4llm
import pymupdf

from .base import Chunk, Parser, parser

logging.getLogger("pypdf._reader").setLevel(logging.ERROR)


@parser("pdf")
class PdfParser(Parser):
    def iter_chunks_io(self, file: BinaryIO) -> Generator[Chunk, Any, None]:
        # reader = PdfReader(file)
        # txt = md.convert_stream(file).text_content
        # txt = pymupdf4llm.to_markdown(pymupdf.open(file))
        with (
            # TODO: зменить на ruff-реализацию
            pymupdf.open(stream=file)
            if not isinstance(file, io.BufferedReader)
            else pymupdf.open(file) as doc
        ):
            txt = "\n".join(page.get_text() for page in doc)  # type: ignore
        idx = 0
        for split in self._config.embed_splitter.split_text(txt):
            yield Chunk(idx=idx, text=split)
            idx += 1
