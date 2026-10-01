"""Устойчивость почтового состояния: битый offsets.json, новые папки, смена корня.

Регрессии на устойчивость состояния почтового тракта.
"""

import json

import pytest
from pytest import fixture

from src.config import Config, EmailConfig
from src.indexer.indexer import Index
from src.indexer.scanners.email_scanner import EmailsScanner, FolderState
from src.server.model import UserConfig
from tests.mbox_fixture import default_inbox_messages, make_plain_email, make_profile


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
    monkeypatch.setattr(config.file_scan_cfg, "paths", [])
    config.__dict__.pop("lance", None)
    yield config
    config.__dict__.pop("lance", None)
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


def _scanner(cfg: Config, root, tmp_path, folders=("INBOX",)):
    return EmailsScanner(
        queue_path=tmp_path / "storage" / "emails",
        email_path=root,
        folders=list(folders),
        lsm_checker=None,  # type: ignore[arg-type]
    )


def _write_state(tmp_path, payload):
    p = tmp_path / "storage" / "offsets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


# ------------------------------------------------ битый offsets.json --------


@pytest.mark.parametrize(
    "payload",
    [
        '{"version": 2, "folders": {"INBOX": {"offset": 1',  # обрыв записи
        "",  # пустой файл
        "[1, 2, 3]",  # не dict
        '{"version": 2}',  # v2 без folders
        '{"version": 2, "folders": {"INBOX": {"unexpected": 1}}}',  # чужие ключи
        '{"version": 2, "folders": "не dict"}',
        "не json вовсе",
    ],
)
def test_corrupt_state_does_not_raise(email_config, mail_root, tmp_path, payload):
    """Битый offsets.json роняет старт демона — а не должен.

    Файл переживает обрыв процесса в полузаписанном виде, и раньше
    JSONDecodeError отсюда поднимался до конструктора Index, убивая
    индексацию и обычных файлов тоже.
    """
    _write_state(tmp_path, payload)
    scanner = _scanner(email_config, mail_root, tmp_path)
    # состояние восстановлено из списка папок, а не потеряно
    assert "INBOX" in scanner.folder_state


def test_valid_v2_state_is_loaded(email_config, mail_root, tmp_path):
    _write_state(
        tmp_path,
        {
            "version": 2,
            "folders": {"INBOX": {"offset": 42, "size": 100, "mtime": 7}},
        },
    )
    scanner = _scanner(email_config, mail_root, tmp_path)
    assert scanner.folder_state["INBOX"] == FolderState(offset=42, size=100, mtime=7)


# ------------------------------------------------- новые папки --------------


def test_new_folder_is_picked_up(email_config, mail_root, tmp_path):
    """Папка, созданная после прошлого прохода, должна попасть в состояние.

    Раньше folder_state брался ТОЛЬКО из offsets.json, и новая папка не
    индексировалась никогда — даже после рестарта демона.
    """
    _write_state(
        tmp_path,
        {"version": 2, "folders": {"INBOX": {"offset": 42, "size": 100, "mtime": 7}}},
    )
    scanner = _scanner(email_config, mail_root, tmp_path, folders=["INBOX", "Проекты"])
    assert scanner.folder_state["INBOX"].offset == 42, "старое состояние сохранено"
    assert scanner.folder_state["Проекты"] == FolderState(), "новая папка с нуля"


@pytest.mark.asyncio
async def test_folder_created_while_running_is_picked_up(
    email_config, mail_root, tmp_path
):
    """Папка, созданная при работающем демоне, индексируется без рестарта.

    Сканер создаётся один раз на весь процесс, поэтому список папок нельзя
    читать только в конструкторе: на живом поллинге новая папка иначе
    невидима до перезапуска.
    """
    from tests.mbox_fixture import write_mbox

    scanner = _scanner(email_config, mail_root, tmp_path)
    assert [m async for m in scanner.preindex()]  # первый проход
    assert "Проекты" not in scanner.folder_state

    write_mbox(mail_root / "Проекты", [make_plain_email("П", "тело", msg_id="p@t")])
    (mail_root / "Проекты.msf").write_bytes(b"")

    metas = [m async for m in scanner.preindex()]
    assert "Проекты" in scanner.folder_state
    assert any(m.path.startswith("email_Проекты:") for m in metas)


