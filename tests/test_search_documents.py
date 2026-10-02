"""Выдача поиска — документы, а не чанки.

limit в /search — число ДОКУМЕНТОВ на источник. Индексатор берёт из таблицы
пул чанков на канал (`search_pool`), SearchResponse собирает из него
документы: порядок — по лучшей строке, до трёх фрагментов на документ,
карточка документа — не фрагмент, а отметка "card" в `match`.

Раньше limit резал чанки: 10 чанков давали в среднем 6,2 документа, потому
что длинный документ занимал места соседей.
"""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pyarrow as pa
import pytest

from src.common import CARD_IDX, CARD_KIND
from src.common.doc_meta import doc_meta_schema
from src.indexer.indexer import (
    SEARCH_POOL_MAX,
    SEARCH_POOL_MIN,
    VECTOR_NPROBES,
    Index,
    search_pool,
)
from src.server.model import MAX_FRAGMENTS_PER_DOC, SearchResponse
from tests.test_indexing_live import index, isolated_config  # noqa: F401


def _frame(rows: list[tuple]) -> pl.DataFrame:
    """(путь, оценка[, kind[, sha256]]) → кадр выдачи гибрида.

    Строки нарочно не по порядку оценки там, где это важно тесту: LanceDB
    отдаёт их отсортированными, но SearchResponse на это не полагается.
    """
    full = [(*r, None, None)[:4] for r in rows]
    return pl.DataFrame(
        {
            "idx": [
                CARD_IDX if k == CARD_KIND else i for i, (_, _, k, _) in enumerate(full)
            ],
            "text": [f"фрагмент {i}" for i in range(len(full))],
            "kind": [k or "text" for _, _, k, _ in full],
            "meta": [
                {"original_name": Path(p).name, "sha256": sha} for p, _, _, sha in full
            ],
            "path": [p for p, _, _, _ in full],
            "_relevance_score": [float(s) for _, s, _, _ in full],
        }
    )


# --------------------------------------------------------- сборка выдачи ----


def test_limit_counts_documents_even_if_pool_starts_with_one_document():
    """Первые 30 строк пула — один документ, а документов всё равно 10."""
    rows = [("/docs/длинный.pdf", 1.0 - i * 0.001) for i in range(30)]
    rows += [(f"/docs/д{i:02}.txt", 0.5 - i * 0.01) for i in range(15)]

    response = SearchResponse.from_dataframes([_frame(rows)], limit=10)

    paths = [i.absolute_path for i in response.results]
    assert paths == ["/docs/длинный.pdf", *(f"/docs/д{i:02}.txt" for i in range(9))]
    assert response.total == 10


def test_document_keeps_at_most_three_best_fragments():
    rows = [("/docs/a.pdf", s) for s in (0.3, 0.9, 0.5, 0.7, 0.8)]
    rows.append(("/docs/b.pdf", 0.6))

    items = SearchResponse.from_dataframes([_frame(rows)], limit=10).results

    assert MAX_FRAGMENTS_PER_DOC == 3
    assert [c.score for c in items[0].chunks] == pytest.approx([0.9, 0.8, 0.7])
    assert items[0].score == pytest.approx(0.9)
    assert [i.absolute_path for i in items] == ["/docs/a.pdf", "/docs/b.pdf"]


def test_card_is_a_match_not_a_fragment():
    """Карточка поднимает документ и отмечает его, но в фрагменты не идёт."""
    rows = [
        ("/docs/a.pdf", 0.6),
        ("/docs/b.pdf", 0.9, CARD_KIND),  # имя/свойства совпали с запросом
        ("/docs/b.pdf", 0.4),
        ("/docs/c.pdf", 0.5),
    ]

    items = SearchResponse.from_dataframes([_frame(rows)], limit=10).results

    assert [i.absolute_path for i in items] == [
        "/docs/b.pdf",
        "/docs/a.pdf",
        "/docs/c.pdf",
    ]
    b = items[0]
    assert b.match == ["card"]
    assert b.score == pytest.approx(0.9), (
        "оценка документа — лучшая строка, карточка тоже"
    )
    assert [c.score for c in b.chunks] == pytest.approx([0.4])
    assert all(c.kind != CARD_KIND for i in items for c in i.chunks)
    assert items[1].match == [] and items[2].match == []


def test_document_found_only_by_card_has_title_and_no_fragments():
    items = SearchResponse.from_dataframes(
        [_frame([("/docs/ЦБ_21.pdf", 0.9, CARD_KIND), ("/docs/a.pdf", 0.5)])],
        limit=10,
    ).results

    assert items[0].absolute_path == "/docs/ЦБ_21.pdf"
    assert items[0].title == "ЦБ_21.pdf"
    assert items[0].chunks == [] and items[0].match == ["card"]


