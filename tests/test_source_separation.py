"""Файлы и письма живут в разных таблицах, и поиск по умолчанию — по файлам.

В общей таблице один `.limit(top_k)` отдавал бы письмам места документов, а
вектор-индекс обучался бы на смешанном корпусе.
"""

import numpy as np
import pyarrow as pa
import pytest
import pytest_asyncio as pa_fixture
from pytest import fixture

from src.common.doc_meta import doc_meta_schema
from src.config import Config, EmailConfig
from src.indexer.indexer import EMAIL_ROWS_PREDICATE, Index
from src.server.model import SearchRequest, SearchResponse
from tests.mbox_fixture import default_inbox_messages, make_profile


@fixture
def mail_root(tmp_path):
    return make_profile(tmp_path / "tb-profile", {"INBOX": default_inbox_messages()})


@fixture
def docs_dir(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "отчёт.txt").write_text(
        "Квартальный отчёт по продажам. Выручка выросла на 12 процентов.",
        encoding="utf-8",
    )
    return docs


@fixture
def email_config(config: Config, mail_root, docs_dir, tmp_path, monkeypatch):
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
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(docs_dir)])
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


def _paths(store) -> set[str]:
    if not store.has_table:
        return set()
    return {row["path"] for row in store.table.search().limit(1000).to_list()}


@pytest.mark.asyncio
async def test_emails_and_files_land_in_separate_tables(index: Index):
    await index.scan_all()

    files = _paths(index.files_store)
    emails = _paths(index.email_store)

    assert files, "документ должен попасть в таблицу файлов"
    assert emails, "письма должны попасть в почтовую таблицу"
    assert not any(p.startswith("email_") for p in files), (
        "письма в таблице файлов вытесняют документы из выдачи поиска"
    )
    assert all(p.startswith("email_") for p in emails)
    assert any(p.endswith("отчёт.txt") for p in files)


@pytest.mark.asyncio
async def test_files_are_indexed_before_emails(index: Index, monkeypatch):
    """Обход ящика идёт минутами и не должен задерживать документы."""
    order: list[str] = []
    real_process = index.process_stream

    async def spy(batch, count=True, store=None):
        order.append((store or index.files_store).table_name)
        return await real_process(batch, count=count, store=store)

    monkeypatch.setattr(index, "process_stream", spy)
    await index.scan_all()

    assert order[0] == index.files_store.table_name, (
        "файлы обязаны индексироваться первыми"
    )


def test_search_request_defaults_to_files():
    assert SearchRequest(query="отчёт").sources == ["files"]


@pytest.mark.parametrize("sources", [[], ["files", "files"]])
def test_search_request_rejects_degenerate_sources(sources):
    with pytest.raises(ValueError):
        SearchRequest(query="отчёт", sources=sources)


@pytest.mark.asyncio
async def test_default_search_returns_no_emails(index: Index):
    await index.scan_all()

    frames = await index.search("совещание", top_k=10)
    response = SearchResponse.from_dataframes(frames)

    assert response.results, "по файлам должно что-то находиться"
    assert {r.type for r in response.results} == {"file"}


@pytest.mark.asyncio
async def test_email_source_returns_emails(index: Index):
    await index.scan_all()

    frames = await index.search("совещание", top_k=10, sources=["email"])
    response = SearchResponse.from_dataframes(frames)

    assert response.results
    assert {r.type for r in response.results} <= {"email", "email_attachment"}


@pytest.mark.asyncio
async def test_both_sources_do_not_share_the_limit(index: Index):
    """Каждый источник получает свой top_k — письма не съедают места файлов."""
    await index.scan_all()

    files_only = SearchResponse.from_dataframes(
        await index.search("совещание", top_k=10, sources=["files"])
    )
    both = SearchResponse.from_dataframes(
        await index.search("совещание", top_k=10, sources=["files", "email"])
    )

    files_in_both = {r.id for r in both.results if r.type == "file"}
    assert {r.id for r in files_only.results} == files_in_both
    assert any(r.type != "file" for r in both.results)


@pytest.mark.asyncio
async def test_search_does_not_create_missing_email_table(index: Index):
    """Поиск не должен писать в хранилище: обращение к .table создаёт таблицу."""
    assert not index.email_store.has_table

    frames = await index.search("совещание", top_k=5, sources=["email"])

    assert frames == []
    assert not index.email_store.has_table
    assert SearchResponse.from_dataframes(frames).results == []


def _mixed_rows(dim: int) -> pa.Table:
    """Индекс, каким он был до разделения: письма вперемешку с файлами."""
    paths = [
        "/home/u/отчёт.txt",
        "/home/u/договор.pdf",
        "email_INBOX:0:100:body",
        "email_INBOX:100:200:body",
        "emailX_не_письмо.txt",
    ]
    rng = np.random.default_rng(seed=7)
    vectors = rng.standard_normal((len(paths), dim)).astype(np.float32)
    return pa.table(
        {
            "idx": pa.array(range(len(paths)), pa.int64()),
            "text": pa.array([f"текст {p}" for p in paths]),
            "meta": pa.array([None] * len(paths), pa.struct(doc_meta_schema)),
            "path": pa.array(paths),
            "vector": pa.FixedSizeListArray.from_arrays(vectors.reshape(-1), dim),
        }
    )