def test_detect_stale_sees_new_folders(email_config, mail_root, tmp_path):
    """detect_stale_folders тоже обновляет список — иначе новая папка
    не попадёт в состояние на почтовом цикле, который идёт до preindex."""
    from tests.mbox_fixture import write_mbox

    scanner = _scanner(email_config, mail_root, tmp_path)
    write_mbox(mail_root / "Новая", [make_plain_email("Н", "тело", msg_id="n@t")])
    (mail_root / "Новая.msf").write_bytes(b"")
    scanner.detect_stale_folders()
    assert "Новая" in scanner.folder_state


def test_state_written_atomically(email_config, mail_root, tmp_path):
    scanner = _scanner(email_config, mail_root, tmp_path)
    scanner._save_state()
    target = tmp_path / "storage" / "offsets.json"
    assert target.exists()
    assert not (tmp_path / "storage" / "offsets.json.tmp").exists()
    assert json.loads(target.read_text())["version"] == 2


# ------------------------------------------- смена почтового корня ----------


@pytest.mark.asyncio
async def test_scanner_recreated_on_root_change(email_config, tmp_path, monkeypatch):
    """Смена email.path_to_email_folder подхватывается без рестарта."""
    index = Index(email_config)
    first = await index._ensure_emails_scanner()
    assert first is not None

    other_root = make_profile(
        tmp_path / "tb2",
        {"INBOX": [make_plain_email("Другой ящик", "тело", msg_id="o@t")]},
    )
    monkeypatch.setattr(
        email_config,
        "email",
        EmailConfig(enabled=True, path_to_email_folder=str(other_root)),
    )
    second = await index._ensure_emails_scanner()
    assert second is not first
    assert second is not None and second.path == other_root


@pytest.mark.asyncio
async def test_scanner_retried_when_root_appears_later(
    config: Config, tmp_path, monkeypatch
):
    """Профиль Thunderbird появился после старта — почта всё равно заработает.

    Раньше start_emails_polling при неразрешимом корне не создавал задачу,
    и повторить попытку было уже некому.
    """
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    missing = tmp_path / "ещё-нет"
    monkeypatch.setattr(
        config,
        "email",
        EmailConfig(enabled=True, path_to_email_folder=str(missing)),
    )
    config.__dict__.pop("lance", None)
    try:
        index = Index(config)
        assert await index._ensure_emails_scanner() is None

        # Thunderbird досинхронизировался
        real_root = make_profile(missing, {"INBOX": default_inbox_messages()})
        monkeypatch.setattr(
            config,
            "email",
            EmailConfig(enabled=True, path_to_email_folder=str(real_root)),
        )
        scanner = await index._ensure_emails_scanner()
        assert scanner is not None and scanner.path == real_root
    finally:
        config.__dict__.pop("lance", None)
        config.__dict__.pop("user_conf_path", None)
        config.__dict__.pop("data_directory_path", None)


# ------------------------------------------------ user_conf.yml -------------


def test_broken_user_conf_does_not_break_config(email_config: Config):
    """Обрезанный user_conf.yml не должен ронять API и обход файлов."""
    p = email_config.user_conf_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("folders: [unclosed\n  bad: : yaml")
    assert email_config.user_config is None
    # фолбэк на конфиг из YAML/env — демон продолжает работать
    assert email_config.user_config_with_default is not None
    assert email_config.email_enabled is True


def test_user_conf_written_atomically(email_config: Config):
    email_config.update_user_config(UserConfig(folders=["/tmp/x"]))
    p = email_config.user_conf_path
    assert p.exists()
    assert not p.with_suffix(".yml.tmp").exists()
    assert email_config.user_config is not None
    assert email_config.user_config.folders == ["/tmp/x"]