def test_card_does_not_take_a_slot_of_its_own():
    """Карточка и чанки одного документа — одно место в limit."""
    rows = [
        ("/docs/a.pdf", 0.9, CARD_KIND),
        ("/docs/a.pdf", 0.8),
        ("/docs/b.pdf", 0.7),
        ("/docs/c.pdf", 0.6),
    ]

    items = SearchResponse.from_dataframes([_frame(rows)], limit=2).results

    assert [i.absolute_path for i in items] == ["/docs/a.pdf", "/docs/b.pdf"]


def test_rows_beyond_limit_still_complete_selected_documents():
    """Хвост пула добирает фрагменты и карточки уже отобранных документов."""
    rows = [
        ("/docs/a.pdf", 0.9),
        ("/docs/b.pdf", 0.8),
        ("/docs/c.pdf", 0.7),  # за пределами limit=2
        ("/docs/a.pdf", 0.6),
        ("/docs/b.pdf", 0.5, CARD_KIND),
    ]

    items = SearchResponse.from_dataframes([_frame(rows)], limit=2).results

    assert [i.absolute_path for i in items] == ["/docs/a.pdf", "/docs/b.pdf"]
    assert len(items[0].chunks) == 2
    assert items[1].match == ["card"]


def test_copies_collapse_into_one_document_with_card():
    """Копии (тот же sha256) — один документ; их карточки и чанки мест не занимают."""
    rows = [
        ("/x/отчёт.pdf", 0.9, CARD_KIND, "1"),
        ("/y/отчёт.pdf", 0.9, CARD_KIND, "1"),
        ("/y/отчёт.pdf", 0.8, "text", "1"),
        ("/x/отчёт.pdf", 0.8, "text", "1"),
        ("/z/договор.pdf", 0.7, "text", "2"),
        ("/w/план.pdf", 0.6, "text", "3"),
    ]

    response = SearchResponse.from_dataframes([_frame(rows)], limit=2)

    assert [(i.absolute_path, i.duplicates, i.match) for i in response.results] == [
        ("/x/отчёт.pdf", ["/y/отчёт.pdf"], ["card"]),
        ("/z/договор.pdf", [], []),
    ]
    assert len(response.results[0].chunks) == 1
    assert response.total == 2


def test_total_is_number_of_returned_documents():
    rows = [(f"/docs/{i}.txt", 1.0 - i / 100) for i in range(7) for _ in range(4)]

    assert SearchResponse.from_dataframes([_frame(rows)], limit=5).total == 5
    assert SearchResponse.from_dataframes([_frame(rows)]).total == 7  # без limit — все
    assert SearchResponse.from_dataframes([None], limit=5).total == 0


def test_each_source_gets_its_own_document_limit():
    files = _frame([(f"/docs/{i}.txt", 0.9 - i / 100) for i in range(12)])
    emails = _frame([(f"email_INBOX:{i}:{i}:body", 0.95 - i / 100) for i in range(12)])

    response = SearchResponse.from_dataframes([files, emails], limit=10)

    types = [i.type for i in response.results]
    assert types.count("file") == 10 and types.count("email") == 10
    assert response.total == 20
    scores = [i.score for i in response.results]
    assert scores == sorted(scores, reverse=True)


# ------------------------------------------------------- запросы к LanceDB ----


def test_pool_follows_limit_with_floor_and_ceiling():
    assert search_pool(1) == SEARCH_POOL_MIN == 50
    assert search_pool(10) == 50  # лучший пул по замеру на стенде
    assert search_pool(20) == 100
    assert search_pool(1000) == SEARCH_POOL_MAX == 500


class _Builder:
    """Билдер запроса LanceDB: записывает цепочку вызовов, отдаёт готовый кадр."""

    def __init__(self, frame, error=None):
        self.calls: list[tuple] = []
        self._frame = frame
        self._error = error

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append((name, args))
            return self

        return record

    def to_polars(self):
        if self._error is not None:
            raise self._error
        return self._frame


class _Table:
    """Таблица, которая запоминает каждый запрос: аргументы search и цепочку."""

    def __init__(self, frame, hybrid_error=None):
        self.frame = frame
        self.hybrid_error = hybrid_error
        self.searches: list[tuple[dict, _Builder]] = []

    def search(self, *args, **kwargs):
        error = self.hybrid_error if kwargs.get("query_type") == "hybrid" else None
        builder = _Builder(self.frame, error)
        self.searches.append((kwargs, builder))
        return builder


