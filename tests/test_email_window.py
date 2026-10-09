"""Расширение окна index_last_n_days и max_message_size доиндексирует пропущенное.

preindex двигает offset и мимо писем, отсечённых окном или лимитом размера,
поэтому после расширения границ хвостом папки их не достать: папку нужно
перечитать с начала, а уже проиндексированные отсеет check_meta.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pytest import fixture

from src.config import Config, EmailConfig
from src.indexer.scanners.email_scanner import EmailsScanner
from src.thunderbird.find_files import list_email_folders
from tests.mbox_fixture import (
    make_email_with_attachment,
    make_plain_email,
    make_profile,
)

_NOW = datetime.now(UTC)
_MIB = 1024 * 1024


def _messages():
    """Два письма вне 30-дневного окна, одно в окне и одно тяжёлое в окне."""
    return [
        make_plain_email(
            "Старое 1", "тело", msg_id="old-1@fixture", date=_NOW - timedelta(days=60)
        ),
        make_plain_email(
            "Старое 2", "тело", msg_id="old-2@fixture", date=_NOW - timedelta(days=50)
        ),
        make_plain_email(
            "Новое", "тело", msg_id="new-1@fixture", date=_NOW - timedelta(days=5)
        ),
        make_email_with_attachment(
            "Тяжёлое",
            "тело",
            msg_id="big-1@fixture",
            filename="big.bin",
            payload=b"\x00" * (256 * 1024),
            maintype="application",
            subtype="octet-stream",
            date=_NOW - timedelta(days=2),
        ),
    ]


@fixture
def mail_root(tmp_path):
    return make_profile(tmp_path / "tb", {"INBOX": _messages()})


def _set_window(cfg: Config, monkeypatch, mail_root, days: int | None) -> None:
    monkeypatch.setattr(
        cfg,
        "email",
        EmailConfig(
            enabled=True, path_to_email_folder=str(mail_root), index_last_n_days=days
        ),
    )


@fixture
def email_config(config: Config, mail_root, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    _set_window(config, monkeypatch, mail_root, 30)
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


async def _paths(scanner: EmailsScanner) -> set[str]:
    return {m.path async for m in scanner.preindex()}


@pytest.mark.asyncio
async def test_widened_window_reads_skipped_mail(
    email_config, mail_root, tmp_path, monkeypatch
):
    scanner = _scanner(mail_root, tmp_path)
    first = await _paths(scanner)
    assert len(first) == 2, "в 30-дневное окно попадают два письма"

    _set_window(email_config, monkeypatch, mail_root, 365)
    second = await _paths(scanner)

    assert first < second and len(second) == 4, "старые письма не доиндексированы"


@pytest.mark.asyncio
async def test_window_widening_survives_restart(
    email_config, mail_root, tmp_path, monkeypatch
):
    """Окно расширили, пока демон стоял: границы берутся из offsets.json."""
    scanner = _scanner(mail_root, tmp_path)
    await _paths(scanner)
    scanner._save_state()

    _set_window(email_config, monkeypatch, mail_root, None)  # null — весь ящик
    restarted = _scanner(mail_root, tmp_path)

    assert len(await _paths(restarted)) == 4


@pytest.mark.asyncio
async def test_same_or_narrower_window_does_not_reread(
    email_config, mail_root, tmp_path, monkeypatch
):
    """Без расширения границ неизменившийся mbox по-прежнему не читается."""
    scanner = _scanner(mail_root, tmp_path)
    await _paths(scanner)
    assert await _paths(scanner) == set()

    _set_window(email_config, monkeypatch, mail_root, 7)
    assert await _paths(scanner) == set()


@pytest.mark.asyncio
async def test_raised_size_limit_reads_skipped_mail(
    email_config, mail_root, tmp_path, monkeypatch
):
    monkeypatch.setattr(email_config.email_scan_cfg, "max_message_size", 64 * 1024)
    scanner = _scanner(mail_root, tmp_path)
    first = await _paths(scanner)
    assert len(first) == 1, "тяжёлое письмо отсечено лимитом"

    monkeypatch.setattr(email_config.email_scan_cfg, "max_message_size", 48 * _MIB)
    second = await _paths(scanner)

    assert first < second and len(second) == 2


@pytest.mark.asyncio
async def test_limits_are_saved_with_state(email_config, mail_root, tmp_path):
    scanner = _scanner(mail_root, tmp_path)
    await _paths(scanner)
    scanner._save_state()

    data = json.loads((tmp_path / "storage" / "offsets.json").read_text())
    assert data["version"] == 2, "формат v2 сохраняется"
    saved = data["folders"]["INBOX"]
    assert isinstance(saved["min_date"], int)
    assert saved["max_size"] == email_config.email_scan_cfg.max_message_size
    reloaded = _scanner(mail_root, tmp_path)
    assert reloaded.folder_state["INBOX"] == scanner.folder_state["INBOX"]


@pytest.mark.asyncio
async def test_unbounded_window_state_roundtrip(
    email_config, mail_root, tmp_path, monkeypatch
):
    """Окно null пишется как "min_date": null и читается обратно, а не
    уводит весь offsets.json в ветку «повреждён»."""
    _set_window(email_config, monkeypatch, mail_root, None)
    scanner = _scanner(mail_root, tmp_path)
    await _paths(scanner)
    scanner._save_state()

    data = json.loads((tmp_path / "storage" / "offsets.json").read_text())
    assert data["folders"]["INBOX"]["min_date"] is None
    reloaded = _scanner(mail_root, tmp_path)
    assert reloaded.folder_state["INBOX"] == scanner.folder_state["INBOX"]
    assert await _paths(reloaded) == set()


@pytest.mark.asyncio
async def test_legacy_state_without_limits_is_not_reread(
    email_config, mail_root, tmp_path, monkeypatch
):
    """offsets.json прежней версии: границ нет — полного перечитывания нет."""
    st = (mail_root / "INBOX").stat()
    state = tmp_path / "storage" / "offsets.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "version": 2,
                "folders": {
                    "INBOX": {
                        "offset": 0,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    }
                },
            }
        )
    )
    _set_window(email_config, monkeypatch, mail_root, 365)

    assert await _paths(_scanner(mail_root, tmp_path)) == set()
