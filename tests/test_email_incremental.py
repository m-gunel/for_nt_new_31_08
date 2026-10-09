"""Инкрементальность почты: новые письма подхватываются, удалённые уходят.

Компакция mbox (Thunderbird переписывает файл при окончательном удалении
писем) сдвигает все байтовые offset'ы, а ключ индекса — это offset: без сброса
состояния папки новые письма не подхватывались бы (offset за EOF), а чанки
удалённых оставались бы в индексе навсегда.
"""

import json
import logging

import pytest
import pytest_asyncio as pa_fixture
from pytest import fixture

from src.config import Config, EmailConfig
from src.indexer.indexer import Index
from src.indexer.scanners.email_scanner import EmailsScanner, FolderState
from src.thunderbird.find_files import list_email_folders
from tests.mbox_fixture import (
    append_to_mbox,
    compact_mbox,
    default_inbox_messages,
    make_plain_email,
    make_profile,
)


@fixture
def mail_root(tmp_path):
    return make_profile(tmp_path / "tb-profile", {"INBOX": default_inbox_messages()})


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
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(docs)])
    config.__dict__.pop("lance", None)
    yield config
    config.__dict__.pop("lance", None)
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


@fixture
def scanner(email_config: Config, mail_root, tmp_path):
    return EmailsScanner(
        queue_path=tmp_path / "storage" / "emails",
        email_path=mail_root,
        folders=list_email_folders(mail_root),
        lsm_checker=None,  # type: ignore[arg-type]  # preindex его не трогает
    )


async def _drain(scanner: EmailsScanner):
    return [m async for m in scanner.preindex()]


@pytest.mark.asyncio
async def test_unchanged_mbox_is_skipped(scanner: EmailsScanner):
    """Файл не менялся между проходами — второй проход его даже не читает."""
    first = await _drain(scanner)
    assert len(first) == 4
    second = await _drain(scanner)
    assert second == []
    # именно пропуск по (size, mtime), а не пустой хвост по offset'у:
    # состояние заполнено реальными size/mtime
    st = scanner.folder_state["INBOX"]
    assert st.size > 0 and st.mtime > 0


@pytest.mark.asyncio
async def test_new_email_is_picked_up(scanner: EmailsScanner, mail_root):
    await _drain(scanner)
    append_to_mbox(
        mail_root / "INBOX",
        [make_plain_email("Новое", "тело нового письма", msg_id="new-1@fixture")],
    )
    new = await _drain(scanner)
    # хвост с offset'а последнего письма: старое граничное письмо отсекает
    # LSM-проверка выше по стеку, здесь важно, что новое письмо пришло
    assert any("new-1" not in m.path and m.path.startswith("email_INBOX:") for m in new)
    assert len(new) <= 2  # граничное + новое, но не весь ящик заново


@pytest.mark.asyncio
async def test_compaction_resets_folder_state(scanner: EmailsScanner, mail_root):
    """После компакции detect_stale_folders требует переиндексации папки,
    а сбрасывает состояние forget_folder — после удаления чанков."""
    await _drain(scanner)
    before = scanner.folder_state["INBOX"]
    assert before.offset > 0
    compact_mbox(mail_root / "INBOX", drop_msg_ids={"plain-1@fixture"})
    stale = scanner.detect_stale_folders()
    assert stale == ["INBOX"]
    assert scanner.folder_state["INBOX"] == before, "до удаления состояние прежнее"
    scanner.forget_folder("INBOX")
    assert scanner.folder_state["INBOX"] == FolderState()
    # после сброса полный перескан отдаёт все живые письма
    again = await _drain(scanner)
    assert len(again) == 3


@pytest.mark.asyncio
async def test_vanished_mbox_drops_folder(scanner: EmailsScanner, mail_root):
    await _drain(scanner)
    (mail_root / "INBOX").unlink()
    assert scanner.detect_stale_folders() == ["INBOX"]
    # чанки удалены — папка уходит из состояния, повторный вызов пуст
    scanner.forget_folder("INBOX")
    assert "INBOX" not in scanner.folder_state
    assert scanner.detect_stale_folders() == []


@pytest.mark.asyncio
async def test_stale_folder_redetected_until_forgotten(
    scanner: EmailsScanner, mail_root
):
    """Удаление по префиксу упало или проход отменили (forget_folder не
    вызван) — папка остаётся протухшей, а не теряет старые ключи навсегда."""
    await _drain(scanner)
    compact_mbox(mail_root / "INBOX", drop_msg_ids={"plain-1@fixture"})
    assert scanner.detect_stale_folders() == ["INBOX"]
    assert scanner.detect_stale_folders() == ["INBOX"]


@pytest.mark.asyncio
async def test_untouched_folder_not_stale(scanner: EmailsScanner):
    """Папка, которую ещё не индексировали, не считается протухшей."""
    assert scanner.detect_stale_folders() == []


