"""Копии одного файла в выдаче, пропуск битых файлов, кодировки, регистр."""

import codecs
from pathlib import Path

import numpy as np
import polars as pl
import pyarrow as pa
import pymupdf
import pytest

from src.common import FileMetadata
from src.common.doc_meta import doc_meta_schema
from src.indexer.exclude import check_path
from src.indexer.indexer import Index
from src.parsers.txt import decode_text
from src.server.model import SearchRequest, SearchResponse
from tests.mcp_fixture import bridge_factory, call_text, stub  # noqa: F401
from tests.test_indexing_live import index, isolated_config  # noqa: F401

COPIES = ["/a/отчёт.pdf", "/b/отчёт.pdf", "/c/копия отчёт.pdf"]


def _table(rows: list[tuple[str, str, str]], embed) -> pa.Table:
    """(путь, текст, sha256) → строки таблицы файлов с векторами модели."""
    vecs = np.asarray(embed([t for _, t, _ in rows]), dtype=np.float32)
    meta = [{"original_name": Path(p).name, "sha256": sha} for p, _, sha in rows]
    return pa.table(
        {
            "idx": pa.array(range(len(rows)), pa.int64()),
            "text": pa.array([t for _, t, _ in rows], pa.string()),
            "meta": pa.array(meta, pa.struct(doc_meta_schema)),
            "path": pa.array([p for p, _, _ in rows], pa.string()),
            "vector": pa.FixedSizeListArray.from_arrays(vecs.ravel(), vecs.shape[1]),
        }
    )


@pytest.mark.asyncio
async def test_three_copies_give_one_result_with_two_duplicates(index: Index):  # noqa: F811
    report = [
        "Квартальный отчёт банка о прибыли и капитале",
        "Отчёт банка: резервы и кредитный риск",
    ]
    rows = [(p, t, "a" * 64) for p in COPIES for t in report]
    rows += [
        ("/d/договор.pdf", "Договор аренды помещения для отделения", "b" * 64),
        ("/e/инструкция.pdf", "Инструкция по работе с кассой отделения", "c" * 64),
    ]
    store = index.cfg.lance.files
    await store.add_batch(_table(rows, index.cfg.ov_model.encode))

    frames = await index.search(
        "квартальный отчёт банка о прибыли", top_k=3, query_type="vector"
    )
    items = SearchResponse.from_dataframes(frames, limit=3).results
    paths = [i.absolute_path for i in items]
    report_items = [i for i in items if i.absolute_path in COPIES]
    assert len(report_items) == 1, "три копии — один результат"
    assert sorted(
        [report_items[0].absolute_path, *report_items[0].duplicates]
    ) == sorted(COPIES)
    assert len(report_items[0].chunks) == 2
    # limit=3 — документы: копии мест не заняли, в выдаче все три документа.
    assert len(items) == 3
    assert {p for p in paths if p not in COPIES} == {
        "/d/договор.pdf",
        "/e/инструкция.pdf",
    }

    store.delete_sync(f"path = '{COPIES[2]}'")  # одну копию удалили с диска
    frames = await index.search(
        "квартальный отчёт банка о прибыли", top_k=3, query_type="vector"
    )
    item = next(
        i
        for i in SearchResponse.from_dataframes(frames, limit=3).results
        if i.absolute_path in COPIES
    )
    assert len(item.duplicates) == 1 and COPIES[2] not in [
        item.absolute_path,
        *item.duplicates,
    ]


