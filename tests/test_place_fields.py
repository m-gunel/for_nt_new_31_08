"""Поля места фрагмента и свойства документа: схема таблицы → /search → MCP.

Новые поля необязательные: строка старой схемы и старый демон для моста
выглядят как «места нет», а не как ошибка.
"""

from datetime import datetime
from pathlib import Path

import polars as pl
import pyarrow as pa
import pytest

from src.common import chunk_with_meta_schema
from src.parsers import file_chunks
from src.server.model import SearchResponse
from src.utils.batch_embeddings import _process_batch
from src.utils.lance_wrapper import LanceWrapper
from tests.mcp_fixture import bridge_factory, call_text, stub  # noqa: F401

SEARCH = "/api/v1/search"
STATUS = "/api/v1/status"


def _fake_embedder(texts):
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def test_rows_of_a_file_go_into_lancedb_with_place_and_meta(tmp_path):
    doc = tmp_path / "заметка.txt"
    doc.write_text(
        "Первая строка заметки о проверке\n\nВторая строка с итогами проверки",
        encoding="utf-8",
    )
    rows = list(file_chunks(doc))
    store = LanceWrapper(str(tmp_path / "lance"), embed_dim=4)
    store.add_sync(_process_batch(rows, _fake_embedder))
    table = store.table.to_arrow()
    assert table.schema.field("page_start").type == pa.int64()
    assert table.schema.field("meta").type.get_field_index("sha256") >= 0
    got = table.to_pylist()[0]
    assert (got["line_start"], got["line_end"], got["kind"]) == (1, 3, "text")
    assert got["meta"]["title"] == "Первая строка заметки о проверке"
    assert (
        got["meta"]["mime"] == "text/plain"
        and got["meta"]["size"] == doc.stat().st_size
    )


def _frame(rows: list[dict]) -> pl.DataFrame:
    table = pa.RecordBatch.from_pylist(rows, schema=chunk_with_meta_schema)
    frame = pl.from_arrow(pa.Table.from_batches([table]))
    return frame.with_columns(pl.Series("_relevance_score", [0.5] * len(rows)))  # type: ignore[union-attr]


def test_search_response_has_place_and_document_and_no_prefix():
    meta = {
        "original_name": "отчёт.pdf",
        "title": "Годовой отчёт",
        "author": "Иванов",
        "page_count": 12,
        "date_modified": datetime(2025, 3, 1, 10, 0),
        "sha256": "ab" * 32,
    }
    frame = _frame(
        [
            {
                "idx": 0,
                "text": "Документ: отчёт\nТекст фрагмента",
                "kind": "text",
                "page_start": 3,
                "page_end": 4,
                "section": "Глава 2 › 2.1",
                "meta": meta,
                "path": "/docs/отчёт.pdf",
            },
        ]
    )
    item = SearchResponse.from_dataframes([frame]).results[0]
    chunk = item.chunks[0]
    assert (
        chunk.content == "Текст фрагмента"
    )  # строка-префикс — для индекса, не для читателя
    assert (chunk.page_start, chunk.page_end, chunk.section, chunk.kind) == (
        3,
        4,
        "Глава 2 › 2.1",
        "text",
    )
    assert item.document is not None
    assert (item.document.title, item.document.author, item.document.page_count) == (
        "Годовой отчёт",
        "Иванов",
        12,
    )
    dumped = item.model_dump(mode="json")
    assert (
        dumped["chunks"][0]["page_start"] == 3
        and dumped["document"]["sha256"] == "ab" * 32
    )


def test_old_rows_have_no_place_and_keep_text():
    # Не префикс этого файла — текст не трогаем.
    frame = _frame(
        [
            {
                "idx": 0,
                "text": "Документ: другой файл\nтекст",
                "meta": {"original_name": "a.txt"},
                "path": "/docs/a.txt",
            }
        ]
    )
    item = SearchResponse.from_dataframes([frame]).results[0]
    assert item.chunks[0].content == "Документ: другой файл\nтекст"
    assert item.chunks[0].page_start is None and item.chunks[0].section is None


def _hit(path, chunk, document=None):
    hit = {
        "id": path,
        "title": Path(path).name,
        "type": "file",
        "score": 0.8,
        "absolute_path": path,
        "chunks": [{"content": "текст фрагмента", "score": 0.8, **chunk}],
    }
    if document is not None:
        hit["document"] = document
    return hit


@pytest.fixture
def bridge(stub, bridge_factory):  # noqa: F811
    return bridge_factory(stub.url)


def test_bridge_shows_place_and_document(stub, bridge):  # noqa: F811
    stub.set("GET", STATUS, {"is_indexing": False, "type": "idle", "files_to_index": 0})
    stub.set(
        "POST",
        SEARCH,
        {
            "results": [
                _hit(
                    "/docs/акт.pdf",
                    {"page_start": 3, "page_end": 4, "section": "Глава 2 › 2.1"},
                    {
                        "title": "Положение о резервах",
                        "author": "Иванов",
                        "last_modified_by": "Петров",
                        "date_modified": "2025-03-01T10:00:00",
                        "page_count": 12,
                        "sha256": None,
                    },
                ),
                _hit("/docs/план.pptx", {"page_start": 2, "page_end": 2}),
                _hit("/docs/скан.pdf", {"kind": "ocr", "page_start": 7, "page_end": 7}),
                _hit("/docs/заметка.txt", {"line_start": 10, "line_end": 12}),
                _hit("/docs/старый.docx", {}),
            ],
            "total": 5,
        },
    )
    response = bridge.call("local_search", {"query": "резервы"})
    text = call_text(response)
    assert "· [стр. 3–4 · Глава 2 › 2.1] текст фрагмента" in text
    assert (
        "«Положение о резервах»; автор: Иванов; изменил: Петров; изменён: 2025-03-01; объём: 12"
        in text
    )
    assert "· [слайд 2] текст фрагмента" in text
    assert "· [стр. 7 · распознано OCR] текст фрагмента" in text
    assert "· [строки 10–12] текст фрагмента" in text
    assert "· текст фрагмента" in text  # у старого демона места нет — без скобок
    results = response["result"]["structuredContent"]["results"]
    assert (
        results[0]["chunks"][0]["page_start"] == 3
        and results[0]["chunks"][0]["section"] == "Глава 2 › 2.1"
    )
    assert results[0]["document"] == {
        "title": "Положение о резервах",
        "author": "Иванов",
        "last_modified_by": "Петров",
        "date_modified": "2025-03-01T10:00:00",
        "page_count": 12,
    }
    assert "document" not in results[4]
