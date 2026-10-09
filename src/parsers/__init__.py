import zipfile
import zlib
from collections.abc import AsyncIterable, Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, Protocol
from xml.etree import ElementTree

import pymupdf
from lxml import etree

from src.common import (
    CARD_IDX,
    CARD_KIND,
    Chunk,
    ChunkWithMeta,
    DocumentMetadata,
    FileMetadata,
    doc_prefix,
    document_card_text,
)
from src.utils import get_config, get_logger, wrap_iter_to_async

from . import ocr
from .base import Parser, SkipFile, file_extension, get_parser, get_parsers_registry
from .doc_model import ParsedDoc, chunk_document, full_text, prepare
from .docx import DocParser, DocxParser
from .metadata import clean_title, content_metadata, first_line_title, language
from .pdf import PdfParser
from .pptx import PptxParser
from .txt import TxtParser
from .xlsx import XlsxParser

__all__ = [
    "DocParser",
    "DocxParser",
    "PdfParser",
    "PptxParser",
    "TxtParser",
    "XlsxParser",
    "document_chunks",
    "file_chunks",
    "file_extension",
    "get_parsers_registry",
    "parse_files",
    "read_document_text",
]

lg = get_logger(__name__)

# Порча содержимого: разбор не удастся, пока файл не изменится. Временные причины
# (OSError — файл занят или исчез, MemoryError) сюда не входят. zlib.error,
# EOFError и NotImplementedError — порча сжатых данных внутри zip (docx/xlsx/pptx)
# при целом центральном каталоге: оборванный поток, неизвестный метод сжатия.
_CORRUPT = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    NotImplementedError,
    ElementTree.ParseError,
    etree.XMLSyntaxError,
    pymupdf.FileDataError,
)


# Поля места чанка (src/common/doc_meta.py: Chunk) — у карточки все пустые.
_PLACE_FIELDS = tuple(
    k for k in Chunk.__annotations__ if k not in ("idx", "text", "kind")
)


def _document_meta(doc: ParsedDoc, base: DocumentMetadata) -> DocumentMetadata:
    """Метаданные документа: поля ФС + свойства файла + производные от текста."""
    meta: dict = dict(base)
    for k, v in doc.meta.items():
        if v is not None and meta.get(k) is None:
            meta[k] = v
    text = full_text(doc)
    head = text[:5000]
    title = next((t for t in (clean_title(c, head) for c in doc.titles) if t), None)
    if title is None and doc.title_from_text:
        title = first_line_title(text)
    meta["title"] = title
    meta["language"] = language(text)
    return DocumentMetadata(**meta)


def document_chunks(
    path: Path,
    parser: Parser,
    *,
    io: BinaryIO | None = None,
    doc_meta: DocumentMetadata | None = None,
    file_meta: FileMetadata | None = None,
    ocr_generation: int | None = None,
) -> Iterator[ChunkWithMeta]:
    """Конвейер разбора одного документа — общий для индексации и стенда.

    Разбор → нормализация → метаданные → нарезка с полями места → фильтр пустого
    и повторов → «Документ: <имя>» сверху каждого чанка → карточка документа
    последней строкой (src/common/doc_meta.py: document_card_text). Синхронный:
    в индексации идёт в потоке через wrap_iter_to_async.
    """
    if io is None:
        with open(path, "rb") as f:
            yield from document_chunks(
                path,
                parser,
                io=f,
                doc_meta=doc_meta or parser.get_metadata(str(path)),
                file_meta=file_meta,
                ocr_generation=ocr_generation,
            )
        return
    base = dict(doc_meta or {})
    ext = base.get("ext") or file_extension(path)
    base.update(content_metadata(io, ext))
    try:
        # OCR — только внутри parse_io; метка поколения — без yield внутри,
        # чтобы не пережить генератор в чужом потоке.
        with ocr.started_in(ocr_generation):
            parsed = parser.parse_io(io)
    except _CORRUPT as exc:
        raise SkipFile(f"содержимое повреждено ({type(exc).__name__}: {exc})") from exc
    doc = prepare(parsed)
    meta = _document_meta(doc, DocumentMetadata(**base))
    prefix = doc_prefix(meta.get("original_name"))
    emitted = False
    for chunk in chunk_document(doc, get_config().embed_splitter):
        chunk["text"] = prefix + chunk["text"]
        yield _row(chunk, meta, path, file_meta)
        emitted = True
    # Карточка — только при наличии чанков (иначе она находила бы пустой документ)
    # и последней: индексатор отмечает файл готовым по чанку следующего файла.
    # Поля места явно None — читатели строк берут их по ключу, как у чанков.
    if emitted and (card := document_card_text(meta)):
        place = dict.fromkeys(_PLACE_FIELDS)
        yield _row(
            {**place, "idx": CARD_IDX, "text": card, "kind": CARD_KIND},
            meta,
            path,
            file_meta,
        )


