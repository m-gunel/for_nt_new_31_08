"""Контракт поиска: что ищется по умолчанию и что переживает выдача.

Колонка `meta` в схеме чанка nullable (src/common/doc_meta.py), и строка с
`meta=None` не должна ронять весь POST /search в 500.
"""

from typing import Any

import polars as pl
import pyarrow as pa
import pytest

from src.common.doc_meta import doc_meta_schema
from src.indexer.indexer import Index
from src.server.model import SearchRequest, SearchResponse

# Index на временном хранилище.
from tests.test_indexing_live import index, isolated_config  # noqa: F401

_FILE_PATH = "/home/user/Документы/Договор поставки.pdf"


def test_search_request_defaults_to_files():
    """По умолчанию поиск идёт по файлам."""
    assert SearchRequest(query="отчёт").sources == ["files"]


@pytest.mark.parametrize("sources", [[], ["files", "files"]])
def test_search_request_rejects_degenerate_sources(sources):
    with pytest.raises(ValueError):
        SearchRequest(query="отчёт", sources=sources)


@pytest.mark.asyncio
async def test_search_does_not_create_missing_table(index: Index):  # noqa: F811
    """Поиск не должен писать в хранилище: обращение к .table создаёт таблицу."""
    assert not index.files_store.has_table

    frame = await index.search("совещание", top_k=5)

    assert frame is None
    assert not index.files_store.has_table
    assert SearchResponse.from_dataframe(frame).results == []


def _frame_with_null_struct_meta(path: str, text: str, score: float) -> pl.DataFrame:
    """Кадр в той форме, в какой его отдаёт LanceDB: meta — null-struct
    настоящей `doc_meta_schema`."""
    frame = pl.from_arrow(
        pa.table(
            {
                "idx": pa.array([0], pa.int64()),
                "text": pa.array([text], pa.string()),
                "meta": pa.array([None], pa.struct(doc_meta_schema)),
                "path": pa.array([path], pa.string()),
                "_relevance_score": pa.array([score], pa.float64()),
            }
        )
    )
    # Предпосылка проверяется явно: смена поведения polars должна давать
    # внятное падение, а не молча выродившийся тест.
    row = next(frame.iter_rows(named=True))  # type: ignore[union-attr]
    assert row["meta"] is None or all(v is None for v in row["meta"].values()), (
        "кадр обязан доносить до модели именно отсутствие метаданных"
    )
    return frame  # type: ignore[return-value]


def _frame_with_null_meta_column(path: str, text: str, score: float) -> pl.DataFrame:
    """Тот же случай, но колонка meta собрана polars'ом из чистых None.

    Как polars отдаёт null-struct (`None` или словарь из `None`), зависит от
    версии, а колонка из одних None доезжает до `from_dataframe` как None при
    любой.
    """
    return pl.DataFrame(
        {
            "idx": [0],
            "text": [text],
            "meta": [None],
            "path": [path],
            "_relevance_score": [score],
        }
    )


def _search_frame(rows: list[tuple[str, str, dict[str, Any] | None, float]]):
    """Многострочный кадр выдачи: (path, text, meta, score)."""
    return pl.from_arrow(
        pa.table(
            {
                "idx": pa.array(list(range(len(rows))), pa.int64()),
                "text": pa.array([r[1] for r in rows], pa.string()),
                "meta": pa.array([r[2] for r in rows], pa.struct(doc_meta_schema)),
                "path": pa.array([r[0] for r in rows], pa.string()),
                "_relevance_score": pa.array([r[3] for r in rows], pa.float64()),
            }
        )
    )


@pytest.mark.parametrize(
    "make_frame",
    [_frame_with_null_struct_meta, _frame_with_null_meta_column],
    ids=["null-struct", "null-column"],
)
def test_row_without_meta_does_not_break_the_whole_search(make_frame):
    """Строка с meta=None не роняет весь ответ; титул берётся из имени файла."""
    frame = make_frame(_FILE_PATH, "предмет договора и сроки поставки", 0.73)

    response = SearchResponse.from_dataframe(frame)

    assert response.total == 1
    (item,) = response.results
    assert item.title == "Договор поставки.pdf", (
        "без метаданных титул обязан падать на имя файла из пути"
    )
    assert item.absolute_path == _FILE_PATH
    assert item.type == "file"
    assert [chunk.content for chunk in item.chunks] == [
        "предмет договора и сроки поставки"
    ]
    assert item.score == pytest.approx(0.73)


def test_row_without_meta_does_not_hide_title_of_its_neighbour():
    """Соседний чанк того же файла с метаданными всё ещё даёт титул.

    Строка без meta идёт в кадре первой: титул не должен браться только из неё.
    """
    frame = _search_frame(
        [
            (_FILE_PATH, "второй кусок договора", None, 0.41),
            (
                _FILE_PATH,
                "первый кусок договора",
                {"original_name": "Договор поставки № 17.pdf"},
                0.92,
            ),
        ]
    )

    response = SearchResponse.from_dataframe(frame)

    assert response.total == 1, "оба чанка — один и тот же файл"
    (item,) = response.results
    assert item.title == "Договор поставки № 17.pdf", (
        "титул есть у соседней строки — имя файла тут уже не фолбэк"
    )
    assert len(item.chunks) == 2
    assert item.score == pytest.approx(0.92), "оценка файла — максимум по чанкам"
