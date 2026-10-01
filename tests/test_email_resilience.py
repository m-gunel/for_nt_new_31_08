"""Почта не должна быть способна уронить индексацию файлов.

Регрессии на баг с прода: при включённой почте и отсутствующей локально
папке (например, «Входящие» не синхронизированы) индексация не стартовала
вообще — FileNotFoundError из обхода mbox убивал задачу scan_all до того,
как она доходила до уже собранных в очередь файлов.

Все почтовые данные — синтетические mbox из tests/mbox_fixture.py, реальный
Thunderbird не нужен.
"""

import pytest
import pytest_asyncio as pa_fixture
from pytest import fixture

from src.config import Config, EmailConfig
from src.indexer.indexer import Index
from src.indexer.scanners.email_scanner import EmailsScanner
from src.thunderbird.find_files import find_email_root, list_email_folders
from src.thunderbird.lazy_iter_mbox import _generate_toc_lazy
from tests.mbox_fixture import default_inbox_messages, make_profile


@fixture
def mail_root(tmp_path):
    """Синтетический почтовый корень: INBOX с пятью письмами."""
    return make_profile(tmp_path / "tb-profile", {"INBOX": default_inbox_messages()})


@fixture
def email_config(config: Config, mail_root, tmp_path, monkeypatch):
    """Конфиг с включённой почтой, явным корнем и «висячей» папкой в offsets.

    Ровно сценарий бага: offsets ссылается и на существующий INBOX, и на
    папку, файла которой на диске нет.
    """
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "index_refresh_delay", 0.05)
    # data_directory во временный каталог: user_conf.yml не существует,
    # значит email_config берётся из поля config.email ниже.
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(
        config,
        "email",
        EmailConfig(
            enabled=True,
            path_to_email_folder=str(mail_root),
            offsets={"INBOX": 0, "Не-синхронизированные-Входящие": 0},
        ),
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "заметка.txt").write_text(
        "Локальный файл про регламент отпусков", encoding="utf-8"
    )
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(docs)])
    config.__dict__.pop("lance", None)
    yield config
    config.__dict__.pop("lance", None)
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


@pa_fixture.fixture
async def index(email_config: Config):
    idx = Index(email_config)
    async with idx:
        try:
            yield idx
        finally:
            if idx._index_task is not None:
                idx._index_task.cancel()
            await idx.stop_emails_polling()


# --------------------------------------------------------------- find root --


def test_find_email_root_without_sent_folder(tmp_path):
    """Корень находится по INBOX, даже когда «Отправленных» нет локально.

    Старый поиск был привязан к mUTF-7-имени папки «Отправленные»: без её
    локальной копии почта молча не индексировалась.
    """
    expected = make_profile(
        tmp_path / ".thunderbird" / "abc.default",
        {"INBOX": default_inbox_messages()},
    )
    assert find_email_root(tmp_path) == expected


def test_find_email_root_missing_profile(tmp_path):
    assert find_email_root(tmp_path) is None


def test_find_email_root_prefers_corporate_host(tmp_path):
    profile = tmp_path / ".thunderbird" / "abc.default"
    make_profile(
        profile, {"INBOX": default_inbox_messages()}, imap_host="imap.other.ru"
    )
    corp = make_profile(
        profile, {"INBOX": default_inbox_messages()}, imap_host="imap.sberbank.ru"
    )
    assert find_email_root(tmp_path) == corp


def test_list_email_folders_requires_pair(mail_root):
    # .msf без mbox рядом не считается папкой
    (mail_root / "Черновики.msf").write_bytes(b"")
    assert list_email_folders(mail_root) == ["INBOX"]


# --------------------------------------------------------------- toc scan ---


def test_toc_of_missing_file_is_empty():
    assert list(_generate_toc_lazy("/нет/такого/mbox")) == []


def test_toc_offset_beyond_eof_is_empty(mail_root, caplog):
    """Сохранённый offset за концом файла — след компакции: пусто + warning."""
    mbox = mail_root / "INBOX"
    with caplog.at_level("WARNING", logger="app.thunderbird.lazy_iter_mbox"):
        toc = list(_generate_toc_lazy(str(mbox), start_offset=10**9))
    assert toc == []
    assert any("compacted" in r.message for r in caplog.records)


# --------------------------------------------------------------- email_root -


def test_email_root_prefers_explicit_path(email_config: Config, mail_root):
    assert email_config.email_root == mail_root


def test_email_root_missing_explicit_path(email_config: Config, monkeypatch):
    """Несуществующий явный путь — None, без фолбэка на обход Thunderbird."""
    import src.thunderbird.find_files as ff

    monkeypatch.setattr(
        ff,
        "find_email_root",
        lambda home=None: pytest.fail("автопоиск не должен вызываться"),
    )
    monkeypatch.setattr(
        email_config,
        "email",
        EmailConfig(enabled=True, path_to_email_folder="/нет/такого"),
    )
    assert email_config.email_root is None


# ---------------------------------------------------------------- scanner ---


@pytest.mark.asyncio
async def test_preindex_skips_missing_folder(email_config: Config, tmp_path):
    """Висячая папка в offsets пропускается, существующая обходится."""
    scanner = EmailsScanner(
        queue_path=tmp_path / "storage" / "emails",
        email_path=email_config.email_root,
        folders=list_email_folders(email_config.email_root),
        lsm_checker=None,  # type: ignore[arg-type]  # preindex его не трогает
    )
    metas = [m async for m in scanner.preindex()]
    # 5 писем в INBOX, одно без Date пропускается; висячая папка — ноль,
    # но и ни одного исключения.
    assert len(metas) == 4
    assert all(m.path.startswith("email_INBOX:") for m in metas)


# ------------------------------------------------------- главный регресс ---


@pytest.mark.asyncio
async def test_missing_mbox_does_not_break_file_indexing(index: Index):
    """Висячая почтовая папка не срывает индексацию файлов.

    FileNotFoundError из почтовой ветки убивал scan_all после сбора файлов в
    очередь, но до их обработки — снаружи это выглядело как «индексация не
    стартовала».
    """
    await index.scan_all()

    paths = {
        row["path"]
        for store in index.cfg.lance
        if store.has_table
        for row in store.table.search().limit(100).to_list()
    }
    assert any(p.endswith("заметка.txt") for p in paths), (
        "файл должен быть проиндексирован несмотря на висячую почтовую папку"
    )
    # Письма из существующего INBOX тоже дошли до индекса.
    assert any(p.startswith("email_INBOX:") for p in paths)
    # Счётчик не залип: статус вернулся к idle.
    status = await index.status()
    assert status.files_to_index == 0
    assert status.is_indexing is False
