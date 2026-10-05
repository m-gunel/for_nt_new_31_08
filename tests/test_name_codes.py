"""Файл по коду из имени: «Найди файл ЦБ_21», «5348-У», «obs_286».

Код в запросе — слово с цифрой и буквой (или «_»). Если он стоит в имени
1–3 файлов, эти документы идут в выдаче первыми с match "name_code", а тех,
кого нет в пуле поиска, индексатор дочитывает из таблицы. Имена берутся из
первых чанков документов (есть и в индексе без карточек) и перестраиваются,
только когда меняется версия таблицы.
"""

from types import SimpleNamespace

import polars as pl
import pyarrow as pa
import pytest

from src.common import CARD_KIND
from src.indexer.indexer import Index
from src.indexer.name_codes import (
    MAX_FILES_PER_CODE,
    PINNED_CHUNKS,
    NameIndex,
    doc_name,
    is_code,
    query_codes,
)
from src.server.model import SearchRequest, SearchResponse
from src.server.router import search as search_endpoint
from tests.test_indexing_live import index, isolated_config  # noqa: F401
from tests.test_search_documents import _frame, _rows_table

# ------------------------------------------------------------ код в запросе ----


@pytest.mark.parametrize(
    "query, codes",
    [
        ("Найди файл ЦБ_21", ["цб 21"]),
        ("Указание «5348-У».", ["5348 у"]),
        ("где obs_286?", ["obs 286"]),
        ("приказ 12-МР от 2024 года", ["12 мр"]),
        ("ЦБ_21 и цб_21, (ЦБ_21)", ["цб 21"]),
        ("Ёлка_1", ["елка 1"]),
    ],
)
def test_query_codes(query, codes):
    assert query_codes(query) == codes


@pytest.mark.parametrize(
    "query, codes",
    [
        ("Найди файл ЦБ_21.pdf", ["цб 21"]),
        ("obs_286.txt", ["obs 286"]),
        ("«5348-У.DOCX»", ["5348 у"]),
        # Не расширение разбираемого формата — часть кода.
        ("версия v1.2", ["v1 2"]),
        ("архив ЦБ_21.zip", ["цб 21 zip"]),
    ],
)
def test_file_extension_is_not_part_of_the_code(query, codes):
    """Файл называют полным именем, а имена в словаре — без расширения."""
    assert query_codes(query) == codes


def test_code_with_extension_finds_the_file():
    table = _CardTable({"/docs/ЦБ_21.pdf": "ЦБ_21.pdf", "/docs/a.txt": "a.txt"})
    assert NameIndex().hits(table, query_codes("Найди файл ЦБ_21.pdf")) == [
        "/docs/ЦБ_21.pdf"
    ]


@pytest.mark.parametrize("query", ["отчёт за 2024", "15 2023", "найди файл отчёт", ""])
def test_years_and_bare_numbers_are_not_codes(query):
    assert query_codes(query) == []


def test_is_code():
    assert is_code("ЦБ_21") and is_code("5348-У") and is_code("1_2")
    assert not is_code("2024") and not is_code("ЦБ") and not is_code("12-15")


def test_doc_name_prefers_original_name():
    assert doc_name("email_INBOX:1:2:вложение", {"original_name": "ЦБ_21.pdf"}) == (
        "цб 21"
    )
    assert doc_name("/docs/Отчёт 5348-У.docx", None) == "отчет 5348 у"


# ----------------------------------------------------- словарь имён таблицы ----


class _CardTable:
    """Первые чанки документов: считает полные проходы, версия — как у LanceDB.

    names — путь → original_name; shas — путь → sha256 (у кого он есть).
    """

    def __init__(self, names: dict[str, str], shas: dict[str, str] | None = None):
        self.version = 1
        self.names = names
        self.shas = shas or {}
        self.loads = 0

    def search(self):
        table = self

        class _Query:
            def where(self, predicate):
                # Не карточки: в индексе, собранном до них, их нет.
                assert predicate == "idx = 0"
                return self

            def select(self, columns):
                # Три строковых поля, а не весь meta: словарь пересобирается
                # на каждую запись в таблицу.
                assert columns == {
                    "path": "path",
                    "name": "meta.original_name",
                    "sha256": "meta.sha256",
                }
                return self

            def to_arrow(self):
                table.loads += 1
                return pa.table(
                    {
                        "path": list(table.names),
                        "name": list(table.names.values()),
                        "sha256": pa.array(
                            [table.shas.get(p) for p in table.names], pa.string()
                        ),
                    }
                )

        return _Query()