def _row(
    chunk: dict, meta: DocumentMetadata, path: Path, file_meta: FileMetadata | None
) -> ChunkWithMeta:
    row = ChunkWithMeta(**chunk, meta=meta, path=str(path))
    if file_meta is not None:
        row["file_meta"] = file_meta
    return row


def file_chunks(path: str | Path) -> Iterator[ChunkWithMeta]:
    """Чанки файла ровно как их пишет индексатор (стенд замера сверяет по ним)."""
    p = Path(path)
    if parser := get_parser(file_extension(p)):
        yield from document_chunks(p, parser)


def read_document_text(path: str | Path) -> str | None:
    """Полный текст документа после нормализации — без нарезки и перекрытий.

    None — формат не индексируется.
    """
    p = Path(path)
    if (parser := get_parser(file_extension(p))) is None:
        return None
    # OCR — только из кэша индексации: ждать распознавания скана здесь нельзя.
    with open(p, "rb") as f, ocr.cache_only():
        return full_text(prepare(parser.parse_io(f)))


class FailCallback(Protocol):
    """Обратный вызов «файл не дал чанков».

    `permanent=True` — повторять разбор незачем, пока файл не изменится: текста
    нет, содержимое повреждено или зашифровано (SkipFile). Файл можно пометить
    обработанным. `permanent=False` — сбой временный или непредвиденный, в том
    числе SkipFile(permanent=False): помечать нельзя, иначе файл выпадет из
    индексации навсегда.
    """

    async def __call__(self, meta: "FileMetadata", *, permanent: bool) -> None: ...


def parse_files(files: Iterable[Path]):
    for p in files:
        if parser := get_parser(file_extension(p)):
            try:
                yield from document_chunks(Path(p), parser)
            except Exception:
                lg.exception("processing %s", p)
        else:
            lg.debug("no parser found for %s", p)


def check_has_parser(p: Path) -> bool:
    return get_parser(file_extension(p)) is not None


async def async_parse_files(
    files: AsyncIterable["FileMetadata"],
    fail_cb: FailCallback,
):
    """Разобрать поток метаданных файлов в чанки."""
    async for meta in files:
        try:
            async for chunk in _process_path(meta, fail_cb=fail_cb):
                yield chunk
        except GeneratorExit:
            return


async def _process_path(meta: FileMetadata, fail_cb: FailCallback):
    path = Path(meta.path)
    shown = str(path)
    if parser := get_parser(file_extension(path)):
        chunks = 0
        try:
            lg.debug("begin procesing %s", shown)
            # Метаданные ФС — внутри try: файл, исчезнувший между сканом и
            # разбором, — временный сбой этого файла, а не падение всего прохода.
            async for chunk in wrap_iter_to_async(
                document_chunks(
                    path,
                    parser,
                    file_meta=meta,
                    # Здесь, в event loop, — до любой остановки индексации.
                    ocr_generation=ocr.generation(),
                )
            ):
                yield chunk
                chunks += 1
        except SkipFile as skip:
            # Причина в содержимом — без стека. permanent действует до изменения
            # файла: новые размер и mtime вернут его в индексацию.
            lg.warning("файл %s не индексируется: %s", shown, skip.reason)
            await fail_cb(meta, permanent=skip.permanent)
            return
        except (OSError, MemoryError) as exc:
            lg.warning(
                "файл %s пока не прочитан (%s: %s) — повтор на следующем проходе",
                shown,
                type(exc).__name__,
                exc,
            )
            await fail_cb(meta, permanent=False)
            return
        except Exception:
            # Непредвиденный сбой — со стеком; не помечаем, следующий проход повторит.
            lg.exception("processing %s", shown)
            await fail_cb(meta, permanent=False)
            return
        if chunks == 0:
            # Разбор прошёл, текста нет — повторять незачем.
            lg.warning("no chunks generated for %s", shown)
            await fail_cb(meta, permanent=True)
    else:
        lg.debug("no parser found for %s", shown)
