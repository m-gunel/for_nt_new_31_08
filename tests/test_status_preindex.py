"""/status во время сбора очереди и в простое."""

import pytest
from pytest import fixture

from src.config import Config
from src.indexer.indexer import Index


@fixture
def status_config(config: Config, tmp_path, monkeypatch):
    """Хранилище и user_conf.yml — во временном каталоге, в папке один файл."""
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "файл.txt").write_text("содержимое файла", encoding="utf-8")
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(docs)])
    config.__dict__.pop("lance", None)
    yield config
    config.__dict__.pop("lance", None)
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


@pytest.mark.asyncio
async def test_status_counts_queued_files(status_config):
    """После сбора очереди /status показывает, сколько файлов ждёт индексации."""
    index = Index(status_config)
    async with index:
        await index._preindex_files()
        await index.count_to_index()

        status = await index.status()

    assert status.queued_files == 1
    assert status.files_to_index == 1


@pytest.mark.asyncio
async def test_status_is_indexing_during_preindex(status_config):
    """Сбор очереди — это работа, а не простой.

    Обход большого дерева папок идёт минутами при нулевом counter — без этого
    клиент считал бы демон свободным.
    """
    index = Index(status_config)
    async with index:
        index.preindex = True

        status = await index.status()

    assert status.type == "preindex"
    assert status.is_indexing is True


@pytest.mark.asyncio
async def test_status_queue_is_empty_when_idle(status_config):
    index = Index(status_config)
    async with index:
        status = await index.status()

    assert status.files_to_index == 0
    assert status.queued_files == 0
