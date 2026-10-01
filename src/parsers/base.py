import abc
import os
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Generator, Iterable
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
)

from src.common import Chunk, DocumentMetadata
from src.utils import ConfigMixin, ExpectedError, wrap_iter_to_async

from .doc_model import ParsedDoc, chunk_document, prepare
from .metadata import file_metadata


class SkipFile(ExpectedError):
    """Файл не индексируется по причине в его содержимом: битый, зашифрован, без текста.

    permanent=True — повторять разбор незачем, пока файл не изменится; False —
    повторить на следующем проходе. В журнал — одна строка WARNING с причиной.
    """

    def __init__(self, reason: str, *, permanent: bool = True):
        super().__init__(reason)
        self.reason, self.permanent = reason, permanent


class Parser(abc.ABC, ConfigMixin):
    @property
    def _config(self):
        return self.cfg

    def get_metadata(self, path: str) -> DocumentMetadata:
        """Поля файловой системы; свойства из содержимого добавляет конвейер разбора."""
        return DocumentMetadata(**file_metadata(path))

    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        """Разобрать файл в блоки и свойства из самого файла (src/parsers/doc_model.py)."""
        raise NotImplementedError

    def iter_chunks(self, path: str) -> Iterable[Chunk]:
        with open(path, "rb") as f:
            yield from self.iter_chunks_io(f)

    async def async_iter_chunks(self, path: str) -> AsyncIterable[Chunk]:
        with open(path, "rb") as f:
            async for chunk in self.async_iter_chunks_io(f):
                yield chunk

    def iter_chunks_io(self, file: BinaryIO) -> Generator[Chunk, Any, None]:
        """Чанки документа с полями места — без префикса «Документ: …» и метаданных:
        их добавляет конвейер индексации, которому известно имя файла."""
        yield from chunk_document(
            prepare(self.parse_io(file)), self._config.embed_splitter
        )

    async def async_iter_chunks_io(self, file: BinaryIO) -> AsyncGenerator[Chunk, Any]:
        # Без лога на каждый чанк: при 10 000 файлов это сотни тысяч записей
        # за проход, а полезной информации в них нет.
        async for chunk in wrap_iter_to_async(self.iter_chunks_io(file)):
            yield chunk


def file_extension(name: str | os.PathLike) -> str:
    """Расширение файла в нижнем регистре, без точки.

    Файл без расширения даёт "" и просто не находит парсер.
    """
    return Path(name).suffix[1:].lower()


_registry: dict[str, type[Parser]] = {}
_registry_serialized: dict[str, Parser] = {}


def parser(*extentions) -> Callable[[type[Parser]], type[Parser]]:
    def wrapper(cls: type[Parser]) -> type[Parser]:
        for ext in extentions:
            _registry[ext] = cls

        return cls

    return wrapper


def get_parser(ext: str):
    if parser := _registry_serialized.get(ext):
        return parser
    else:
        parser_cls = _registry.get(ext)
        if parser_cls:
            _registry_serialized[ext] = parser = parser_cls()
            return parser


def get_parsers_registry() -> dict[str, type[Parser]]:
    return _registry


# .doc - Microsoft Word document (legacy)
# .docx - Microsoft Word Open XML document
# .pdf - Portable Document Format (Adobe PDF)
# .txt - Plain text file
# .rtf - Rich Text Format
# .odt - OpenDocument Text Document
# .html/.htm - HyperText Markup Language
# .xls - Microsoft Excel spreadsheet (legacy)
# .xlsx - Microsoft Excel Open XML spreadsheet
# .csv - Comma-separated values file
# .ods - OpenDocument Spreadsheet
# .ppt - Microsoft PowerPoint presentation (legacy)
# .pptx - Microsoft PowerPoint Open XML presentation
# .odp - OpenDocument Presentation