def test_code_matches_whole_words_of_the_name():
    table = _CardTable(
        {
            "/a/ЦБ_21 положение.pdf": "ЦБ_21 положение.pdf",
            "/a/ЦБ_210.pdf": "ЦБ_210.pdf",
            "/a/Указание 5348-У.docx": "Указание 5348-У.docx",
        }
    )
    names = NameIndex()
    assert names.hits(table, query_codes("найди ЦБ_21")) == ["/a/ЦБ_21 положение.pdf"]
    assert names.hits(table, query_codes("5348-У и ЦБ_21")) == [
        "/a/Указание 5348-У.docx",
        "/a/ЦБ_21 положение.pdf",
    ]
    assert names.hits(table, query_codes("obs_286")) == []


def test_code_shared_by_more_than_three_files_is_ignored():
    shared = {f"/a/v2 отчёт {i}.pdf": f"v2 отчёт {i}.pdf" for i in range(4)}
    table = _CardTable({**shared, "/a/obs_286.txt": "obs_286.txt"})
    names = NameIndex()
    assert len(shared) > MAX_FILES_PER_CODE
    assert names.hits(table, ["v2"]) == []
    # Чужой бесполезный код не мешает полезному.
    assert names.hits(table, query_codes("v2 obs_286")) == ["/a/obs_286.txt"]
    del table.names["/a/v2 отчёт 3.pdf"]
    table.version += 1
    assert len(names.hits(table, ["v2"])) == MAX_FILES_PER_CODE


def test_copies_of_one_document_count_once():
    """Копии одного содержимого (тот же sha256) — один документ, а не четыре.

    Выдача схлопывает их в один результат (duplicates); считай порог пути,
    четвёртая копия — «Загрузки», «Документы», «(1)» от браузера — отключала
    бы закрепление единственного документа.
    """
    copies = {
        "/Загрузки/5348-У.pdf": "5348-У.pdf",
        "/Загрузки/5348-У (1).pdf": "5348-У (1).pdf",
        "/Документы/5348-У.pdf": "5348-У.pdf",
        "/Рабочий стол/5348-У.pdf": "5348-У.pdf",
    }
    table = _CardTable(copies, dict.fromkeys(copies, "s1"))
    assert len(copies) > MAX_FILES_PER_CODE
    assert NameIndex().hits(table, query_codes("Найди файл 5348-У")) == sorted(copies)

    # Разные документы под тем же кодом по-прежнему считаются порознь.
    table = _CardTable(copies, {p: str(i) for i, p in enumerate(copies)})
    assert NameIndex().hits(table, query_codes("Найди файл 5348-У")) == []


def test_names_rebuilt_only_when_table_version_changes():
    table = _CardTable({"/a/ЦБ_21.pdf": "ЦБ_21.pdf"})
    names = NameIndex()
    for _ in range(3):
        assert names.hits(table, ["цб 21"]) == ["/a/ЦБ_21.pdf"]
    assert table.loads == 1, "без записи в таблицу словарь не перестраивается"

    table.names = {"/b/ЦБ_21 новый.pdf": "ЦБ_21 новый.pdf"}
    assert names.hits(table, ["цб 21"]) == ["/a/ЦБ_21.pdf"], "версия та же"
    table.version += 1
    assert names.hits(table, ["цб 21"]) == ["/b/ЦБ_21 новый.pdf"]
    assert table.loads == 2

    # Таблицу пересоздали (сброс индекса): версия могла совпасть со старой.
    fresh = _CardTable({"/c/ЦБ_21.pdf": "ЦБ_21.pdf"})
    fresh.version = table.version
    assert names.hits(fresh, ["цб 21"]) == ["/c/ЦБ_21.pdf"]


