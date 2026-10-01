import os
from asyncio import timeout
from pathlib import Path

import pytest
import pytest_asyncio as pa
from pytest import fixture

from src.config import Config
from src.indexer.indexer import EmailsScanner, FilesScanner, list_email_folders
from src.thunderbird.find_files import find_email_root
from src.utils.lsm_adapter import _decode_file_meta, _encode_file_meta
from tests.test_lsm import LSMTable

root = Path(__file__).parent / "data"


@fixture
def lsm_table():
    # Каталог рабочий, а не с фикстурами, и правило `data` в .gitignore матчит
    # его на любом уровне — поэтому в свежем клоне его нет. LSM родительский
    # каталог не создаёт, и без этой строки фикстура падает на «unable to open
    # database file» ещё до первой проверки.
    root.mkdir(parents=True, exist_ok=True)
    with LSMTable(
        root / "lsm.sqlite",
        encoder=_encode_file_meta,
        decoder=_decode_file_meta,
    ) as lsm_table:
        try:
            yield lsm_table
        finally:
            os.remove(root / "lsm.sqlite")


@pa.fixture
async def files_scanner(lsm_table: LSMTable, tmp_path, monkeypatch):
    # Сканируем собственный каталог, а не cfg.scan_paths: тот берётся из
    # ~/.ragsearch/user_conf.yml и зависит от машины.
    for i in range(3):
        (tmp_path / f"doc_{i}.txt").write_text(f"документ {i}", encoding="utf-8")
    monkeypatch.setattr(Config, "scan_paths", property(lambda self: [str(tmp_path)]))
    async with FilesScanner(
        lsm_table,
        queue_path=root / "files_queue",
        reset=True,
    ) as scanner:
        yield scanner


@pa.fixture
async def emails_scanner(lsm_table: LSMTable, config: Config):
    email_root = config.email_root
    if email_root is None:
        pytest.skip(
            "нет профиля Thunderbird с mbox — почтовому сканеру не на чем работать"
        ) # type: ignore
    async with EmailsScanner(
        email_path=email_root,
        folders=list_email_folders(email_root),
        lsm_checker=lsm_table,
        queue_path=root / "emails_queue",
        reset=True,
    ) as scanner:
        yield scanner


@pytest.mark.asyncio
async def test_files_scanner(files_scanner: FilesScanner):
    await files_scanner.scan()
    assert await files_scanner.total() > 0
    to_count = await files_scanner.total()
    cnt = 0
    async with timeout(5):
        async for meta in files_scanner.iter(endless=False):
            cnt += 1
    assert await files_scanner.total() == 0
    assert cnt == to_count


@pytest.mark.asyncio
async def test_clear_during_iteration(files_scanner: FilesScanner):
    """clear() на ходу не ломает уже начатый обход очереди.

    POST /config зовёт clear() ровно тогда, когда проход может быть в
    середине. Раньше очередь пересоздавалась под тем же путём, а обход
    продолжал звать task_done() — но уже у НОВОГО объекта очереди, где
    незавершённых задач нет: persistqueue отвечает на это «task_done()
    called too many times».
    """
    await files_scanner.scan()
    assert await files_scanner.total() > 0

    seen = 0
    async with timeout(10):
        async for _meta in files_scanner.iter(endless=False):
            seen += 1
            if seen == 1:
                await files_scanner.clear()

    assert seen == 1, "после подмены очереди обход должен прекратиться"
    assert await files_scanner.total() == 0
    # Каталог очереди пересоздан и пригоден к работе.
    assert files_scanner.queu_path.is_dir()
    await files_scanner.scan()
    assert await files_scanner.total() > 0


@pytest.mark.asyncio
async def test_clear_removes_retired_queue_dir(files_scanner: FilesScanner):
    """Старый каталог очереди уводится в сторону и удаляется, не накапливаясь."""
    await files_scanner.scan()
    parent = files_scanner.queu_path.parent
    await files_scanner.clear()
    retired = list(parent.glob(f"{files_scanner.queu_path.name}.retired.*"))
    assert retired == [], f"остались временные каталоги очереди: {retired}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_emails_scanner(emails_scanner: EmailsScanner):
    await emails_scanner.scan()
