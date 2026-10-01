"""Контракт почтовой очереди и наблюдаемость почтового прохода.

Регрессии по замечаниям к почтовому тракту: тип элементов в очереди,
раздельный счёт файлов и писем, фазовые логи.
"""

import pytest
from pytest import fixture

from src.common import FileMetadata
from src.config import Config, EmailConfig
from src.indexer.indexer import Index
from src.indexer.scanners.email_scanner import EmailsScanner
from src.thunderbird.find_files import list_email_folders
from src.utils.lsm_adapter import LSMCollection
from tests.mbox_fixture import default_inbox_messages, make_profile


@fixture
def mail_root(tmp_path):
    return make_profile(tmp_path / "tb", {"INBOX": default_inbox_messages()})


@fixture
def email_config(config: Config, mail_root, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(
        config,
        "email",
        EmailConfig(enabled=True, path_to_email_folder=str(mail_root)),
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "файл.txt").write_text("содержимое файла", encoding="utf-8")
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(docs)])
    config.__dict__.pop("lance", None)
    yield config
    config.__dict__.pop("lance", None)
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


# --------------------------------------------------- контракт очереди -------


@pytest.mark.asyncio
async def test_queue_holds_file_metadata_not_tuples(email_config, mail_root, tmp_path):
    """В очередь кладутся FileMetadata, а не кортежи (offset, meta).

    `iter_emails_as_metadata` отдаёт пары (start, meta), и если бы preindex
    пробрасывал их как есть, `iter_preindexed_queue` падал бы с
    AttributeError на `meta.path` — почта не индексировалась бы вовсе.
    """
    lsm = LSMCollection()
    with lsm:
        scanner = EmailsScanner(
            queue_path=tmp_path / "storage" / "emails",
            email_path=mail_root,
            folders=list_email_folders(mail_root),
            lsm_checker=lsm.indexed,
        )
        async with scanner:
            await scanner.scan()
            assert await scanner.total() > 0
            async for item in scanner.iter(endless=False):
                assert isinstance(item, FileMetadata), f"в очереди {type(item)}"
                assert item.path.startswith("email_INBOX:")


@pytest.mark.asyncio
async def test_preindex_yields_bare_metadata(email_config, mail_root, tmp_path):
    lsm = LSMCollection()
    with lsm:
        scanner = EmailsScanner(
            queue_path=tmp_path / "storage" / "emails",
            email_path=mail_root,
            folders=list_email_folders(mail_root),
            lsm_checker=lsm.indexed,
        )
        items = [m async for m in scanner.preindex()]
    assert items and all(isinstance(m, FileMetadata) for m in items)


# ------------------------------------------- раздельный счёт в /status ------


@pytest.mark.asyncio
async def test_status_splits_files_and_emails(email_config):
    """/status показывает отдельно файлы и письма.

    Общий `files_to_index` читался как «столько файлов осталось», хотя
    почти всё в нём — письма.
    """
    index = Index(email_config)
    async with index:
        try:
            await index._preindex_files()
            await index._preindex_emails()
            await index.count_to_index()
            status = await index.status()
            assert status.queued_files == 1
            assert status.queued_emails == 4
            assert status.files_to_index == status.queued_files + status.queued_emails
        finally:
            if index._index_task is not None:
                index._index_task.cancel()
            await index.stop_emails_polling()


@pytest.mark.asyncio
async def test_status_is_indexing_during_preindex(email_config, monkeypatch):
    """Сбор очереди — это работа, а не простой.

    Обход многогигабайтного mbox идёт минутами при нулевом counter, и
    /status отвечал "is_indexing: false" — клиент считал демон свободным.
    """
    index = Index(email_config)
    async with index:
        try:
            index.preindex = True
            status = await index.status()
            assert status.type == "preindex"
            assert status.is_indexing is True
        finally:
            index.preindex = False
            await index.stop_emails_polling()


@pytest.mark.asyncio
async def test_status_breakdown_zero_when_idle(email_config):
    index = Index(email_config)
    async with index:
        try:
            status = await index.status()
            assert status.files_to_index == 0
            assert status.queued_files == 0
            assert status.queued_emails == 0
        finally:
            await index.stop_emails_polling()


# ------------------------------------------------------ фазовые логи --------


@pytest.mark.asyncio
async def test_phase_logs_on_info_level(email_config, mail_root, tmp_path, caplog):
    """Обход mbox виден на INFO: размер, длительность, разбивка.

    Раньше всё было на DEBUG — в бою по логу нельзя было отличить
    «читаю большой mbox» от «завис».
    """
    lsm = LSMCollection()
    with lsm, caplog.at_level("INFO", logger="app.email"):
        scanner = EmailsScanner(
            queue_path=tmp_path / "storage" / "emails",
            email_path=mail_root,
            folders=list_email_folders(mail_root),
            lsm_checker=lsm.indexed,
        )
        [m async for m in scanner.preindex()]
    messages = [r.getMessage() for r in caplog.records]
    assert any("читаю mbox" in m for m in messages), messages
    assert any("mbox прочитан за" in m for m in messages), messages


@pytest.mark.asyncio
async def test_skipped_by_date_is_reported(
    email_config, mail_root, tmp_path, caplog, monkeypatch
):
    """Письма, отсечённые по index_last_n_days, видны в логе отдельно.

    Иначе непонятно, почему на 15000 писем в очередь ушло 500.
    """
    # index_last_n_days=1 отсекает всю фикстуру: её письма старше суток
    # (см. _NOW в tests/mbox_fixture.py — они от 5 до 2 суток назад)
    monkeypatch.setattr(
        email_config,
        "email",
        EmailConfig(
            enabled=True,
            path_to_email_folder=str(mail_root),
            index_last_n_days=1,
        ),
    )
    lsm = LSMCollection()
    with lsm, caplog.at_level("INFO", logger="app.email"):
        scanner = EmailsScanner(
            queue_path=tmp_path / "storage" / "emails",
            email_path=mail_root,
            folders=list_email_folders(mail_root),
            lsm_checker=lsm.indexed,
        )
        items = [m async for m in scanner.preindex()]
    assert items == [], "все письма должны быть отсечены по дате"
    done = [r.getMessage() for r in caplog.records if "прочитан за" in r.getMessage()]
    assert done and "4 пропущено по дате" in done[0], done