def test_query_without_codes_does_not_read_names():
    table = _CardTable({"/a/ЦБ_21.pdf": "ЦБ_21.pdf"})
    assert NameIndex().hits(table, query_codes("найди положение")) == []
    assert table.loads == 0


# --------------------------------------------------------- порядок выдачи ----


def _pool_and_pinned_tail(score_col: str = "_relevance_score") -> pl.DataFrame:
    """Пул из 12 документов и дочитанные строки закреплённого /z.txt без оценки."""
    pool = _frame([(f"/docs/{i:02d}.txt", 1.0 - i / 100) for i in range(12)])
    if score_col == "_distance":
        pool = pool.with_columns(
            (1 - pl.col("_relevance_score")).alias("_distance")
        ).drop("_relevance_score")
    tail = pl.DataFrame(
        {
            "idx": [-1, 0, 1],
            "text": ["Документ: z", "z 0", "z 1"],
            "kind": [CARD_KIND, "text", "text"],
            "meta": [{"original_name": "z.txt", "sha256": None}] * 3,
            "path": ["/z.txt"] * 3,
        }
    )
    return pl.concat([pool, tail], how="diagonal_relaxed")


def test_pinned_document_from_deep_in_the_pool_goes_first():
    frame = _frame([(f"/docs/{i:02d}.txt", 1.0 - i / 100) for i in range(30)])
    items = SearchResponse.from_dataframes(
        [frame], limit=5, pinned=["/docs/27.txt"]
    ).results
    assert [i.absolute_path for i in items] == [
        "/docs/27.txt",
        "/docs/00.txt",
        "/docs/01.txt",
        "/docs/02.txt",
        "/docs/03.txt",
    ], "закреплённый занимает место из того же limit"
    assert items[0].match == ["name_code"]
    assert items[0].score == pytest.approx(0.73), "оценка не подделывается"
    assert all(i.match == [] for i in items[1:])


def test_pinned_order_pool_score_then_documents_read_outside_the_pool():
    for score_col in ("_relevance_score", "_distance"):
        items = SearchResponse.from_dataframes(
            [_pool_and_pinned_tail(score_col)],
            limit=4,
            pinned=["/z.txt", "/docs/09.txt", "/docs/05.txt"],
        ).results
        assert [i.absolute_path for i in items] == [
            "/docs/05.txt",
            "/docs/09.txt",
            "/z.txt",
            "/docs/00.txt",
        ], score_col
        z = items[2]
        # Дочитанная карточка с запросом не сверялась: отметки card нет.
        assert z.match == ["name_code"]
        assert [c.content for c in z.chunks] == ["z 0", "z 1"]
        # Оценки у дочитанного нет — null, а не 0, который в vector был бы
        # лучшей оценкой выдачи, а в hybrid — худшей.
        assert z.score is None and [c.score for c in z.chunks] == [None, None]
        assert all(i.score is not None for i in items if i is not z)


def test_without_pinned_rows_read_outside_the_pool_do_not_outrank_it():
    """Вектор: оценка — минус расстояние, а у дочитанных строк её нет вовсе."""
    items = SearchResponse.from_dataframes(
        [_pool_and_pinned_tail("_distance")], limit=20
    ).results
    assert items[-1].absolute_path == "/z.txt"


def test_pinned_copy_pins_the_path_kept_in_the_results():
    frame = _frame(
        [("/a/копия.txt", 0.9, "text", "s1"), ("/b/ЦБ_21.txt", 0.8, "text", "s1")]
        + [(f"/docs/{i}.txt", 0.95 - i / 100) for i in range(3)]
    )
    items = SearchResponse.from_dataframes(
        [frame], limit=2, pinned=["/b/ЦБ_21.txt"]
    ).results
    assert items[0].absolute_path == "/a/копия.txt"
    assert items[0].duplicates == ["/b/ЦБ_21.txt"]
    assert items[0].match == ["name_code"]


