import asyncio
import concurrent
import concurrent.futures
import email
import io
from collections.abc import AsyncIterable, Iterable
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Protocol

import aiofiles
from aiofiles.threadpool.binary import AsyncBufferedReader

from src.common import ChunkWithMeta, DocumentMetadata, FileMetadata, is_email_path
from src.thunderbird.get_body import Attachment, Body, process_message
from src.thunderbird.lazy_iter_mbox import _decode_filename
from src.utils import get_config, get_logger

from .base import file_extension, get_parser, get_parsers_registry
from .docx import DocParser, DocxParser
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
    "file_extension",
    "get_parsers_registry",
    "parse_files",
    "parse_files_io",
]

lg = get_logger(__name__)


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
            meta = parser.get_metadata(str(p))
            try:
                for chunk in parser.iter_chunks(str(p)):
                    yield ChunkWithMeta(
                        **chunk,
                        meta=meta,
                        path=str(p),
                    )
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
        doc_meta = doc_meta or parser.get_metadata(str(path))
        try:
            if io is None:
                lg.debug("begin procesing %s", path)
                async for chunk in parser.async_iter_chunks(str(path)):
                    yield ChunkWithMeta(
                        **chunk,
                        meta=doc_meta,
                        path=str(path),
                        file_meta=meta,
                    )
                    chunks += 1
            else:
                async for chunk in parser.async_iter_chunks_io(io):
                    yield ChunkWithMeta(
                        **chunk,
                        meta=doc_meta,
                        path=str(path),
                        file_meta=meta,
                    )
                    chunks += 1
        except Exception:
            # Разбор упал — это может быть временной причиной (файл ещё
            # дописывается, занят, сбой инференса). Файл НЕ помечаем
            # обработанным, чтобы следующий полный проход к нему вернулся.
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