@pytest.mark.asyncio
async def test_migration_moves_emails_and_keeps_files(index: Index):
    rows = _mixed_rows(index.cfg.embed_dim)
    await index.files_store.add_batch(rows)

    moved = await index.migrate_emails_out_of_files_table()

    assert moved == 2
    assert _paths(index.files_store) == {
        "/home/u/отчёт.txt",
        "/home/u/договор.pdf",
        # LIKE 'email_%' снёс бы и это: в LIKE `_` — «любой символ».
        "emailX_не_письмо.txt",
    }
    assert _paths(index.email_store) == {
        "email_INBOX:0:100:body",
        "email_INBOX:100:200:body",
    }


@pytest.mark.asyncio
async def test_migration_preserves_vectors(index: Index):
    """Переносим, а не пересчитываем: повторный инференс по ящику дорог."""
    rows = _mixed_rows(index.cfg.embed_dim)
    await index.files_store.add_batch(rows)
    before = {
        r["path"]: r["vector"]
        for r in index.files_store.table.search().limit(100).to_list()
        if r["path"].startswith("email_INBOX")
    }

    await index.migrate_emails_out_of_files_table()

    after = {
        r["path"]: r["vector"]
        for r in index.email_store.table.search().limit(100).to_list()
    }
    assert after.keys() == before.keys()
    for path, vector in before.items():
        assert np.allclose(vector, after[path]), f"вектор {path} пересчитан заново"


@pytest.mark.asyncio
async def test_migration_is_idempotent(index: Index):
    await index.files_store.add_batch(_mixed_rows(index.cfg.embed_dim))
    assert await index.migrate_emails_out_of_files_table() == 2

    assert await index.migrate_emails_out_of_files_table() == 0, (
        "признак «мигрировать нечего» — отсутствие почтовых строк в таблице файлов"
    )
    assert len(_paths(index.email_store)) == 2, "повтор не должен плодить дубли"


@pytest.mark.asyncio
async def test_migration_leaves_no_email_rows_behind(index: Index):
    await index.files_store.add_batch(_mixed_rows(index.cfg.embed_dim))

    await index.migrate_emails_out_of_files_table()

    assert index.files_store.count_matching(EMAIL_ROWS_PREDICATE) == 0


@pytest.mark.asyncio
async def test_second_pass_is_skipped_while_one_is_running(index: Index):
    """Два потребителя очереди заклинивают друг друга — проход должен быть один.

    `iter(endless=False)` проверяет пустоту до блокирующего `get()`, поэтому
    опоздавший ждёт элемент вечно.
    """
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()
    passes = 0
    real_run = index._run_pass

    async def slow_pass(collect, scanner, store):
        nonlocal passes
        passes += 1
        started.set()
        await release.wait()
        return await real_run(collect, scanner, store)

    index._run_pass = slow_pass  # type: ignore[method-assign]
    first = asyncio.create_task(index.scan_all())
    await asyncio.wait_for(started.wait(), 5)

    await index.scan_all(emails_only=True)  # должен быть пропущен

    assert passes == 1, "второй проход обязан быть пропущен, а не запущен"
    release.set()
    await asyncio.wait_for(first, 30)


@pytest.mark.asyncio
async def test_pass_lock_is_released_after_failure(index: Index, monkeypatch):
    """Упавший проход не должен запирать индексацию до рестарта демона."""

    async def boom(*args, **kwargs):
        raise RuntimeError("сбой прохода")

    monkeypatch.setattr(index, "_run_pass", boom)
    with pytest.raises(RuntimeError):
        await index.scan_all()

    assert not index._scan_lock.locked()


@pytest.mark.asyncio
async def test_file_pipeline_does_not_open_mailboxes(index: Index, monkeypatch):
    """Сохранение одного файла не должно тянуть за собой обход mbox.

    При включённой почте `async_parse_files` открывает все mbox и заводит пул
    потоков на каждый вызов, а зовут её и на каждую пачку от watcher'а.
    """
    import src.indexer.indexer as indexer_module

    calls: list[bool] = []
    real = indexer_module.async_parse_files

    def spy(files, fail_cb, with_emails=True):
        calls.append(with_emails)
        return real(files, fail_cb=fail_cb, with_emails=with_emails)

    monkeypatch.setattr(indexer_module, "async_parse_files", spy)
    await index.scan_all()

    assert calls == [False, True], (
        "файловый проход обязан идти без открытия mbox, почтовый — с ним"
    )
