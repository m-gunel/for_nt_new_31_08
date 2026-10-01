import email
import email.utils
import os
from collections.abc import AsyncGenerator
from datetime import datetime
from email.header import decode_header
from email.parser import BytesParser
from typing import Literal, TypedDict

import aiofiles
from aiofiles.threadpool.binary import AsyncBufferedReader
from rich.progress import Progress

from src.common import FileMetadata
from src.utils import get_logger

linesep = "\n".encode("ascii")
logger = get_logger(__name__)


async def _iter_lines(f: AsyncBufferedReader, chunk_size=1024 * 1024):
    current_offset = await f.tell()
    remainder = b""

    while True:
        chunk = await f.read(chunk_size)
        if not chunk:
            # Last segment of the file (no trailing newline)
            if remainder:
                yield current_offset, remainder
            break

        combined = remainder + chunk
        # splitlines(keepends=True) is more efficient than manual splitting
        # as it handles \n, \r\n, etc., and preserves the byte length perfectly
        lines = combined.splitlines(keepends=True)

        # If the chunk doesn't end with a newline, the last element is incomplete
        if not combined.endswith((b"\n", b"\r")):
            remainder = lines.pop()
        else:
            remainder = b""

        for line in lines:
            yield current_offset, line
            current_offset += len(line)


async def _generate_toc_lazy(
    file_name: str,
    progress: Progress | None = None,
    task_name: str = "Generating TOC",
    start_offset: int = 0,
):
    """Generate key-to-(start, stop) table of contents as a lazy generator.

    This is a lazy version of generate_toc that yields (index, (start, stop)) pairs
    one at a time rather than building the entire dictionary at once.
    """
    starts: list[int] = []
    last_was_empty = False
    async with aiofiles.open(file_name, "rb") as _file:
        await _file.seek(start_offset)
        total_file_size = os.path.getsize(file_name)
        if start_offset >= total_file_size:
            return
        task = (
            progress.add_task(task_name, total=total_file_size - start_offset)
            if progress
            else None
        )
        while True:
            line_pos = await _file.tell()
            line = await _file.readline()

            if line.startswith(b"From "):
                # If we already have a start position, we can yield the previous message
                if len(starts) > 0:
                    if last_was_empty:
                        stop_pos = line_pos - len(linesep)
                    else:
                        stop_pos = line_pos

                    yield (starts[-1], stop_pos)

                starts.append(line_pos)
                last_was_empty = False

            elif not line:
                # End of file - yield the last message if we have one
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

            # Update progress
            if progress and task is not None:
                progress.update(task, completed=(await _file.tell()) - start_offset)
                if line_pos >= total_file_size:
                    break


class _Attachment(TypedDict):
    """TypedDict for attachment metadata."""

    offset_start: int
    offset_stop: int
    payload: bytes
    date_created: datetime | None
    original_name: str | None
    th_message_id: str | None
    content_type: str | None
    ext: str | None


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


async def _iter_emails(
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
    async with aiofiles.open(file_name, "rb") as _file:
        async for start, stop in _toc:
            await _file.seek(start)
            _bytes = await _file.read(stop - start)
            # msg = email.message_from_bytes(_bytes)
            msg = BytesParser().parsebytes(_bytes, headersonly=headersonly)
            if msg["Date"] is None:
                continue
            date = email.utils.parsedate_to_datetime(msg["Date"])
            if min_date and date:
                # Convert timezone-aware datetime to naive datetime for comparison
                naive_date = date.replace(tzinfo=None)
                if naive_date < min_date:
                    yield None, (None, start, stop)
                    continue
            if is_first:
                logger.debug(f"first offset: {start}")
                is_first = False
            yield msg, (date, start, stop)


async def async_iter_emails_as_metadata(
    file_name: str,
    prefix: Literal["email_i:", "email_o:"] = "email_i:",  # "i: - inbox, o: - outbox",
    start_offset: int = 0,
    min_date: datetime | None = None,
    progress: Progress | None = None,
) -> AsyncGenerator[tuple[int, FileMetadata | None], None, None]:
    async for msg, (date, start, stop) in _iter_emails(
        file_name=file_name,
        start_offset=start_offset,
        min_date=min_date,
        headersonly=True,
        progress=progress,
    ):
        if msg is None or date is None:
            yield (start, None)
        else:
            yield (
                start,
                FileMetadata(
                    f"{prefix}{start}:{stop}",
                    stop - start,
                    int(date.timestamp()),
                ),
            )
