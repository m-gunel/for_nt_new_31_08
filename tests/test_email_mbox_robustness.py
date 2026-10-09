"""Обход mbox: кривая дата письма и прогресс-бар не должны мешать индексации.

Письма собираются вручную, байтами: EmailMessage сам разбирает Date при
присваивании и битый заголовок построить не даст.
"""

from datetime import datetime

import pytest
import rich.progress
from pytest import fixture

from src.config import Config, EmailConfig
from src.indexer.scanners.email_scanner import EmailsScanner
from src.thunderbird.find_files import list_email_folders
from src.thunderbird.lazy_iter_mbox import _generate_toc_lazy, iter_emails_as_metadata

_FROM_LINE = b"From - Thu Oct 01 10:00:00 2026"
# Строку From Thunderbird пишет в местном времени и без пояса.
_FROM_LINE_TS = int(datetime(2026, 10, 1, 10, 0).timestamp())
_GOOD_DATE = b"Tue, 06 Oct 2026 09:00:00 +0300"


def _raw_email(n: int, date: bytes | None, from_line: bytes = _FROM_LINE) -> bytes:
    head = from_line + b"\nSubject: m%d\nMessage-ID: <m%d@fixture>\n" % (n, n)
    if date is not None:
        head += b"Date: " + date + b"\n"
    return head + b"\nbody %d\n" % n


def _write_mbox(path, emails: list[bytes]) -> None:
    """mbox и пустой .msf рядом: папкой list_email_folders считает только пару."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n".join(emails))
    (path.parent / f"{path.name}.msf").write_bytes(b"")


def _metas(mbox) -> list:
    return [
        m for _start, m in iter_emails_as_metadata(str(mbox), prefix="email_INBOX:")
    ]


_BROKEN_DATES = {
    "мусор": b"not a date",
    "пустой": b"",
    "ISO": b"2024-01-05 10:00:00",
    "не-ASCII": "Пн, 5 янв 2024 10:00:00 +0300".encode(),
    "32 января": b"Mon, 32 Jan 2024 10:00:00 +0000",
    "год 99999": b"Mon, 5 Jan 99999 10:00:00 +0000",
    "год больше C long": b"Mon, 5 Jan 99999999999999999999 10:00:00 +0000",
    "пояс +9999": b"Mon, 5 Jan 2024 10:00:00 +9999",
    "конец 9999 года с поясом": b"Fri, 31 Dec 9999 23:59:59 -2359",
}


@pytest.mark.parametrize(
    "case", sorted(_BROKEN_DATES), ids=lambda c: c.replace(" ", "-")
)
def test_broken_date_takes_from_line_date(tmp_path, case):
    """Письмо с битым Date получает дату строки From, соседние — свои даты."""
    mbox = tmp_path / "INBOX"
    _write_mbox(
        mbox,
        [
            _raw_email(1, _GOOD_DATE),
            _raw_email(2, _BROKEN_DATES[case]),
            _raw_email(3, b"Wed, 07 Oct 2026 09:00:00 +0300"),
        ],
    )

    metas = _metas(mbox)

    assert [m is not None for m in metas] == [True, True, True]
    assert metas[1].mtime_seconds == _FROM_LINE_TS
    assert metas[0].mtime_seconds != _FROM_LINE_TS


def test_undatable_email_is_skipped_not_fatal(tmp_path, caplog):
    """Нет даты ни в Date, ни в строке From — письмо пропускается с WARNING."""
    mbox = tmp_path / "INBOX"
    _write_mbox(
        mbox,
        [
            _raw_email(1, _GOOD_DATE),
            _raw_email(2, b"not a date", from_line=b"From - no date here"),
            _raw_email(3, _GOOD_DATE),
        ],
    )

    with caplog.at_level("WARNING", logger="app.thunderbird.lazy_iter_mbox"):
        metas = _metas(mbox)

    assert [m is not None for m in metas] == [True, False, True]
    assert any("not a date" in r.getMessage() for r in caplog.records)


@fixture
def mail_root(config: Config, tmp_path, monkeypatch):
    """Почтовый корень во временном каталоге, почта включена, окна по дате нет."""
    root = tmp_path / "ImapMail" / "imap.sberbank.ru"
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(
        config,
        "email",
        EmailConfig(
            enabled=True, path_to_email_folder=str(root), index_last_n_days=None
        ),
    )
    yield root
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


@pytest.mark.asyncio
async def test_broken_date_does_not_stop_folder_and_next_folders(mail_root, tmp_path):
    """Письмо с битым Date не обрывает обход: дочитана его папка и следующая."""
    root = mail_root
    _write_mbox(
        root / "INBOX",
        [
            _raw_email(1, _GOOD_DATE),
            _raw_email(2, b"not a date"),
            _raw_email(3, _GOOD_DATE),
        ],
    )
    _write_mbox(root / "Sent", [_raw_email(4, _GOOD_DATE)])
    scanner = EmailsScanner(
        queue_path=tmp_path / "storage" / "emails",
        email_path=root,
        folders=list_email_folders(root),
        lsm_checker=None,  # type: ignore[arg-type]  # preindex его не трогает
    )

    metas = [m async for m in scanner.preindex()]

    assert sorted(m.path.split(":")[0] for m in metas) == [
        "email_INBOX",
        "email_INBOX",
        "email_INBOX",
        "email_Sent",
    ]
    assert all(st.size > 0 for st in scanner.folder_state.values())


def test_disabled_progress_is_not_touched(tmp_path, monkeypatch):
    """Выключенный прогресс-бар не обновляется на каждой строке mbox."""
    mbox = tmp_path / "INBOX"
    _write_mbox(mbox, [_raw_email(n, _GOOD_DATE) for n in range(3)])
    calls: list[str] = []
    progress = rich.progress.Progress(disable=True)
    monkeypatch.setattr(
        progress, "add_task", lambda *a, **k: calls.append("add_task") or 0
    )
    monkeypatch.setattr(progress, "update", lambda *a, **k: calls.append("update"))

    toc = list(_generate_toc_lazy(str(mbox), progress=progress))

    assert len(toc) == 3
    assert calls == []


def test_enabled_progress_still_reports(tmp_path):
    """Включённый прогресс-бар (SHOW_PROGRESS=Y) по-прежнему двигается."""
    mbox = tmp_path / "INBOX"
    _write_mbox(mbox, [_raw_email(n, _GOOD_DATE) for n in range(3)])
    progress = rich.progress.Progress()  # не запущен: в терминал не пишет

    list(_generate_toc_lazy(str(mbox), progress=progress))

    [task] = progress.tasks
    assert task.completed == mbox.stat().st_size


def test_toc_stops_at_size_snapshot_without_progress(tmp_path):
    """Письмо, дописанное во время обхода, — дело следующего прохода.

    Граница по размеру файла на входе не должна зависеть от прогресс-бара.
    """
    mbox = tmp_path / "INBOX"
    mbox.write_bytes(
        _raw_email(1, _GOOD_DATE) + b"\n" + _raw_email(2, _GOOD_DATE) + b"\n"
    )
    expected = list(_generate_toc_lazy(str(mbox)))
    toc = _generate_toc_lazy(str(mbox))

    got = [next(toc)]
    with open(mbox, "ab") as f:
        f.write(_raw_email(3, _GOOD_DATE))
    got += list(toc)

    assert got == expected
