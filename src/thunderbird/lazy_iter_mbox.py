import email
import email.utils
import os
from collections.abc import Generator
from datetime import datetime
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser

from rich.progress import Progress

from src.common import FileMetadata
from src.utils import get_logger

linesep = os.linesep.encode("ascii")
logger = get_logger(__name__)

# Заголовки читаются кусками до пустой строки (границы блока), а не всё письмо:
# на вложении в 80 МБ это 80 МБ в памяти ради одной строки Date.
_HEADER_CHUNK = 64 * 1024
# Потолок на случай письма без границы заголовков (битый mbox): читать ради
# заголовков десятки мегабайт нельзя. Заголовки крупнее мегабайта — сломанная
# почта, а не длинная цепочка Received.
_HEADER_READ_LIMIT = 1024 * 1024
# Граница блока заголовков. Проверяются оба варианта переноса: mbox с АРМ
# приходит и с CRLF, и с LF.
_HEADER_SEPARATORS = (b"\r\n\r\n", b"\n\n")


def _read_header_block(file, length: int) -> bytes:
    """Прочитать блок заголовков письма, не поднимая в память его тело.

    Не одним фиксированным куском: MTA дописывают Received сверху, `Date`
    уезжает за первые десятки килобайт, а письмо без `Date` молча выпадает из
    индексации.
    """
    cap = min(length, _HEADER_READ_LIMIT)
    buf = bytearray()
    while len(buf) < cap:
        chunk = file.read(min(_HEADER_CHUNK, cap - len(buf)))
        if not chunk:
            break
        # Граница могла попасть на стык кусков, поэтому ищем с перекрытием
        # в три байта — на один короче самого длинного разделителя.
        search_from = max(0, len(buf) - 3)
        buf += chunk
        if any(buf.find(sep, search_from) != -1 for sep in _HEADER_SEPARATORS):
            break
    return bytes(buf)


def _generate_toc_lazy(
    file_name: str,
    progress: Progress | None = None,
    task_name: str = "Generating TOC",
    start_offset: int = 0,
):
    """Лениво отдать байтовые границы писем mbox — пары (start, stop).

    start — позиция строки `From ` письма, stop — конец письма.
    """
    starts = []
    last_was_empty = False
    try:
        _file_ctx = open(file_name, "rb")
    except OSError as e:
        # Недоступный mbox не должен ронять обход: папка могла быть удалена
        # или не синхронизирована локально.
        logger.warning("cannot open mbox %s: %s", file_name, e)
        return
    with _file_ctx as _file:
        _file.seek(start_offset)
        total_file_size = os.path.getsize(file_name)
        if start_offset > 0 and start_offset >= total_file_size:
            # Offset за концом файла — след компакции mbox: без предупреждения
            # новые письма молча не индексируются до полного сброса.
            logger.warning(
                "mbox %s: saved offset %s beyond file size %s — "
                "the mbox was likely compacted",
                file_name,
                start_offset,
                total_file_size,
            )
            return
        if start_offset >= total_file_size:
            return
        # Сканер передаёт прогресс всегда, в поставке — выключенный: он истинен,
        # а add_task отдаёт TaskID(0), и без проверки disable его обновление
        # (tell() и лок) шло бы на каждой строке многогигабайтного mbox.
        task = (
            progress.add_task(task_name, total=total_file_size - start_offset)
            if progress is not None and not progress.disable
            else None
        )
        while True:
            line_pos = _file.tell()
            line = _file.readline()

            if line.startswith(b"From "):
                if len(starts) > 0:
                    if last_was_empty:
                        stop_pos = line_pos - len(linesep)
                    else:
                        stop_pos = line_pos

                    yield (starts[-1], stop_pos)

                starts.append(line_pos)
                last_was_empty = False

            elif not line:
                if len(starts) > 0:
                    if last_was_empty:
                        stop_pos = line_pos - len(linesep)
                    else:
                        stop_pos = line_pos

                    yield (starts[-1], stop_pos)

                break

            elif line == linesep:
                last_was_empty = True
            else:
                last_was_empty = False

            if progress is not None and task is not None:
                progress.update(task, completed=_file.tell() - start_offset)
            # Граница — размер файла на входе: письмо, которое Thunderbird
            # дописывает во время обхода, не попадёт в TOC недописанным, его
            # прочитает следующий проход.
            if line_pos >= total_file_size:
                break


