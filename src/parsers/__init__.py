import asyncio
import concurrent
import concurrent.futures
import email
import io
import zipfile
from collections.abc import AsyncIterable, Iterable, Iterator
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Protocol
from xml.etree import ElementTree

import aiofiles
import pymupdf
from aiofiles.threadpool.binary import AsyncBufferedReader
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
    is_email_path,
)
from src.thunderbird.get_body import Attachment, Body, process_message
from src.thunderbird.lazy_iter_mbox import _decode_filename
from src.utils import get_config, get_logger, wrap_iter_to_async

from . import ocr
from .base import Parser, SkipFile, file_extension, get_parser, get_parsers_registry
from .doc_model import ParsedDoc, chunk_document, full_text, prepare
from .docx import DocParser, DocxParser
from .metadata import clean_title, content_metadata, first_line_title, language
from .normalize import normalize_field
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
    "parse_files_io",
    "read_document_text",
]

lg = get_logger(__name__)

# Порча содержимого: разбор не удастся, пока файл не изменится. Временные причины
# (OSError — файл занят или исчез, MemoryError) сюда не входят.
_CORRUPT = (
    zipfile.BadZipFile,
    ElementTree.ParseError,
    etree.XMLSyntaxError,
    pymupdf.FileDataError,
)


# Поля места чанка (src/common/doc_meta.py: Chunk) — у карточки все пустые.
_PLACE_FIELDS = tuple(
    k for k in Chunk.__annotations__ if k not in ("idx", "text", "kind")
)