def test_more_pinned_than_limit_keeps_the_best_pinned():
    frame = _frame([(f"/docs/{i}.txt", 1.0 - i / 10) for i in range(5)])
    items = SearchResponse.from_dataframes(
        [frame], limit=1, pinned=["/docs/4.txt", "/docs/3.txt"]
    ).results
    assert [i.absolute_path for i in items] == ["/docs/3.txt"]


@pytest.mark.asyncio
async def test_search_without_pinned_list_does_not_look_for_codes():
    """Старые вызовы Index.search (без pinned) работают как раньше."""

    class _Table:
        searches = 0

        def search(self, *args, **kwargs):
            _Table.searches += 1
            builder = SimpleNamespace()
            for name in (
                "refine_factor",
                "nprobes",
                "distance_type",
                "vector",
                "text",
                "rerank",
                "limit",
            ):
                setattr(builder, name, lambda *a: builder)
            builder.to_polars = lambda: None
            return builder

    async def async_encode(texts, role=None):
        return [[0.0] * 8]

    store = SimpleNamespace(table=_Table(), has_table=True, table_name="files")
    fake = SimpleNamespace(
        cfg=SimpleNamespace(ov_model=SimpleNamespace(async_encode=async_encode)),
        files_store=store,
        email_store=store,
    )
    assert await Index.search(fake, "Найди файл ЦБ_21") == [None]
    assert _Table.searches == 1, "ни словаря имён, ни дочитывания"


# ------------------------------------------------- живой индекс (LanceDB) ----


async def _add(rag: Index, rows: list[tuple[str, str, str]]) -> None:
    store = rag.cfg.lance.files
    await store.add_batch(_rows_table(rows, rag.cfg.ov_model.encode))
    store.create_fts(force=True)


def _doc(path: str, text: str, chunks: int = 1) -> list[tuple[str, str, str]]:
    rows = [(path, "text", f"{text}, часть {i}") for i in range(chunks)]
    return rows + [(path, CARD_KIND, f"Документ: {path.rsplit('/', 1)[-1]}")]


async def _search(rag: Index, query: str, limit: int = 5, query_type="hybrid"):
    """Ручка /search целиком: индексатор находит код, выдача ставит первым."""
    request = SearchRequest(query=query, limit=limit, query_type=query_type)
    return (await search_endpoint(request, rag)).results


@pytest.mark.asyncio
async def test_live_code_puts_the_file_first(index: Index):  # noqa: F811
    rows = _doc("/docs/ЦБ_21 положение о резервах.txt", "Положение о резервах")
    for i in range(8):
        rows += _doc(f"/docs/резервы {i}.txt", f"Резервы банка, найди файл {i}", 3)
    await _add(index, rows)

    for query_type in ("hybrid", "vector", "text"):
        items = await _search(index, "Найди файл ЦБ_21", query_type=query_type)
        assert items[0].absolute_path == "/docs/ЦБ_21 положение о резервах.txt"
        assert items[0].match[0] == "name_code", query_type
        assert all("name_code" not in i.match for i in items[1:])


@pytest.mark.asyncio
async def test_live_code_of_many_files_and_years_change_nothing(index: Index):  # noqa: F811
    rows = []
    for i in range(MAX_FILES_PER_CODE + 1):
        rows += _doc(f"/docs/форма 0409_{i} v2.txt", f"Отчётная форма {i}")
    rows += _doc("/docs/отчёт 2024.txt", "Годовой отчёт")
    await _add(index, rows)

    for query in ("форма v2", "отчёт 2024", "отчёт 2024 15"):
        pinned: list[str] = []
        frames = await index.search(query, top_k=5, pinned=pinned)
        assert pinned == [], query
        plain = SearchResponse.from_dataframes(frames, limit=5).results
        assert [i.model_dump() for i in await _search(index, query)] == [
            i.model_dump() for i in plain
        ]