@pytest.mark.asyncio
async def test_state_roundtrip_v2(email_config, mail_root, tmp_path):
    qp = tmp_path / "storage" / "emails"
    scanner = EmailsScanner(
        queue_path=qp,
        email_path=mail_root,
        folders=["INBOX"],
        lsm_checker=None,  # type: ignore[arg-type]
    )
    await _drain(scanner)
    saved = scanner.folder_state["INBOX"]
    async with scanner:
        pass  # __aexit__ пишет offsets.json
    data = json.loads((qp.parent / "offsets.json").read_text())
    assert data["version"] == 2

    reloaded = EmailsScanner(
        queue_path=qp,
        email_path=mail_root,
        folders=["INBOX"],
        lsm_checker=None,  # type: ignore[arg-type]
    )
    assert reloaded.folder_state["INBOX"] == saved


def test_state_reads_legacy_v1(email_config, mail_root, tmp_path):
    qp = tmp_path / "storage" / "emails"
    qp.parent.mkdir(parents=True, exist_ok=True)
    (qp.parent / "offsets.json").write_text(json.dumps({"INBOX": 123}))
    scanner = EmailsScanner(
        queue_path=qp,
        email_path=mail_root,
        folders=["INBOX"],
        lsm_checker=None,  # type: ignore[arg-type]
    )
    assert scanner.folder_state["INBOX"] == FolderState(offset=123)


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
    # Обе таблицы: файлы и письма живут раздельно.
    return {
        row["path"]
        for store in cfg.lance
        if store.has_table
        for row in store.table.search().limit(1000).to_list()
    }


@pytest.mark.asyncio
async def test_failed_delete_is_retried_next_pass(index: Index, mail_root, monkeypatch):
    """Сбой delete_prefix после компакции не оставляет в индексе ключей,
    указывающих на чужие байты: следующий проход повторяет удаление."""
    await index.scan_all()
    compact_mbox(mail_root / "INBOX", drop_msg_ids={"plain-1@fixture"})

    async def failing_delete(prefix: str) -> None:
        raise RuntimeError("сбой записи в Lance")

    monkeypatch.setattr(index, "delete_prefix", failing_delete)
    await index.scan_all(emails_only=True)  # сбой гасит _preindex_emails
    monkeypatch.delattr(index, "delete_prefix")
    await index.scan_all(emails_only=True)

    mbox_bytes = (mail_root / "INBOX").read_bytes()
    email_keys = {p for p in _indexed_paths(index.cfg) if p.startswith("email_INBOX:")}
    assert email_keys
    for key in email_keys:
        start = int(key.split(":")[1])
        assert mbox_bytes[start : start + 5] == b"From ", key


@pytest.mark.asyncio
async def test_vanished_folder_leaves_state(index: Index, mail_root, caplog):
    """Исчезнувшая папка после удаления её чанков уходит из состояния:
    иначе WARNING «mbox file not found» шёл бы на каждом поллинге."""
    await index.scan_all()
    (mail_root / "INBOX").unlink()
    (mail_root / "INBOX.msf").unlink()
    await index.scan_all(emails_only=True)

    assert index.emails_scanner is not None
    assert "INBOX" not in index.emails_scanner.folder_state
    assert not any(p.startswith("email_INBOX:") for p in _indexed_paths(index.cfg))
    with caplog.at_level(logging.WARNING, logger="app.email"):
        await index.scan_all(emails_only=True)
    assert "mbox file not found" not in caplog.text


@pytest.mark.asyncio
async def test_compaction_cleans_index_and_keeps_fresh(index: Index, mail_root):
    """Полный цикл: скан → компакция → скан. Удалённое письмо уходит из
    индекса, живые переиндексируются с корректными ключами, новые письма
    после компакции продолжают подхватываться."""
    await index.scan_all()
    before = _indexed_paths(index.cfg)
    assert any(p.startswith("email_INBOX:") for p in before)

    # «Удалено из корзины»: Thunderbird компактит mbox
    compact_mbox(mail_root / "INBOX", drop_msg_ids={"plain-1@fixture"})
    append_to_mbox(
        mail_root / "INBOX",
        [
            make_plain_email(
                "После компакции",
                "письмо после компакции",
                msg_id="post-compact@fixture",
            )
        ],
    )
    await index.scan_all(emails_only=True)

    after = _indexed_paths(index.cfg)
    # старые ключи ушли с delete_prefix по папке: все email-ключи указывают
    # на границы писем в новом файле
    mbox_bytes = (mail_root / "INBOX").read_bytes()
    email_keys = {p for p in after if p.startswith("email_INBOX:")}
    assert email_keys, "почта должна быть в индексе после компакции"
    for key in email_keys:
        start, stop = map(int, key.split(":")[1:3])
        assert mbox_bytes[start : start + 5] == b"From ", key
    # письмо, добавленное после компакции, проиндексировано
    assert any(
        b"post-compact@fixture"
        in mbox_bytes[int(k.split(":")[1]) : int(k.split(":")[2])]
        for k in email_keys
    )
    # состояние согласовано: статус вернулся в idle
    status = await index.status()
    assert status.files_to_index == 0
