"""Пороги размера в почтовом тракте.

Письмо разбирается только целиком в памяти, поэтому вложение на 80 МБ — это
80 МБ RSS ради пары строк текста. Обход за метаданными письмо целиком не
читает: ему нужен один заголовок Date.
"""

import builtins
import io
from email.parser import BytesParser

import aiofiles
import pytest
from pytest import fixture

from src.common import FileMetadata
from src.config import Config, EmailConfig, EmailScanConfig
from src.indexer.scanners.email_scanner import EmailsScanner, FolderState
from src.parsers import _process_email_chunks
from src.thunderbird.find_files import list_email_folders
from src.thunderbird.lazy_iter_mbox import (
    _HEADER_CHUNK,
    _read_header_block,
    iter_emails_as_metadata,
)
from tests.mbox_fixture import (
    make_email_with_attachment,
    make_plain_email,
    make_profile,
    minimal_docx,
)

# Заметно больше _HEADER_READ_LIMIT: иначе проверки ничего не значат.
_HUGE_PAYLOAD = b"\x00" * (256 * 1024)
_ATTACHMENT_NAME = "report.docx"
_ATTACHMENT_TEXT = "во вложении квартальные цифры по марже"


def _messages():
    """Три письма; тяжёлое — последним, чтобы проверять продвижение offset'а."""
    return [
        make_plain_email(
            "Короткое",
            "тело короткого письма",
            msg_id="small-1@fixture",
        ),
        make_email_with_attachment(
            "С документом",
            "тело письма с документом",
            msg_id="doc-1@fixture",
            filename=_ATTACHMENT_NAME,
            payload=minimal_docx(_ATTACHMENT_TEXT),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        make_email_with_attachment(
            "С тяжёлым вложением",
            "тело письма с тяжёлым вложением",
            msg_id="huge-1@fixture",
            filename="huge.bin",
            payload=_HUGE_PAYLOAD,
            maintype="application",
            subtype="octet-stream",
        ),
    ]


@fixture
def mail_root(tmp_path):
    return make_profile(tmp_path / "tb-profile", {"INBOX": _messages()})


@fixture
def email_config(config: Config, mail_root, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(
        config,
        "email",
        EmailConfig(enabled=True, path_to_email_folder=str(mail_root)),
    )
    yield config
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


def _scanner(mail_root, tmp_path) -> EmailsScanner:
    return EmailsScanner(
        queue_path=tmp_path / "storage" / "emails",
        email_path=mail_root,
        folders=list_email_folders(mail_root),
        lsm_checker=None,  # type: ignore[arg-type]  # preindex его не трогает
    )


def _metas(mail_root) -> list[FileMetadata]:
    return [
        meta
        for _start, meta in iter_emails_as_metadata(
            str(mail_root / "INBOX"), prefix="email_INBOX:"
        )
        if meta is not None
    ]


async def _chunks(mail_root, meta: FileMetadata):
    async def fail_cb(meta, *, permanent):
        return None

    async with aiofiles.open(mail_root / "INBOX", "rb") as f:
        async for chunk in _process_email_chunks({"INBOX": f}, meta, fail_cb):
            yield chunk


class _ReadSpy:
    """Файловый объект, считающий объём чтения: пиковый буфер и сумма по read().

    Считает и `readline()`: TOC (`_generate_toc_lazy`) строится им, отдельным
    дескриптором. Инвариант по памяти — размер одного буфера, сколько бы ни
    весило письмо.
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped
        self.max_buffer = 0
        self.total_read = 0

    def read(self, size=-1):
        data = self._wrapped.read(size)
        self.max_buffer = max(self.max_buffer, len(data))
        self.total_read += len(data)
        return data

    def readline(self, *args):
        data = self._wrapped.readline(*args)
        self.max_buffer = max(self.max_buffer, len(data))
        return data

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def __enter__(self):
        self._wrapped.__enter__()
        return self

    def __exit__(self, *exc):
        return self._wrapped.__exit__(*exc)


def test_metadata_pass_reads_only_headers(mail_root, monkeypatch):
    """Метаданные письма не должны стоить как чтение письма целиком."""
    mbox = str(mail_root / "INBOX")
    spies: list[_ReadSpy] = []
    real_open = builtins.open

    def spying_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if "b" in mode and str(file) == mbox:
            spy = _ReadSpy(handle)
            spies.append(spy)
            return spy
        return handle

    monkeypatch.setattr(builtins, "open", spying_open)
    metas = [
        meta
        for _start, meta in iter_emails_as_metadata(mbox, prefix="email_INBOX:")
        if meta is not None
    ]

    assert len(metas) == 3
    # в фикстуре обязано быть письмо крупнее куска чтения, иначе проверять нечего
    biggest = max(m.size for m in metas)
    assert biggest > _HEADER_CHUNK
    assert spies, "mbox так и не был открыт"
    # ни один буфер в памяти не превышает куска чтения — ни при построчном
    # обходе TOC, ни при чтении заголовков
    assert max(spy.max_buffer for spy in spies) <= _HEADER_CHUNK
    # через read() прочитано меньше, чем весит одно тяжёлое письмо: тела в
    # память не поднимаются
    assert sum(spy.total_read for spy in spies) < biggest


@pytest.mark.asyncio
async def test_oversized_message_not_queued(
    email_config, mail_root, tmp_path, monkeypatch
):
    """Тяжёлое письмо отсекается по TOC — его не читают и не ставят в очередь."""
    limit = 64 * 1024
    monkeypatch.setattr(email_config.email_scan_cfg, "max_message_size", limit)
    scanner = _scanner(mail_root, tmp_path)

    metas = [m async for m in scanner.preindex()]

    assert len(metas) == 2, "остаться должны только два лёгких письма"
    assert all(m.size <= limit for m in metas)
    # offset прошёл дальше пропущенного письма: иначе следующий проход
    # наткнётся на него снова
    last_queued_start = max(int(m.path.split(":")[1]) for m in metas)
    assert scanner.folder_state["INBOX"].offset > last_queued_start


def test_unknown_offsets_folder_ignored(email_config, mail_root, tmp_path, monkeypatch):
    """offsets из конфига не должны рождать папки, которых нет на диске.

    Swagger подставляет в тело запроса additionalProp1, и каждый проход писал
    бы по такой «папке» «mbox file not found».
    """
    monkeypatch.setattr(
        email_config,
        "email",
        EmailConfig(
            enabled=True,
            path_to_email_folder=str(mail_root),
            offsets={"INBOX": 0, "additionalProp1": 0},
        ),
    )
    scanner = _scanner(mail_root, tmp_path)

    assert "INBOX" in scanner.folder_state
    assert "additionalProp1" not in scanner.folder_state


@pytest.mark.asyncio
async def test_oversized_message_skipped_before_read(email_config, monkeypatch):
    """Письмо из очереди, набранной до появления лимита, не читается вовсе.

    files пуст намеренно: сработай гейт позже, разбор дошёл бы до
    «no file for prefix» и пометил письмо permanent=False.
    """
    monkeypatch.setattr(email_config.email_scan_cfg, "max_message_size", 1024)
    seen: list[bool] = []

    async def fail_cb(meta, *, permanent):
        seen.append(permanent)

    meta = FileMetadata("email_INBOX:0:5000", 5000, 0)
    chunks = [c async for c in _process_email_chunks({}, meta, fail_cb)]

    assert chunks == []
    assert seen == [True]


@pytest.mark.asyncio
async def test_attachment_indexed_within_limit(email_config, mail_root):
    """Контроль: вложение в пределах лимита попадает в индекс."""
    doc_meta = _metas(mail_root)[1]

    paths = [chunk["path"] async for chunk in _chunks(mail_root, doc_meta)]

    assert any(p.endswith(f":{_ATTACHMENT_NAME}") for p in paths)


@pytest.mark.asyncio
async def test_large_attachment_skipped_body_kept(email_config, mail_root, monkeypatch):
    """Слишком тяжёлое вложение пропускается, тело письма индексируется."""
    monkeypatch.setattr(email_config.email_scan_cfg, "max_attachment_size", 8)
    doc_meta = _metas(mail_root)[1]

    paths = [chunk["path"] async for chunk in _chunks(mail_root, doc_meta)]

    assert paths, "тело письма должно было проиндексироваться"
    assert all(p.endswith(":body") for p in paths)


def test_message_limit_fits_a_full_size_attachment():
    """Порог письма обязан вмещать вложение предельного размера.

    Вложение едет внутри письма в base64: +1/3 на кодирование и ещё ~2.6% на
    переносы строк. Если порог письма окажется ниже, порог вложения не
    сработает никогда — письмо отсечётся раньше, чем разбор дойдёт до вложений,
    и настройка превратится в мёртвую.
    """
    defaults = EmailScanConfig()
    encoded_size = defaults.max_attachment_size * 4 / 3 * (1 + 2 / 76)

    assert defaults.max_message_size > encoded_size


def test_message_with_late_date_header_is_not_lost(tmp_path):
    """`Date`, вытесненный за первый кусок чтения, не должен терять письмо.

    MTA дописывают Received сверху, и в длинной цепочке `Date` оказывается за
    десятками килобайт заголовков. Без Date письмо не дошло бы даже до очереди.
    """
    received = b"".join(
        b"Received: from relay%d.sberbank.ru by mx.sberbank.ru with ESMTP id %s;\r\n"
        b"\tTue, 01 Aug 2026 09:00:00 +0300\r\n" % (i, b"X" * 100)
        for i in range(500)
    )
    mbox = tmp_path / "INBOX"
    mbox.write_bytes(
        b"From nobody Tue Aug  1 09:00:00 2026\r\n"
        + received
        + b"Subject: pozdniy date\r\n"
        b"From: ivanov@sberbank.ru\r\n"
        b"Message-ID: <late-date@fixture>\r\n"
        b"Date: Tue, 01 Aug 2026 09:00:00 +0300\r\n"
        b"\r\n"
        b"telo pisma\r\n\r\n"
    )
    assert mbox.read_bytes().find(b"Date:") > _HEADER_CHUNK, (
        "фикстура не воспроизводит случай"
    )

    metas = [
        meta
        for _start, meta in iter_emails_as_metadata(str(mbox), prefix="email_INBOX:")
        if meta is not None
    ]

    assert len(metas) == 1


_HEADER_CASES = {
    "пустое письмо": b"",
    "только заголовки": b"Subject: a\r\nDate: Tue, 01 Aug 2026 09:00:00 +0300\r\n\r\n",
    "переносы LF": b"Subject: a\nDate: Tue, 01 Aug 2026 09:00:00 +0300\n\ntelo\n",
    "переносы CRLF": b"Subject: a\r\nDate: Tue, 01 Aug 2026 09:00:00 +0300\r\n\r\ntelo\r\n",
    "тело больше куска": (
        b"Subject: a\r\nDate: Tue, 01 Aug 2026 09:00:00 +0300\r\n\r\n"
        + b"x" * (3 * _HEADER_CHUNK)
    ),
    "заголовки больше куска": (
        b"".join(b"X-Pad-%d: %s\r\n" % (i, b"y" * 200) for i in range(600))
        + b"Date: Tue, 01 Aug 2026 09:00:00 +0300\r\n\r\ntelo\r\n"
    ),
    "разделитель на стыке кусков": b"X" * (_HEADER_CHUNK - 2) + b"\r\n\r\n" + b"telo",
    "письмо без разделителя": b"Subject: a\r\nDate: Tue, 01 Aug 2026 09:00:00 +0300\r\n",
}


@pytest.mark.parametrize(
    "case", sorted(_HEADER_CASES), ids=lambda c: c.replace(" ", "-")
)
def test_header_block_matches_full_read(case):
    """Чтение блоком обязано давать те же заголовки, что и чтение целиком.

    Выигрыш по памяти не должен менять то, что видит парсер.
    """
    raw = _HEADER_CASES[case]

    from_block = BytesParser().parsebytes(
        _read_header_block(io.BytesIO(raw), len(raw)), headersonly=True
    )
    from_full = BytesParser().parsebytes(raw, headersonly=True)

    assert dict(from_block.items()) == dict(from_full.items())


@pytest.mark.asyncio
async def test_clear_resets_offsets_even_when_config_has_them(
    email_config, mail_root, tmp_path, monkeypatch
):
    """`clear()` обязан обнулять позиции чтения, а не возвращать их из конфига.

    Его зовут в паре с `delete_prefix("email_")` при выключении почты: с
    офсетом из конфига удалённые письма не вернулись бы в индекс никогда.
    """
    monkeypatch.setattr(
        email_config,
        "email",
        EmailConfig(
            enabled=True,
            path_to_email_folder=str(mail_root),
            offsets={"INBOX": 5_635_568_676},
        ),
    )
    scanner = _scanner(mail_root, tmp_path)
    assert scanner.folder_state["INBOX"].offset == 5_635_568_676, (
        "офсет из конфига применяется при старте"
    )

    async with scanner:
        await scanner.clear()

    assert scanner.folder_state["INBOX"] == FolderState()


def test_config_offsets_override_is_logged(
    email_config, mail_root, tmp_path, monkeypatch, caplog
):
    """Перекрытие offsets из конфига сохранённым состоянием не должно быть немым.

    Приоритет offsets.json намеренный, но без записи в лог offsets из конфига
    выглядят неработающей ручкой.
    """
    import json
    import logging

    monkeypatch.setattr(
        email_config,
        "email",
        EmailConfig(
            enabled=True,
            path_to_email_folder=str(mail_root),
            offsets={"INBOX": 0},
        ),
    )
    queue_path = tmp_path / "storage" / "emails"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    (queue_path.parent / "offsets.json").write_text(
        json.dumps(
            {"version": 2, "folders": {"INBOX": {"offset": 999, "size": 1, "mtime": 1}}}
        )
    )

    with caplog.at_level(logging.INFO):
        scanner = _scanner(mail_root, tmp_path)

    assert scanner.folder_state["INBOX"].offset == 999, "приоритет не изменился"
    assert "INBOX" in caplog.text
    assert "offsets.json" in caplog.text
