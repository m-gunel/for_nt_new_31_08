import abc
import os
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Generator, Iterable
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
)

from src.common import Chunk, DocumentMetadata
from src.utils import ConfigMixin, wrap_iter_to_async


class Parser(abc.ABC, ConfigMixin):
    @property
    def _config(self):
        return self.cfg

    def get_metadata(self, path: str):
        # Get the creation and modification time
        creation_time = os.path.getctime(path)
        modification_time = os.path.getmtime(path)

        # Convert timestamps to readable date-time format
        creation_time_readable = datetime.fromtimestamp(creation_time)
        modification_time_readable = datetime.fromtimestamp(modification_time)
        return DocumentMetadata(
            date_created=creation_time_readable,
            date_modified=modification_time_readable,
            original_name=Path(path).name,
            ext=Path(path).suffix[1:],
        )

    def iter_chunks(self, path: str) -> Iterable[Chunk]:
        with open(path, "rb") as f:
            yield from self.iter_chunks_io(f)

    async def async_iter_chunks(self, path: str) -> AsyncIterable[Chunk]:
        with open(path, "rb") as f:
            async for chunk in self.async_iter_chunks_io(f):
                yield chunk

    def iter_chunks_io(self, file: BinaryIO) -> Generator[Chunk, Any, None]:
        """Парсер должен возвращать итератор чанков документа"""
        raise NotImplementedError

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
