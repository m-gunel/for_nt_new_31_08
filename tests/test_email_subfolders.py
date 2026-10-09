"""Вложенные папки Thunderbird индексируются наравне с папками верхнего уровня.

Подпапку Thunderbird кладёт в каталог ``<родитель>.sbd/`` рядом с mbox
родителя: ``INBOX.sbd/Проекты`` + ``INBOX.sbd/Проекты.msf``.
"""

import pytest
import pytest_asyncio as pa_fixture
from pytest import fixture

from src.config import Config, EmailConfig
from src.indexer.indexer import Index
from src.indexer.scanners.email_scanner import EmailsScanner
from src.server.model import RequestFilePath
from src.server.router import get_file_path
from src.thunderbird.find_files import find_email_root, list_email_folders
from src.thunderbird.lazy_iter_mbox import iter_emails_as_metadata
from tests.mbox_fixture import (
    compact_mbox,
    default_inbox_messages,
    make_plain_email,
    make_profile,
    write_mbox,
)

_FOLDERS = ["INBOX", "INBOX.sbd/Проекты", "INBOX.sbd/Проекты.sbd/Ромашка"]


def _add_folder(parent_dir, name: str, msg_id: str) -> None:
    write_mbox(
        parent_dir / name, [make_plain_email(name, f"тело {name}", msg_id=msg_id)]
    )
    (parent_dir / f"{name}.msf").write_bytes(b"")


@fixture
def mail_root(tmp_path):
    root = make_profile(
        tmp_path / ".thunderbird" / "p.default",
        {"INBOX": default_inbox_messages()[:1]},
    )
    _add_folder(root / "INBOX.sbd", "Проекты", "proj@fixture")
    _add_folder(root / "INBOX.sbd" / "Проекты.sbd", "Ромашка", "romashka@fixture")
    return root


@fixture
def email_config(config: Config, mail_root, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "index_refresh_delay", 0.05)
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(
        config,
        "email",
        EmailConfig(enabled=True, path_to_email_folder=str(mail_root)),
    )
    monkeypatch.setattr(config.file_scan_cfg, "paths", [])
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


def _indexed_paths(cfg: Config) -> set[str]:
    return {
        row["path"]
        for store in cfg.lance
        if store.has_table
        for row in store.table.search().limit(1000).to_list()
    }


def test_list_email_folders_includes_subfolders(mail_root):
    assert sorted(list_email_folders(mail_root)) == _FOLDERS


def test_sbd_symlink_loop_is_not_followed(mail_root):
    """.sbd-симлинк на предка не зацикливает обход и не плодит дубли."""
    (mail_root / "INBOX.sbd" / "Петля.sbd").symlink_to(mail_root / "INBOX.sbd")
    assert sorted(list_email_folders(mail_root)) == _FOLDERS


def test_email_root_found_by_subfolder_only(tmp_path):
    """Корень без пар верхнего уровня (INBOX не синхронизирован), но с
    синхронизированной подпапкой — тоже почтовый."""
    host = tmp_path / ".thunderbird" / "p.default" / "ImapMail" / "imap.sberbank.ru"
    _add_folder(host / "INBOX.sbd", "Проекты", "proj@fixture")
    assert find_email_root(tmp_path) == host


def test_email_files_include_subfolders(email_config: Config, mail_root):
    files = {name: path for path, name in email_config.email_files}
    assert sorted(files) == _FOLDERS
    assert files["INBOX.sbd/Проекты"] == mail_root / "INBOX.sbd" / "Проекты"


@pytest.mark.asyncio
async def test_subfolder_mail_is_preindexed(email_config: Config, mail_root, tmp_path):
    scanner = EmailsScanner(
        queue_path=tmp_path / "storage" / "emails",
        email_path=mail_root,
        folders=list_email_folders(mail_root),
        lsm_checker=None,  # type: ignore[arg-type]  # preindex его не трогает
    )
    folders = {m.path.rsplit(":", 2)[0] async for m in scanner.preindex()}
    assert folders == {f"email_{name}" for name in _FOLDERS}


@pytest.mark.asyncio
async def test_get_file_path_for_subfolder_key(email_config: Config, mail_root):
    """Ключ письма подпапки открывается: папка ищется по имени со слэшем."""
    mbox = mail_root / "INBOX.sbd" / "Проекты"
    (_start, meta), *_ = iter_emails_as_metadata(
        str(mbox), prefix="email_INBOX.sbd/Проекты:"
    )
    assert meta is not None
    resp = await get_file_path(
        RequestFilePath(absolute_path=f"{meta.path}:body"), email_config
    )
    assert resp.absolute_temp_path.endswith(".eml")


@pytest.mark.asyncio
async def test_subfolders_indexed_and_survive_parent_compaction(
    index: Index, mail_root
):
    """Письма подпапок попадают в индекс; компакция родителя сносит только
    его префикс: ``email_INBOX:`` не задевает ``email_INBOX.sbd/…``."""
    await index.scan_all()
    paths = _indexed_paths(index.cfg)
    for name in _FOLDERS:
        assert any(p.startswith(f"email_{name}:") for p in paths), name

    compact_mbox(mail_root / "INBOX", drop_msg_ids={"plain-1@fixture"})
    await index.scan_all(emails_only=True)

    paths = _indexed_paths(index.cfg)
    assert not any(p.startswith("email_INBOX:") for p in paths)
    for name in _FOLDERS[1:]:
        assert any(p.startswith(f"email_{name}:") for p in paths), name