@pytest.mark.asyncio
async def test_live_document_outside_the_pool_is_read_in(index: Index):  # noqa: F811
    path = "/docs/д'Артаньян obs_286.txt"  # кавычка в пути — экранирование SQL
    rows = [(path, "text", f"Мушкетёры, глава {i}") for i in range(PINNED_CHUNKS + 2)]
    # Ни карточка, ни чанки с запросом не совпадают: документ находит только
    # код из имени (имя — meta.original_name, а не текст карточки).
    rows.append((path, CARD_KIND, "Документ: мушкетёры"))
    rows += _doc("/docs/o'brien.txt", "Резервы банка")
    for i in range(60):
        rows.append((f"/docs/резервы {i}.txt", "text", f"Резервы банка, раздел {i}"))
    await _add(index, rows)

    pinned: list[str] = []
    frames = await index.search(
        "резервы obs_286", top_k=3, query_type="text", pinned=pinned
    )
    assert pinned == [path]
    (frame,) = frames
    assert (
        frame.filter(pl.col("_score").is_not_null())["path"]
        .str.contains("obs_286")
        .sum()
        == 0
    ), "в пуле документа нет — его строки дочитаны"
    read_in = frame.filter(pl.col("path") == path)
    assert read_in["idx"].to_list() == [-1, *range(PINNED_CHUNKS)], (
        "карточка и первые чанки, по порядку"
    )
    assert (
        "vector" not in read_in.columns
        or read_in["vector"].null_count() == read_in.height
    ), "вектор дочитанным строкам не нужен"

    items = SearchResponse.from_dataframes(frames, limit=3, pinned=pinned).results
    assert len(items) == 3
    assert items[0].absolute_path == path
    # Карточка дочитана, а не найдена: запрос с ней не совпал.
    assert items[0].match == ["name_code"]
    assert items[0].score is None
    assert [c.content for c in items[0].chunks] == [
        f"Мушкетёры, глава {i}" for i in range(PINNED_CHUNKS)
    ]


@pytest.mark.asyncio
async def test_live_names_follow_table_changes(index: Index):  # noqa: F811
    await _add(index, _doc("/docs/резервы.txt", "Резервы банка"))
    assert all("name_code" not in i.match for i in await _search(index, "obs_286"))

    await _add(index, _doc("/docs/obs_286 наблюдения.txt", "Журнал наблюдений"))
    items = await _search(index, "obs_286")
    assert (items[0].absolute_path, items[0].match[0]) == (
        "/docs/obs_286 наблюдения.txt",
        "name_code",
    )

    await index.files_store.delete_where("path = '/docs/obs_286 наблюдения.txt'")
    assert all("name_code" not in i.match for i in await _search(index, "obs_286")), (
        "удалённый файл из словаря пропал"
    )


@pytest.mark.asyncio
async def test_live_pinned_document_found_only_by_its_card_gets_fragments(
    index: Index,  # noqa: F811
):
    """«Найди файл …» обычно совпадает с карточкой, а не с текстом файла.

    Дочитывались только документы, которых в пуле нет вовсе, и закреплённый
    документ, от которого в пуле одна карточка, шёл первым без единого
    фрагмента — меньше, чем получил бы, не найдись он вовсе.
    """
    path = "/docs/obs_286 журнал.txt"
    rows = [(path, "text", f"Мушкетёры, глава {i}") for i in range(PINNED_CHUNKS + 2)]
    rows.append((path, CARD_KIND, "Документ: obs_286 журнал наблюдений"))
    for i in range(60):
        rows += _doc(f"/docs/запись {i}.txt", f"Журнал наблюдений, запись {i}")
    await _add(index, rows)

    pinned: list[str] = []
    frames = await index.search(
        "журнал наблюдений obs_286", top_k=3, query_type="text", pinned=pinned
    )
    assert pinned == [path]
    pooled = frames[0].filter(pl.col("_score").is_not_null() & (pl.col("path") == path))
    assert pooled["kind"].to_list() == [CARD_KIND], "в пуле от документа — карточка"

    (item, *_) = SearchResponse.from_dataframes(frames, limit=3, pinned=pinned).results
    assert (item.absolute_path, item.match) == (path, ["name_code", "card"])
    assert [c.content for c in item.chunks] == [
        f"Мушкетёры, глава {i}" for i in range(PINNED_CHUNKS)
    ]
