"""Неверно заданный путь сканирования должен быть слышен.

Локализованных имён папок (`~/Документы`, `~/Загрузки`) нет ни в macOS, ни в
англоязычном Linux, и молчаливый обход по такому пути выглядел бы как поломка
поиска.
"""

import logging
import os
from pathlib import Path

import pytest
from pytest import fixture

from src.config import Config
from src.indexer.scan_dir import iter_file_metadata
from src.indexer.scanners.files_scanner import FilesScanner
from src.utils.lsm_adapter import LSMTable, _decode_file_meta, _encode_file_meta


@fixture
def lsm_table(tmp_path):
    with LSMTable(
        tmp_path / "lsm.sqlite",
        encoder=_encode_file_meta,
        decoder=_decode_file_meta,
    ) as table:
        yield table


def test_missing_root_is_reported(tmp_path, caplog):
    missing = tmp_path / "Документы"

    with caplog.at_level(logging.WARNING, logger="app.scanner"):
        found = list(iter_file_metadata(str(missing)))

    assert found == []
    assert any(str(missing) in r.getMessage() for r in caplog.records), (
        "недоступный корень обязан попасть в лог: без этого неверный путь "
        "в конфиге неотличим от пустого каталога"
    )


def test_existing_root_is_not_reported(tmp_path, caplog):
    (tmp_path / "заметка.txt").write_text("текст", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.scanner"):
        found = list(iter_file_metadata(str(tmp_path)))

    assert len(found) == 1
    assert not caplog.records


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 0o000 не работает для root — тест бессмысленен")
def test_unreadable_subdir_stays_silent(tmp_path, caplog):
    """Подкаталог без прав — норма, о нём шуметь не надо."""
    (tmp_path / "заметка.txt").write_text("текст", encoding="utf-8")
    closed = tmp_path / "closed"
    closed.mkdir()
    (closed / "секрет.txt").write_text("текст", encoding="utf-8")
    closed.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="app.scanner"):
            found = list(iter_file_metadata(str(tmp_path)))
    finally:
        closed.chmod(0o755)

    assert {Path(f.path).name for f in found} == {"заметка.txt"}
    assert not caplog.records


@pytest.mark.asyncio
async def test_scanner_logs_file_count_per_path(
    tmp_path, monkeypatch, caplog, lsm_table
):
    """Ноль файлов по пути виден в логе — иначе путь без документов и
    неверный путь выглядят одинаково."""
    populated = tmp_path / "populated"
    populated.mkdir()
    (populated / "doc.txt").write_text("документ", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()

    monkeypatch.setattr(
        Config, "scan_paths", property(lambda self: [str(populated), str(empty)])
    )

    async with FilesScanner(
        lsm_table, queue_path=tmp_path / "queue", reset=True
    ) as scanner:
        with caplog.at_level(logging.INFO, logger="app.file_scan"):
            found = [meta async for meta in scanner.preindex()]

    assert len(found) == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any(str(populated) in m and "1" in m for m in messages)
    assert any(str(empty) in m and "0" in m for m in messages)
