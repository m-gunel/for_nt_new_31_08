"""Фильтры поиска по метаданным: расширение, папка, даты, автор.

SearchRequest.filters переводится в условие where LanceDB (SearchFilters.where)
и применяется ДО отбора пула во всех query_type, в откате гибрида на вектор и
в дочитывании файла по коду из имени. MCP-мост принимает те же фильтры
параметрами local_search и проверяет их типы сам.
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest
from pydantic import ValidationError

from src.common import CARD_IDX, CARD_KIND
from src.common.doc_meta import doc_meta_schema
from src.indexer.indexer import VECTOR_NPROBES, VECTOR_REFINE_FACTOR, Index
from src.server.model import SearchFilters, SearchRequest
from src.server.router import _SEARCH_EXAMPLES
from src.server.router import search as search_endpoint
from tests.mcp_fixture import bridge_factory, call_text, stub  # noqa: F401
from tests.test_indexing_live import index, isolated_config  # noqa: F401
from tests.test_search_documents import _frame, _index, _Table


def test_no_filters_give_no_condition():
    assert SearchFilters().where() is None
    assert SearchRequest(query="x").filters is None


def test_ext_is_normalized_and_deduplicated():
    filters = SearchFilters(ext=[".DOCX", " pdf ", "docx"])
    assert filters.ext == ["docx", "pdf"]
    assert filters.where() == "meta.ext IN ('docx', 'pdf')"


def test_folder_is_a_prefix_with_separator():
    """«/docs/a» не захватывает «/docs/ab»; кавычка в пути экранирована."""
    assert SearchFilters(folder="/docs/a/").where() == "starts_with(path, '/docs/a/')"
    assert (
        SearchFilters(folder="/docs/д'Артаньян").where()
        == "starts_with(path, '/docs/д''Артаньян/')"
    )
    assert SearchFilters(folder="/").where() == "starts_with(path, '/')"


def test_date_bounds_are_inclusive_days():
    """Дата документа — из его свойств, без них — файла: та, что видна в выдаче."""
    where = SearchFilters(modified_from="2024-01-01", modified_to="2024-12-31").where()
    modified = "coalesce(meta.doc_modified, meta.date_modified)"
    assert where == (
        f"{modified} >= TIMESTAMP '2024-01-01 00:00:00' AND "
        f"{modified} < TIMESTAMP '2025-01-01 00:00:00'"
    )
    assert SearchFilters(created_to="2023-02-28").where() == (
        "coalesce(meta.doc_created, meta.date_created)"
        " < TIMESTAMP '2023-03-01 00:00:00'"
    )


def test_author_escapes_like_wildcards_and_quotes():
    where = SearchFilters(author=" O'Bri_en 100%\\ ").where()
    pattern = "'%o''bri\\_en 100\\%\\\\%' ESCAPE '\\'"
    assert where == (
        f"(replace(lower(meta.author), 'ё', 'е') LIKE {pattern} "
        f"OR replace(lower(meta.last_modified_by), 'ё', 'е') LIKE {pattern})"
    )


def test_filters_combine_with_and():
    where = SearchFilters(ext=["pdf"], author="иванов", folder="/docs").where()
    assert where.split(" AND ")[:2] == [
        "meta.ext IN ('pdf')",
        "starts_with(path, '/docs/')",
    ]
    assert "replace(lower(meta.author), 'ё', 'е') LIKE '%иванов%'" in where


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"ext": []},
        {"ext": ["", "  "]},
        {"folder": ""},
        {"author": "   "},
        {"modified_from": "", "modified_to": "", "created_from": "", "created_to": ""},
        {"ext": [], "folder": " ", "author": "", "modified_from": None},
    ],
)
def test_blank_filters_mean_no_filter(filters):
    """Незаполненные поля формы — «не фильтровать», а не ошибка 422."""
    request = SearchRequest(query="x", filters=filters)
    assert request.filters.where() is None


def test_blank_items_are_dropped_from_ext():
    assert SearchFilters(ext=["pdf", ""]).where() == "meta.ext IN ('pdf')"


@pytest.mark.parametrize(
    "filters",
    [
        {"ext": ["docx,pdf"]},
        {"ext": ["d'x"]},
        {"ext": ["."]},
        {"folder": "docs/отчёты"},
        {"folder": "string"},  # заглушка Swagger — не путь
        {"modified_from": "2024-02-01", "modified_to": "2024-01-31"},
        {"created_from": "2024-02-01", "created_to": "2024-01-31"},
        {"modified_to": "3000-01-01"},  # за пределом timestamp[ns] индекса
        {"modified_from": "2024-13-01"},
        {"modified_after": "2024-01-01"},  # опечатка клиента — не молчим
    ],
)
def test_bad_filters_are_rejected(filters):
    with pytest.raises(ValidationError):
        SearchRequest(query="x", filters=filters)


def test_swagger_examples_are_valid_and_the_first_has_no_filters():
    """«Try it out» в /docs подставляет первый пример: он должен сразу искать,
    а не падать на заглушках «string» в фильтрах."""
    first, *rest = _SEARCH_EXAMPLES.values()
    assert "filters" not in first["value"]
    for example in (first, *rest):
        SearchRequest(**example["value"])


WHERE = "meta.ext IN ('pdf')"


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["hybrid", "vector", "text"])
async def test_every_query_type_applies_the_filter(query_type):
    table = _Table(_frame([("/docs/a.pdf", 0.9)]))

    await Index.search(_index(table), "договор", query_type=query_type, where=WHERE)

    ((_, builder),) = table.searches
    assert ("where", (WHERE,)) in builder.calls


@pytest.mark.asyncio
async def test_hybrid_fallback_to_vector_keeps_the_filter():
    table = _Table(_frame([("/docs/a.pdf", 0.9)]), RuntimeError("no inverted index"))

    await Index.search(_index(table), "договор", where=WHERE)

    (_, hybrid), (_, vector) = table.searches
    assert ("where", (WHERE,)) in hybrid.calls
    assert ("where", (WHERE,)) in vector.calls


@pytest.mark.asyncio
async def test_without_filter_where_is_not_called():
    table = _Table(_frame([("/docs/a.pdf", 0.9)]))

    await Index.search(_index(table), "договор")

    ((_, builder),) = table.searches
    assert all(name != "where" for name, _ in builder.calls)


# путь → meta (поверх original_name и sha256); у каждого документа один чанк
# про отчёт и карточка. Ключ "text" — свой текст чанка и карточки вместо них.
DOCS = {
    "/docs/a/отчёт иванова.docx": {
        "ext": "docx",
        "author": "Иванов Иван",
        "date_modified": datetime(2024, 12, 31, 18, 30),
        "date_created": datetime(2023, 5, 1),
    },
    "/docs/a/отчёт петрова.pdf": {
        "ext": "pdf",
        "author": "Петров",
        "last_modified_by": "ИВАНОВ И.И.",
        "date_modified": datetime(2025, 1, 1, 0, 0),
        "date_created": datetime(2024, 6, 1),
    },
    "/docs/ab/отчёт сидорова.docx": {
        "ext": "docx",
        "author": "Сидоров_С",
        "date_modified": datetime(2024, 1, 1, 0, 0),
    },
    "/docs/o'k/отчёт 100%.txt": {
        "ext": "txt",
        "author": "100% Отдел",
        "date_modified": datetime(2023, 12, 31, 23, 59),
    },
}


def _meta_table(docs: dict[str, dict], embed) -> pa.Table:
    rows = []
    for path, meta in docs.items():
        text = meta.get("text")
        rows.append(
            (path, "text", 0, text or f"Годовой отчёт отдела, {Path(path).stem}")
        )
        rows.append((path, CARD_KIND, CARD_IDX, f"Документ: {text or Path(path).stem}"))
    vecs = np.asarray(embed([t for *_, t in rows]), dtype=np.float32)
    return pa.table(
        {
            "idx": pa.array([i for _, _, i, _ in rows], pa.int64()),
            "text": pa.array([t for *_, t in rows], pa.string()),
            "kind": pa.array([k for _, k, _, _ in rows], pa.string()),
            "meta": pa.array(
                [
                    {
                        "original_name": Path(p).name,
                        "sha256": p,
                        **{k: v for k, v in docs[p].items() if k != "text"},
                    }
                    for p, *_ in rows
                ],
                pa.struct(doc_meta_schema),
            ),
            "path": pa.array([p for p, *_ in rows], pa.string()),
            "vector": pa.FixedSizeListArray.from_arrays(vecs.ravel(), vecs.shape[1]),
        }
    )


async def _add(rag: Index, docs: dict[str, dict]) -> None:
    store = rag.cfg.lance.files
    await store.add_batch(_meta_table(docs, rag.cfg.ov_model.encode))
    store.create_fts(force=True)


async def _found(rag: Index, query: str, filters: dict | None, query_type: str):
    request = SearchRequest(
        query=query, limit=10, query_type=query_type, filters=filters
    )
    return (await search_endpoint(request, rag)).results


@pytest.mark.asyncio
async def test_live_each_filter_and_combination(index: Index):  # noqa: F811
    await _add(index, DOCS)
    a, b, ab, ok = DOCS
    cases = [
        (None, {a, b, ab, ok}),
        ({"ext": ["DOCX"]}, {a, ab}),
        ({"ext": ["docx", ".txt"]}, {a, ab, ok}),
        ({"folder": "/docs/a"}, {a, b}),  # не захватывает /docs/ab
        ({"folder": "/docs/o'k/"}, {ok}),
        # Конец периода — весь день: 31.12 18:30 входит, 01.01 00:00 — нет.
        ({"modified_from": "2024-01-01", "modified_to": "2024-12-31"}, {a, ab}),
        ({"modified_from": "2025-01-01"}, {b}),
        ({"modified_to": "2023-12-31"}, {ok}),
        # Без даты создания документ под фильтр по ней не попадает.
        ({"created_from": "2024-01-01"}, {b}),
        ({"created_to": "2023-12-31"}, {a}),
        # Автор или редактор, без учёта регистра.
        ({"author": "иванов"}, {a, b}),
        ({"author": "ИВАН"}, {a, b}),
        ({"ext": ["docx"], "author": "Иванов", "modified_to": "2024-12-31"}, {a}),
        ({"ext": ["docx"], "folder": "/docs/a", "author": "петров"}, set()),
        # % и _ из ввода — буквально, а не шаблон LIKE.
        ({"author": "100%"}, {ok}),
        ({"author": "%"}, {ok}),
        ({"author": "сидоров_"}, {ab}),
        ({"author": "в_и"}, set()),  # «в и» в «Иванов Иван» — не «в_и»
        ({"ext": ["xlsx"]}, set()),
    ]
    for query_type in ("hybrid", "vector", "text"):
        for filters, expected in cases:
            items = await _found(index, "годовой отчёт", filters, query_type)
            got = {i.absolute_path for i in items}
            assert got == expected, (query_type, filters)
            assert all(i.document is not None for i in items)


@pytest.mark.asyncio
async def test_live_name_code_respects_the_filter(index: Index):  # noqa: F811
    """Файл по коду из имени, не прошедший фильтр, не показывается."""
    code_doc = "/docs/a/ЦБ_21 положение.pdf"
    docs = {**DOCS, code_doc: {"ext": "pdf", "author": "Банк"}}
    await _add(index, docs)

    for query_type in ("hybrid", "vector", "text"):
        items = await _found(index, "Найди файл ЦБ_21", None, query_type)
        assert (items[0].absolute_path, items[0].match[0]) == (code_doc, "name_code")

        items = await _found(index, "Найди файл ЦБ_21", {"ext": ["pdf"]}, query_type)
        assert items[0].absolute_path == code_doc, query_type
        assert {i.absolute_path for i in items} <= {
            code_doc,
            "/docs/a/отчёт петрова.pdf",
        }, "только pdf"

        items = await _found(index, "Найди файл ЦБ_21", {"ext": ["docx"]}, query_type)
        paths = {i.absolute_path for i in items}
        assert code_doc not in paths, query_type
        assert all("name_code" not in i.match for i in items)


@pytest.mark.asyncio
async def test_live_document_read_in_by_code_is_filtered_too(index: Index):  # noqa: F811
    """Документ вне пула дочитывается с тем же условием, что и пул."""
    # Ни чанк, ни карточка с запросом не совпадают: документ находит только
    # код из имени (meta.original_name), и его строки дочитываются.
    code_doc = "/docs/b/obs_286 журнал.txt"
    docs = {code_doc: {"ext": "txt", "text": "Мушкетёры, глава первая"}}
    for i in range(60):
        docs[f"/docs/a/отчёт {i}.docx"] = {"ext": "docx"}
    await _add(index, docs)

    for filters, expected in (
        ({"folder": "/docs/b"}, [code_doc]),
        ({"ext": ["txt"]}, [code_doc]),
        ({"ext": ["docx"]}, []),
    ):
        pinned: list[str] = []
        (frame,) = await index.search(
            "Годовой отчёт obs_286",
            top_k=3,
            query_type="text",
            pinned=pinned,
            where=SearchFilters(**filters).where(),
        )
        assert pinned == expected, filters
        paths = set(frame["path"].to_list()) if frame is not None else set()
        assert (code_doc in paths) == bool(expected), filters


@pytest.mark.asyncio
async def test_live_author_filter_ignores_yo(index: Index):  # noqa: F811
    """«Ковалев» находит «Ковалёв», и наоборот: ё пишут как придётся."""
    yo, ye = "/docs/a/отчёт ковалёва.docx", "/docs/a/отчёт семенова.docx"
    await _add(
        index,
        {
            yo: {"ext": "docx", "author": "Ковалёв А."},
            ye: {"ext": "docx", "last_modified_by": "СЕМЕНОВ Б."},
        },
    )
    for query_type in ("hybrid", "vector", "text"):
        for author, expected in (
            ("Ковалев", {yo}),
            ("КОВАЛЁВ", {yo}),
            ("Семёнов", {ye}),
            ("семенов", {ye}),
        ):
            items = await _found(index, "годовой отчёт", {"author": author}, query_type)
            assert {i.absolute_path for i in items} == expected, (query_type, author)


@pytest.mark.asyncio
async def test_live_date_filter_uses_the_date_shown_in_results(index: Index):  # noqa: F811
    """Дата из свойств документа, а без неё — файла на диске.

    docx с dcterms:modified 2023-05-10, сохранённый из почты 2024-03-01:
    выдача пишет «изменён: 2023-05-10», и фильтр «за 2023» обязан его найти.
    """
    saved = "/docs/a/из почты.docx"
    plain = "/docs/a/без свойств.txt"
    await _add(
        index,
        {
            saved: {
                "ext": "docx",
                "doc_modified": datetime(2023, 5, 10),
                "date_modified": datetime(2024, 3, 1),
                "doc_created": datetime(2023, 5, 1),
                "date_created": datetime(2024, 3, 1),
            },
            plain: {"ext": "txt", "date_modified": datetime(2024, 3, 1)},
        },
    )
    year_2023 = {"modified_from": "2023-01-01", "modified_to": "2023-12-31"}
    year_2024 = {"modified_from": "2024-01-01", "modified_to": "2024-12-31"}
    for query_type in ("hybrid", "vector", "text"):
        for filters, expected in (
            (year_2023, {saved}),
            (year_2024, {plain}),
            ({"created_to": "2023-12-31"}, {saved}),
            ({"created_from": "2024-01-01"}, set()),  # у plain даты создания нет
        ):
            items = await _found(index, "годовой отчёт", filters, query_type)
            assert {i.absolute_path for i in items} == expected, (query_type, filters)


def _clustered_table(tmp_path, author_rows: int = 20):
    """6400 строк в 64 кластерах, IVF-PQ на 80 разделов (как create_vector_index).

    Под фильтр author='Иванов' — author_rows строк кластера 0, далёкого от
    запросов к остальным кластерам. Возвращает таблицу и центры кластеров.
    """
    import math

    import lancedb

    rng = np.random.default_rng(0)
    dim, rows = 16, 6400
    centers = rng.normal(size=(64, dim))
    labels = rng.integers(0, 64, rows)
    vectors = (centers[labels] + 0.05 * rng.normal(size=(rows, dim))).astype(np.float32)
    authors = ["x"] * rows
    for i in np.where(labels == 0)[0][:author_rows]:
        authors[i] = "Иванов"
    table = lancedb.connect(tmp_path / "ivf").create_table(
        "files",
        pa.table(
            {
                "path": [f"/docs/{i}.txt" for i in range(rows)],
                "text": ["запись журнала"] * rows,
                "author": authors,
                "vector": pa.FixedSizeListArray.from_arrays(vectors.ravel(), dim),
            }
        ),
    )
    table.create_index(
        vector_column_name="vector",
        num_partitions=int(math.sqrt(rows)),
        num_sub_vectors=4,
        metric="cosine",
    )
    table.create_fts_index("text")
    return table, centers


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["vector", "hybrid"])
async def test_narrow_filter_is_not_lost_in_unprobed_ivf_partitions(
    tmp_path, query_type
):
    """Строки под узким фильтром находятся и вне ближайших к запросу разделов
    IVF: nprobes ограничивает и максимум разделов, под фильтром потолок снят."""
    table, centers = _clustered_table(tmp_path)
    found = []
    for cluster in (2, 3, 4):

        async def async_encode(texts, role=None, center=centers[cluster]):
            return [center.tolist()]

        rag = SimpleNamespace(
            cfg=SimpleNamespace(ov_model=SimpleNamespace(async_encode=async_encode)),
            files_store=SimpleNamespace(
                table=table, has_table=True, table_name="files"
            ),
        )
        (frame,) = await Index.search(
            rag, "договор", top_k=10, query_type=query_type, where="author = 'Иванов'"
        )
        found.append(frame.height)
    assert found == [20, 20, 20]


@pytest.mark.asyncio
async def test_filter_lifts_the_ivf_probe_ceiling_only_with_filter():
    """Без фильтра предел разделов остаётся: поиск по всей таблице их и так находит.

    refine — в обеих ветках: под фильтром порядок тоже задают полные векторы.
    """
    refine = ("refine_factor", (VECTOR_REFINE_FACTOR,))
    for where, expected, absent in (
        (None, [("nprobes", (VECTOR_NPROBES,)), refine], "maximum_nprobes"),
        (
            WHERE,
            [
                ("minimum_nprobes", (VECTOR_NPROBES,)),
                ("maximum_nprobes", (0,)),
                refine,
            ],
            "nprobes",
        ),
    ):
        for query_type in ("hybrid", "vector"):
            table = _Table(_frame([("/docs/a.pdf", 0.9)]))
            await Index.search(
                _index(table), "договор", query_type=query_type, where=where
            )
            ((_, builder),) = table.searches
            assert all(call in builder.calls for call in expected), query_type
            assert all(name != absent for name, _ in builder.calls), query_type


SEARCH = "/api/v1/search"
STATUS = "/api/v1/status"


@pytest.fixture
def bridge(stub, bridge_factory):  # noqa: F811
    return bridge_factory(stub.url)


def test_mcp_maps_filter_arguments(stub, bridge):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    stub.set("GET", STATUS, {"is_indexing": False, "type": "idle", "files_to_index": 0})
    response = bridge.call(
        "local_search",
        {
            "query": "отчёт",
            "ext": ["DOCX", ".pdf", "docx"],
            "folder": "/docs/a/",
            "modified_after": "2024-01-01",
            "modified_before": "2024-12-31",
            "author": " Иванов ",
        },
    )
    (body,) = stub.bodies("POST", SEARCH)
    assert body["filters"] == {
        "ext": ["docx", "pdf"],
        "folder": "/docs/a",
        "modified_from": "2024-01-01",
        "modified_to": "2024-12-31",
        "author": "Иванов",
    }
    # Пусто — может, из-за фильтров: агент должен видеть, чем сужена выдача.
    text = call_text(response)
    assert "Фильтры: ext=docx, pdf; папка: /docs/a" in text
    assert "автор: Иванов" in text


def test_mcp_single_ext_string_and_no_filters(stub, bridge):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    bridge.call("local_search", {"query": "x", "ext": "XLSX"})
    bridge.call("local_search", {"query": "x", "ext": None, "author": None})
    first, second = stub.bodies("POST", SEARCH)
    assert first["filters"] == {"ext": ["xlsx"]}
    assert "filters" not in second, "без фильтров тело запроса прежнее"


def test_mcp_filters_are_shown_with_results(stub, bridge):  # noqa: F811
    hit = {
        "id": "/docs/a.docx",
        "title": "a.docx",
        "type": "file",
        "score": 0.5,
        "absolute_path": "/docs/a.docx",
        "chunks": [{"content": "текст", "score": 0.5}],
    }
    stub.set("POST", SEARCH, {"results": [hit], "total": 1})
    text = call_text(
        bridge.call("local_search", {"query": "x", "modified_after": "2024-01-01"})
    )
    assert text.splitlines()[:2] == [
        "Найдено документов: 1",
        "Фильтры: изменён с 2024-01-01",
    ]


def test_mcp_schema_lists_filter_parameters(bridge):
    (tool,) = [t for t in bridge.tools() if t["name"] == "local_search"]
    properties = tool["inputSchema"]["properties"]
    for name in ("ext", "folder", "modified_after", "modified_before", "author"):
        assert properties[name]["description"], name
    assert properties["ext"]["type"] == ["string", "array"]
    assert tool["inputSchema"]["required"] == ["query"]


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({"ext": 5}, "'ext'"),
        ({"ext": []}, "'ext'"),
        ({"ext": ["docx", 1]}, "расширение"),
        ({"ext": "docx,pdf"}, "списком"),
        ({"folder": "docs/a"}, "абсолютным"),
        ({"folder": ""}, "'folder'"),
        ({"folder": ["/docs"]}, "'folder'"),
        ({"modified_after": "01.02.2024"}, "ГГГГ-ММ-ДД"),
        ({"modified_before": "2024-02-30"}, "ГГГГ-ММ-ДД"),
        ({"modified_before": "3000-01-01"}, "2200"),
        ({"modified_after": 2024}, "'modified_after'"),
        (
            {"modified_after": "2024-12-31", "modified_before": "2024-01-01"},
            "позже",
        ),
        ({"author": "  "}, "'author'"),
        ({"author": ["Иванов"]}, "'author'"),
        ({"created_after": "2024-01-01"}, "неизвестные параметры"),
    ],
)
def test_mcp_bad_filter_arguments_are_rejected(stub, bridge, arguments, expected):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    error = bridge.call("local_search", {"query": "x", **arguments})["error"]
    assert error["code"] == -32602
    assert expected in error["message"]
    assert stub.bodies("POST", SEARCH) == [], (
        "невалидный вызов не должен доходить до API"
    )