def _decode_filename(filename):
    """Decode MIME encoded filenames."""
    if not filename:
        return None

    try:
        decoded_parts = decode_header(filename)
        result = ""
        for content, charset in decoded_parts:
            if isinstance(content, bytes):
                if charset:
                    result += content.decode(charset, errors="replace")
                else:
                    result += content.decode("utf-8", errors="replace")
            else:
                result += content
        return result
    except Exception as e:
        logger.warning(f"Failed to decode filename {filename}: {e}")
        return filename


def _parse_date(value: str) -> datetime | None:
    """Разобрать дату письма; None — если она битая или вне диапазона datetime.

    Дата едет в метаданные как timestamp, а разбор письма переводит его
    обратно (datetime.fromtimestamp), поэтому дата, не переживающая этот
    путь (31 декабря 9999 года с поясом), — тоже битая.
    """
    try:
        date = email.utils.parsedate_to_datetime(value)
        datetime.fromtimestamp(date.timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return date


def _message_date(msg: Message) -> datetime | None:
    """Дата письма: заголовок Date, а если он битый — дата строки `From `.

    Битый Date (спам, ISO-дата автоматических систем, не-ASCII) иначе ронял
    бы обход всей папки на этом письме. Строку `From ` Thunderbird пишет,
    когда сохраняет письмо в mbox, — это время получения.
    """
    if (date := _parse_date(str(msg["Date"]))) is not None:
        return date
    parts = (msg.get_unixfrom() or "").split(None, 2)
    return _parse_date(parts[2]) if len(parts) == 3 else None


def _iter_emails(
    file_name: str,
    headersonly: bool = False,
    start_offset: int = 0,
    min_date: datetime | None = None,
    progress: Progress | None = None,
):
    _toc = _generate_toc_lazy(
        file_name,
        progress=progress,
        task_name="Reading emails",
        start_offset=start_offset,
    )
    is_first = True
    if min_date:
        logger.debug(f"searching for first message >= {min_date}")
    try:
        _file_ctx = open(file_name, "rb")
    except OSError as e:
        # Файл мог исчезнуть между проверкой в сканере и чтением.
        logger.warning("cannot open mbox %s: %s", file_name, e)
        return
    with _file_ctx as _file:
        for start, stop in _toc:
            _file.seek(start)
            length = stop - start
            _bytes = (
                _read_header_block(_file, length) if headersonly else _file.read(length)
            )
            msg = BytesParser().parsebytes(_bytes, headersonly=headersonly)
            if msg["Date"] is None:
                continue
            if (date := _message_date(msg)) is None:
                # Ни Date, ни строка `From ` не разбираются: письмо
                # пропускается, но обход папки идёт дальше и offset двигается.
                logger.warning(
                    "mbox %s: письмо по offset %s без даты (Date: %.80r) пропущено",
                    file_name,
                    start,
                    str(msg["Date"]),
                )
                yield None, (None, start, stop)
                continue
            if min_date and date:
                # min_date наивный, а aware и naive datetime не сравниваются.
                naive_date = date.replace(tzinfo=None)
                if naive_date < min_date:
                    yield None, (None, start, stop)
                    continue
            if is_first:
                logger.debug(f"first offset: {start}")
                is_first = False
            yield msg, (date, start, stop)


def iter_emails_as_metadata(
    file_name: str,
    prefix: str,
    start_offset: int = 0,
    min_date: datetime | None = None,
    progress: Progress | None = None,
) -> Generator[tuple[int, FileMetadata | None], None, None]:
    for msg, (date, start, stop) in _iter_emails(
        file_name=file_name,
        start_offset=start_offset,
        min_date=min_date,
        headersonly=True,
        progress=progress,
    ):
        if msg is None or date is None:
            yield start, None
        else:
            yield (
                start,
                FileMetadata(
                    f"{prefix}{start}:{stop}",
                    stop - start,
                    int(date.timestamp()),
                ),
            )