def _index(files_table, email_table=None) -> SimpleNamespace:
    async def async_encode(texts, role=None):
        return [[0.0] * 8]

    def store(table, name):
        return SimpleNamespace(table=table, has_table=True, table_name=name)

    return SimpleNamespace(
        cfg=SimpleNamespace(ov_model=SimpleNamespace(async_encode=async_encode)),
        files_store=store(files_table, "files"),
        email_store=store(email_table or files_table, "files_email"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["hybrid", "vector", "text"])
@pytest.mark.parametrize("limit", [1, 10, 37])
async def test_every_query_type_asks_pool_once(query_type, limit):
    """Один запрос на источник, пул по limit; копии повторного запроса не вызывают."""
    copies = _frame([("/x/a.txt", 0.9, "text", "1"), ("/y/a.txt", 0.8, "text", "1")])
    table = _Table(copies)

    frames = await Index.search(
        _index(table), "договор", top_k=limit, query_type=query_type
    )

    assert frames == [copies]
    assert len(table.searches) == 1, "копии больше не требуют запроса с запасом"
    kwargs, builder = table.searches[0]
    assert ("limit", (search_pool(limit),)) in builder.calls
    if query_type == "hybrid":
        assert kwargs["fts_columns"] == "text"
        assert ("nprobes", (VECTOR_NPROBES,)) in builder.calls
    elif query_type == "vector":
        assert ("nprobes", (VECTOR_NPROBES,)) in builder.calls
    else:
        assert kwargs["fts_columns"] == "text"


@pytest.mark.asyncio
async def test_hybrid_fallback_to_vector_uses_the_same_pool():
    table = _Table(_frame([("/docs/a.txt", 0.9)]), RuntimeError("no inverted index"))

    await Index.search(_index(table), "договор", top_k=30)

    (hybrid_kwargs, _), (vector_kwargs, vector) = table.searches
    assert hybrid_kwargs["query_type"] == "hybrid"
    assert vector_kwargs["query_type"] == "vector"
    assert ("limit", (search_pool(30),)) in vector.calls
    assert ("nprobes", (VECTOR_NPROBES,)) in vector.calls


@pytest.mark.asyncio
async def test_email_source_gets_its_own_pool():
    files, emails = _Table(_frame([("/docs/a.txt", 0.9)])), _Table(None)

    frames = await Index.search(
        _index(files, emails), "совещание", top_k=20, sources=["files", "email"]
    )

    assert len(frames) == 2
    for table in (files, emails):
        ((kwargs, builder),) = table.searches
        assert kwargs["fts_columns"] == "text"
        assert ("limit", (search_pool(20),)) in builder.calls


# ------------------------------------------------- живой индекс (LanceDB) ----


def _rows_table(rows: list[tuple[str, str, str]], embed) -> pa.Table:
    """(путь, kind, текст) → строки таблицы файлов с векторами модели."""
    vecs = np.asarray(embed([t for _, _, t in rows]), dtype=np.float32)
    idx, seen = [], {}
    for path, kind, _ in rows:
        seen[path] = seen.get(path, -1) + (kind != CARD_KIND)
        idx.append(CARD_IDX if kind == CARD_KIND else seen[path])
    return pa.table(
        {
            "idx": pa.array(idx, pa.int64()),
            "text": pa.array([t for _, _, t in rows], pa.string()),
            "kind": pa.array([k for _, k, _ in rows], pa.string()),
            "meta": pa.array(
                [{"original_name": Path(p).name, "sha256": p} for p, _, _ in rows],
                pa.struct(doc_meta_schema),
            ),
            "path": pa.array([p for p, _, _ in rows], pa.string()),
            "vector": pa.FixedSizeListArray.from_arrays(vecs.ravel(), vecs.shape[1]),
        }
    )


@pytest.mark.asyncio
async def test_live_search_returns_documents_and_card_match(index: Index):  # noqa: F811
    """Настоящая таблица: длинный документ не вытесняет соседей, карточка — match."""
    rows = [
        ("/docs/кредит.txt", "text", f"Кредитный риск банка, раздел {i}")
        for i in range(20)
    ]
    rows += [
        ("/docs/резервы.txt", "text", "Резервы банка на возможные потери по ссудам"),
        ("/docs/капитал.txt", "text", "Достаточность капитала банка и нормативы"),
        ("/docs/положение.txt", "text", "Порядок расчёта процентного риска банка"),
        ("/docs/положение.txt", CARD_KIND, "Документ: положение\nАвтор: Центробанк"),
    ]
    store = index.cfg.lance.files
    await store.add_batch(_rows_table(rows, index.cfg.ov_model.encode))
    store.create_fts(force=True)

    items = SearchResponse.from_dataframes(
        await index.search("кредитный риск банка", top_k=4, query_type="vector"),
        limit=4,
    ).results
    assert len(items) == 4, "20 чанков одного документа не съедают выдачу"
    assert all(len(i.chunks) <= MAX_FRAGMENTS_PER_DOC for i in items)
    assert all(c.kind != CARD_KIND for i in items for c in i.chunks)

    (item,) = SearchResponse.from_dataframes(
        await index.search("Центробанк", top_k=4, query_type="text"), limit=4
    ).results
    assert (item.absolute_path, item.match, item.chunks) == (
        "/docs/положение.txt",
        ["card"],
        [],
    )

    items = SearchResponse.from_dataframes(
        await index.search("Центробанк", top_k=4, query_type="hybrid"), limit=4
    ).results
    found = {i.absolute_path: i for i in items}
    assert found["/docs/положение.txt"].match == ["card"]
    assert len(items) == 4