def _document_meta(doc: ParsedDoc, base: DocumentMetadata) -> DocumentMetadata:
    """Метаданные документа: поля ФС/письма + свойства файла + производные от текста."""
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
) -> Iterator[ChunkWithMeta]:
    """Конвейер разбора одного документа — общий для индексации, стенда и вложений.

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
            )
        return
    base = dict(doc_meta or {})
    ext = base.get("ext") or file_extension(path)
    base.update(content_metadata(io, ext))
    try:
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
    # Карточка — только у документа с текстом: без чанков файл в индекс не
    # попадает (_process_path помечает его «без текста»), и одинокая карточка
    # находила бы пустой документ. Последней — индексатор отмечает файл
    # готовым по чанку следующего файла, и карточка уходит с тем же файлом.
    if emitted and (card := document_card_text(meta)):
        # Поля места явно пустые: у карточки нет страницы и позиции в тексте,
        # а читатели строк (стенд, тесты) берут их по ключу, как у чанков.
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

    `permanent=True` — разбор прошёл, но текста нет: файл можно пометить
    обработанным. `permanent=False` — разбор упал: помечать нельзя, иначе
    файл выпадет из индексации навсегда.
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


async def _process_email_chunks(
    files: dict[str, AsyncBufferedReader],
    meta: FileMetadata,
    fail_cb: FailCallback,
):
    cfg = get_config()
    p = meta.path
    # rsplit, а не split: ключ имеет вид email_<папка>:<start>:<stop>, и имя
    # почтовой папки само может содержать двоеточие. Смещения — всегда два
    # последних поля и двоеточий не содержат, поэтому режем справа. При split
    # такая папка давала ValueError, письмо гасил внешний except (см.
    # async_parse_files) — и оно не индексировалось уже никогда.
    prefix, _start, _stop = p.rsplit(":", 2)
    prefix = prefix.removeprefix("email_")
    start, stop = int(_start), int(_stop)
    if stop - start > cfg.email_scan_cfg.max_message_size:
        # Вторая линия защиты после EmailsScanner: очередь лежит на диске и
        # переживает рестарт, поэтому может донести письмо, поставленное до
        # появления лимита. permanent=True — меньше письмо уже не станет.
        lg.debug(
            "skip oversized email %s: %s Б больше лимита %s Б",
            p,
            stop - start,
            cfg.email_scan_cfg.max_message_size,
        )
        await fail_cb(meta, permanent=True)
        return
    file = files.get(prefix)
    if not file:
        lg.warning(f"skip indexing email, no file for {prefix=}")
        # Без fail_cb письмо навсегда осталось бы в счётчике очереди, и
        # /status вечно показывал бы is_indexing=true.
        await fail_cb(meta, permanent=False)
        return
    await file.seek(start)
    data = await file.read(stop - start)
    msg = email.message_from_bytes(data)
    dt = datetime.fromtimestamp(meta.mtime_seconds)
    subject = _decode_filename(msg["Subject"])
    sender = normalize_field(_decode_filename(msg["From"]) if msg["From"] else None)
    lg.debug("process email subject=%s", subject)
    chunks_cnt = 0
    for part in process_message(msg):
        if isinstance(part, Body):
            doc_meta = DocumentMetadata(
                date_created=dt,
                date_modified=dt,
                original_name=subject,
                ext="body",
                th_message_id=msg["Message-ID"],
                conversation_id=msg["In-Reply-To"],
                source_type="email",
                title=normalize_field(subject),
                author=sender,
            )
            if not part.body:
                continue
            chunks = cfg.embed_splitter.split_text(part.body)
            lg.debug("split body into %s chunks", len(chunks))
            for idx, text in enumerate(chunks):
                yield ChunkWithMeta(
                    idx=idx,
                    text=text,
                    meta=doc_meta,
                    path=f"{p}:body",  # добавляем суффикc body чтобы не путать с остальными частями почтового сообщения
                    file_meta=meta,
                )
                chunks_cnt += 1
        elif isinstance(part, Attachment):
            if len(part.payload) > cfg.email_scan_cfg.max_attachment_size:
                lg.debug(
                    "skip oversized attachment %s (%s Б) in %s",
                    part.original_name,
                    len(part.payload),
                    p,
                )
                continue
            ext = file_extension(part.original_name)
            if get_parser(ext):
                doc_meta = DocumentMetadata(
                    date_created=dt,
                    date_modified=dt,
                    original_name=part.original_name,
                    ext=ext,
                    th_message_id=msg["Message-ID"],
                    conversation_id=msg["In-Reply-To"],
                    source_type="email_attachment",
                )
                lg.debug("process email attachment %s", part.original_name)
                async for chunk in _process_path(
                    meta,
                    fail_cb=fail_cb,
                    doc_meta=doc_meta,
                    ext=ext,
                    io=io.BytesIO(part.payload),
                ):
                    chunk["path"] = f"{meta.path}:{part.original_name}"
                    yield chunk
                    chunks_cnt += 1
    if chunks_cnt == 0:
        # DEBUG, а не WARNING: письмо без индексируемого текста — обычное дело
        # (автоответ, приглашение, одна картинка), и на ящике в десятки тысяч
        # писем WARNING на каждое превращает лог в шум. Типы частей отвечают
        # на вопрос «почему пусто» — например, только application/zip.
        content_types = ", ".join(sorted({x.get_content_type() for x in msg.walk()}))
        lg.debug(
            "no chunks generated for %s (%s Б, части: %s)",
            p,
            stop - start,
            content_types,
        )
        await fail_cb(meta, permanent=True)


async def async_parse_files(
    files: AsyncIterable["FileMetadata"],
    fail_cb: FailCallback,
    with_emails: bool = True,
):
    """Разобрать поток метаданных в чанки.

    `with_emails=False` — поток заведомо файловый, mbox открывать не нужно:
    открытие стоит обхода профиля и пула потоков на каждый вызов, а зовут её
    в том числе на каждую пачку от watcher'а. Проверка `email_` ниже остаётся:
    очередь переживает рестарт, и чужой ключ лучше отловить.
    """
    from src.utils import get_config

    cfg = get_config()
    emails_enabled = with_emails and cfg.email_enabled
    emails_files: dict[str, AsyncBufferedReader] = {}
    async with AsyncExitStack() as stack:
        if emails_enabled:
            email_files = list(cfg.email_files)
            executor = concurrent.futures.ThreadPoolExecutor(
                len(email_files) * 2 or 1,
                thread_name_prefix="read_email_file_pool",
            )
            # Пул создаётся на каждый проход — без закрытия его потоки
            # накапливались бы до конца жизни демона.
            stack.callback(executor.shutdown, False)
            for file, prefix in email_files:
                try:
                    emails_files[prefix] = await asyncio.wait_for(
                        stack.enter_async_context(
                            aiofiles.open(file, "br", executor=executor)
                        ),
                        5,
                    )
                except Exception:
                    # Недоступная папка не должна ронять индексацию: письма
                    # из неё пропустит _process_email_chunks («no file for
                    # prefix»), остальные папки и файлы индексируются дальше.
                    lg.exception(f"while opening email {file=}")
        async for p in files:
            if is_email_path(p.path):
                try:
                    async for chunk in _process_email_chunks(
                        emails_files, p, fail_cb=fail_cb
                    ):
                        yield chunk
                except GeneratorExit:
                    return
                except Exception:
                    # Ошибка одного письма (битый ключ, сбой чтения) не
                    # должна обрывать весь поток индексации — симметрично
                    # файловой ветке в _process_path.
                    lg.exception("processing email %s", p.path)
                    await fail_cb(p, permanent=False)
            else:
                try:
                    async for chunk in _process_path(
                        p,
                        fail_cb=fail_cb,
                    ):
                        yield chunk
                except GeneratorExit:
                    return


async def _process_path(
    meta: FileMetadata,
    fail_cb: FailCallback,
    doc_meta: DocumentMetadata | None = None,
    io: BinaryIO | None = None,
    ext: str | None = None,
):
    path = Path(meta.path)
    if ext is None:
        ext = file_extension(path)
    if parser := get_parser(ext.lower()):
        chunks = 0
        try:
            lg.debug("begin procesing %s", path)
            # Метаданные ФС — внутри try: файл, исчезнувший между сканом и
            # разбором, — временный сбой этого файла, а не падение всего прохода.
            async for chunk in wrap_iter_to_async(
                document_chunks(path, parser, io=io, doc_meta=doc_meta, file_meta=meta)
            ):
                yield chunk
                chunks += 1
        except SkipFile as skip:
            # Причина в содержимом (битый, зашифрован, без текста): одна строка
            # без стека. permanent=True — до изменения файла: дописанный файл
            # сменит размер и время и вернётся в индексацию сам.
            lg.warning("файл %s не индексируется: %s", path, skip.reason)
            await fail_cb(meta, permanent=skip.permanent)
            return
        except (OSError, MemoryError) as exc:
            # Файл занят, исчез или не хватило памяти — повторить на следующем проходе.
            lg.warning(
                "файл %s пока не прочитан (%s: %s) — повтор на следующем проходе",
                path,
                type(exc).__name__,
                exc,
            )
            await fail_cb(meta, permanent=False)
            return
        except Exception:
            # Непредвиденный сбой (ошибка в коде, инференс) — со стеком; файл НЕ
            # помечаем обработанным, чтобы следующий полный проход к нему вернулся.
            lg.exception("processing %s", path)
            await fail_cb(meta, permanent=False)
            return
        if chunks == 0:
            # Разобрали успешно, но текста нет — возвращаться незачем.
            lg.warning("no chunks generated for %s", path)
            await fail_cb(meta, permanent=True)
    else:
        lg.debug("no parser found for %s", path)


def parse_files_io(files: Iterable[tuple[io.BytesIO, str, DocumentMetadata | None]]):
    for file, ext, meta in files:
        if parser := get_parser(ext.lower()):
            for chunk in parser.iter_chunks_io(file):
                yield ChunkWithMeta(
                    **chunk,
                    meta=meta,
                    path="",
                )
        else:
            lg.debug("no parser found for %s", ext)