def _frame(rows: list[tuple[str, float, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "idx": list(range(len(rows))),
            "text": [f"фрагмент {i}" for i in range(len(rows))],
            "meta": [
                {"original_name": Path(p).name, "sha256": sha} for p, _, sha in rows
            ],
            "path": [p for p, _, _ in rows],
            "_relevance_score": [s for _, s, _ in rows],
        }
    )


def test_without_copies_response_is_unchanged():
    frame = _frame(
        [("/x/a.txt", 0.9, "1"), ("/y/b.txt", 0.8, "2"), ("/x/a.txt", 0.7, "1")]
    )
    response = SearchResponse.from_dataframes([frame], limit=3)
    assert [i.absolute_path for i in response.results] == ["/x/a.txt", "/y/b.txt"]
    assert all(i.duplicates == [] for i in response.results)
    assert [len(i.chunks) for i in response.results] == [2, 1]


def test_copies_do_not_take_slots():
    frame = _frame(
        [
            ("/x/a.txt", 0.9, "1"),
            ("/z/a.txt", 0.9, "1"),
            ("/y/b.txt", 0.8, "2"),
            ("/w/c.txt", 0.7, "3"),
        ]
    )
    items = SearchResponse.from_dataframes([frame], limit=2).results
    assert [(i.absolute_path, i.duplicates) for i in items] == [
        ("/x/a.txt", ["/z/a.txt"]),
        ("/y/b.txt", []),
    ]


@pytest.mark.parametrize(
    ("data", "encoding"),
    [
        ("Отчёт о проверке".encode("cp1251"), "cp1251"),
        (codecs.BOM_UTF8 + "Отчёт".encode(), "utf-8-sig"),
        (codecs.BOM_UTF16_LE + "Отчёт".encode("utf-16-le"), "utf-16"),
        ("Отчёт о проверке филиала".encode("utf-16-le"), "utf-16-le"),
        (
            "Отчёт о проверке филиала банка".encode() + b"\xff",
            "utf-8",
        ),  # один битый байт — допуск
    ],
)
def test_txt_encoding_detection(data, encoding):
    text, found = decode_text(data)
    assert found == encoding
    assert text.lstrip("﻿").startswith("Отчёт")


_LOG = b"2026-10-08 12:00:01 INFO connection pool ok, host=db01 port=5432\n" * 800
_RU = ("Отчёт о проверке филиала банка.\n" * 50).encode()


@pytest.mark.parametrize(
    "tail", ["Ошибка подключения к базе", "Ок"], ids=["phrase", "word"]
)
def test_cp1251_words_in_long_ascii_log(tail):
    """Русских букв меньше одной на тысячу символов — всё равно cp1251, а не ромбы."""
    text, found = decode_text(_LOG + (tail + "\n").encode("cp1251"))
    assert found == "cp1251"
    assert text.endswith(tail + "\n") and "\ufffd" not in text


@pytest.mark.parametrize(
    "data",
    [
        _LOG + "Ошибка подключения к базе\n".encode() + b"\xff",
        _RU + b"\xff\xfe\xfd",
        _RU[:-2],  # обрезан посреди последней буквы
    ],
    ids=["log", "three-bad", "truncated"],
)
def test_utf8_with_few_broken_bytes_stays_utf8(data):
    text, found = decode_text(data)
    assert (found, text) == ("utf-8", data.decode("utf-8", "replace"))


def test_request_parameters_ignore_case():
    request = SearchRequest(
        query="отчёт", query_type="Hybrid", sources=["Files", " EMAIL "]
    )
    assert (request.query_type, request.sources) == ("hybrid", ["files", "email"])


def test_folder_exclusion_ignores_case():
    assert not check_path(Path("/home/u/Node_Modules/pkg/readme.txt"))
    assert not check_path(Path("/home/u/VENV/lib/notes.txt"))
    assert check_path(Path("/home/u/Документы/отчёт.txt"))


async def _fail_calls(path: Path) -> list[bool]:
    from src.parsers import _process_path

    stat = path.stat()
    calls: list[bool] = []

    async def fail_cb(_meta, *, permanent: bool) -> None:
        calls.append(permanent)

    async for _ in _process_path(
        FileMetadata(str(path), stat.st_size, int(stat.st_mtime)), fail_cb=fail_cb
    ):
        pass
    return calls


@pytest.mark.asyncio
async def test_pdf_without_text_and_scans_is_skipped_until_changed(tmp_path, caplog):
    path = tmp_path / "пустой.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.new_page()
        doc.save(str(path))
    with caplog.at_level("WARNING"):
        # Распознавать нечего: повтор дал бы то же — пропуск до изменения файла.
        assert await _fail_calls(path) == [True]
    assert any(
        "нет текстового слоя (2 стр., из них сканов 0" in r.getMessage()
        and r.exc_info is None
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_pdf_scan_without_ocr_is_retried(tmp_path, monkeypatch, caplog):
    import src.parsers.pdf as pdf_parser

    path = tmp_path / "скан.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), False)
        pix.clear_with(200)
        page.insert_image(page.rect, pixmap=pix)  # страница-картинка: скан
        doc.save(str(path))
    monkeypatch.setattr(pdf_parser, "recognize", lambda file, pages: {})  # OCR нет
    with caplog.at_level("WARNING"):
        # OCR может появиться (tessdata, включение в конфиге) — повтор на проходе.
        assert await _fail_calls(path) == [False]
    assert any("сканов 1, распознано OCR 0" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_encrypted_pdf_is_skipped(tmp_path, caplog):
    path = tmp_path / "secret.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "secret text")
        doc.save(
            str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o"
        )
    with caplog.at_level("WARNING"):
        assert await _fail_calls(path) == [True]
    assert any("зашифрован" in r.getMessage() for r in caplog.records)


@pytest.fixture
def bridge(stub, bridge_factory):  # noqa: F811
    return bridge_factory(stub.url)


def test_bridge_lists_copies(stub, bridge):  # noqa: F811
    stub.set(
        "GET",
        "/api/v1/status",
        {"is_indexing": False, "type": "idle", "files_to_index": 0},
    )
    hit = {"id": "/a/отчёт.pdf", "title": "отчёт.pdf", "type": "file", "score": 0.8,
           "absolute_path": "/a/отчёт.pdf", "duplicates": ["/b/отчёт.pdf"],
           "chunks": [{"content": "текст", "score": 0.8}]}  # fmt: skip
    stub.set("POST", "/api/v1/search", {"results": [hit], "total": 1})
    response = bridge.call("local_search", {"query": "отчёт", "query_type": "Hybrid"})
    assert "копии того же файла: /b/отчёт.pdf" in call_text(response)
    assert response["result"]["structuredContent"]["results"][0]["duplicates"] == [
        "/b/отчёт.pdf"
    ]
    assert stub.bodies("POST", "/api/v1/search")[0]["query_type"] == "hybrid"
