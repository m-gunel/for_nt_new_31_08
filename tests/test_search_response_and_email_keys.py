"""Две регрессии на «одна строка данных гасит целую функцию».

1. `SearchResponse.from_dataframes` (src/server/model.py) индексировала
   `row["meta"]["original_name"]`. Колонка `meta` в схеме чанка nullable
   (src/common/doc_meta.py), то есть строка с `meta=None` — законное
   содержимое таблицы: она могла быть записана до появления метаданных,
   мигрирована из старой схемы или вставлена руками. Одной такой строки в
   выдаче хватало, чтобы весь POST /search отдал 500 с
   `TypeError: 'NoneType' object is not subscriptable` — падали и все
   остальные, совершенно здоровые результаты.

2. `_process_email_chunks` (src/parsers/__init__.py) разбирала ключ письма
   через `p.split(":")` в ровно три части. Ключ имеет вид
   `email_<папка>:<start>:<stop>`, а имя почтовой папки само может содержать
   двоеточие («Входящие:Архив 2024» — обычное дело для IMAP-иерархии).
   Тогда `split` давал четыре части и более, распаковка бросала ValueError,
   письмо гасил внешний `except Exception` в `async_parse_files` — и оно не
   индексировалось никогда.

   Тот же ключ разбирает и ручка `POST /api/v1/get_file_path`
   (src/server/router.py): она резала его `split(":", 3)` слева и на папке с
   двоеточием давала 400 «invalid offsets». Теперь разбор вынесен в
   `_EMAIL_PATH_RE`, и он стережётся здесь же — так закреплены обе половины
   контракта: и индексация письма, и его открытие.

Почему второй дефект проверяется прямым вызовом `_process_email_chunks`, а
не через `async_parse_files`: снаружи дефект НЕОТЛИЧИМ от штатной ситуации.
`async_parse_files` ловит любое исключение письма и зовёт
`fail_cb(permanent=False)` — ровно то же самое, что происходит, когда папка
не открылась («no file for prefix»). Именно поэтому дефект и жил незамеченным:
в логе — обычная строчка, в очереди — обычный повтор. Различает старый и
новый код только уровень самой `_process_email_chunks`: раньше на ключе с
двоеточием в имени папки она бросала ValueError, не дойдя ни до чтения, ни до
разбора; теперь доходит и отдаёт чанки.

Модель эмбеддингов не поднимается: mbox подставной, а сплиттер подменён —
настоящий меряет длину токенизатором OpenVINO и тянет за собой модель.
"""

from typing import Any

import polars as pl
import pyarrow as pa
import pytest
from pytest import fixture

from src.common import FileMetadata
from src.common.doc_meta import doc_meta_schema
from src.config import Config
from src.parsers import _process_email_chunks
from src.server.model import SearchResponse
from src.server.router import _EMAIL_PATH_RE
from tests.mbox_fixture import make_plain_email

# ==========================================================================
# Дефект 1: строка без метаданных в выдаче поиска
# ==========================================================================

_FILE_PATH = "/home/user/Документы/Договор поставки.pdf"


def _frame_with_null_struct_meta(path: str, text: str, score: float) -> pl.DataFrame:
    """Кадр ровно в той форме, в какой его отдаёт LanceDB: meta — null-struct.

    Схема колонки — настоящая `doc_meta_schema`, значение — null (см.
    `_mixed_rows` в tests/test_source_separation.py: точно так же там
    собирается таблица со строками без метаданных).
    """
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
    # Предпосылка кадра — явно, чтобы смена поведения polars была видна как
    # падение с внятным сообщением, а не как молча выродившийся тест: если
    # null-struct вдруг доедет чем-то осмысленным, проверять тут нечего.
    row = next(frame.iter_rows(named=True))  # type: ignore[union-attr]
    assert row["meta"] is None or all(v is None for v in row["meta"].values()), (
        "кадр обязан доносить до модели именно отсутствие метаданных"
    )
    return frame  # type: ignore[return-value]


def _frame_with_null_meta_column(path: str, text: str, score: float) -> pl.DataFrame:
    """Тот же случай, но колонка meta собрана polars'ом из чистых None.

    Две формы одного и того же кадра — намеренно. Как polars отдаёт наружу
    null-struct (`None` или словарь из `None`), зависит от его версии, а вот
    колонка из одних None доезжает до `from_dataframes` как None при любой.
    Так хотя бы один из двух прогонов гарантированно воспроизводит дефект.
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
    """Строка с meta=None не должна ронять весь ответ поиска.

    Раньше `row["meta"]["original_name"]` на такой строке бросало TypeError,
    и POST /search отдавал 500 целиком — вместе со всеми здоровыми
    результатами. Титул при отсутствии метаданных берётся из имени файла.
    """
    frame = make_frame(_FILE_PATH, "предмет договора и сроки поставки", 0.73)

    response = SearchResponse.from_dataframes([frame])

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

    Строка без meta стоит ПЕРВОЙ намеренно: старый код падал на ней, не успев
    дойти до строки с метаданными, и терялся не только титул, а весь ответ.

    Строка без метаданных приходит отдельным кадром, а не соседней строкой
    arrow-таблицы: `from_dataframes` принимает выдачи нескольких источников и
    склеивает их строки по порядку, так что первой она быть не перестала,
    зато колонка из одних None гарантированно доезжает до модели как None
    (arrow'ов null-struct polars иных версий отдаёт словарём из None — на нём
    старое `row["meta"]["original_name"]` не упало бы, и тест был бы зелёным
    ещё до правки).
    """
    frames = [
        _frame_with_null_meta_column(_FILE_PATH, "второй кусок договора", 0.41),
        _search_frame(
            [
                (
                    _FILE_PATH,
                    "первый кусок договора",
                    {"original_name": "Договор поставки № 17.pdf"},
                    0.92,
                ),
            ]
        ),
    ]

    response = SearchResponse.from_dataframes(frames)

    assert response.total == 1, "оба чанка — один и тот же файл"
    (item,) = response.results
    assert item.title == "Договор поставки № 17.pdf", (
        "титул есть у соседней строки — имя файла тут уже не фолбэк"
    )
    assert len(item.chunks) == 2
    assert item.score == pytest.approx(0.92), "оценка файла — максимум по чанкам"


def test_email_row_without_meta_stays_an_email():
    """Письмо без метаданных не должно ни падать, ни менять тип на «файл».

    Тот же TypeError, но на почтовой строке: тип определяется по пути, а не
    по meta, и титулом становится сам ключ письма.
    """
    key = "email_Входящие:Архив 2024:100:250:body"
    frame = _frame_with_null_meta_column(key, "текст письма без метаданных", 0.5)

    response = SearchResponse.from_dataframes([frame])

    (item,) = response.results
    assert item.type == "email"
    assert item.title == key
    assert item.id == key


# ==========================================================================
# Дефект 2: двоеточие в имени почтовой папки
# ==========================================================================

_SUBJECT = "Протокол совещания"
_BODY = "Коллеги, регламент согласован."
_MESSAGE = make_plain_email(
    _SUBJECT, _BODY, msg_id="colon-folder-1@fixture"
).as_bytes()
# Письмо лежит не с нулевого смещения: если start/stop разберутся неверно,
# из mbox прочитается мусор, а не письмо, и это будет видно по чанкам.
_START = 137
_STOP = _START + len(_MESSAGE)
_MBOX = b"F" * _START + _MESSAGE + b"\nFrom hvost@fixture\n\n"


class _FakeMbox:
    """Подставной mbox с интерфейсом aiofiles, запоминающий запросы.

    Настоящий файл здесь не нужен и даже вреден: проверяется, что письмо
    ищут по правильному смещению, а `seeks`/`reads` говорят об этом прямо.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.seeks: list[int] = []
        self.reads: list[int] = []

    async def seek(self, offset: int) -> int:
        self.seeks.append(offset)
        self._pos = offset
        return offset

    async def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        data = self._data[self._pos :] if size < 0 else self._data[self._pos :][:size]
        self._pos += len(data)
        return data


class _RecordingFailCb:
    """`FailCallback`, запоминающий (путь, permanent) каждого вызова."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, meta: FileMetadata, *, permanent: bool) -> None:
        self.calls.append((meta.path, permanent))


@fixture
def one_chunk_splitter(config: Config, monkeypatch):
    """Сплиттер-заглушка вместо настоящего.

    Настоящий `cfg.embed_splitter` меряет длину токенизатором OpenVINO, то
    есть поднимает модель ради разбора одного ключа. Значение cached_property
    лежит в `__dict__` инстанса (тем же и пользуются соседние тесты, делая
    `config.__dict__.pop("lance")`), поэтому monkeypatch.setitem подменяет его
    и возвращает всё как было.
    """

    class _Splitter:
        def split_text(self, text: str) -> list[str]:
            return [text]

    monkeypatch.setitem(config.__dict__, "embed_splitter", _Splitter())
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "folder",
    [
        "INBOX",  # контроль: обычная папка, этот случай работал и раньше
        "Входящие:Архив 2024",
        "Проекты:2024:Q1",
    ],
)
async def test_email_key_offsets_are_the_last_two_fields(
    one_chunk_splitter: Config, folder: str
):
    """Смещения письма — два последних поля ключа, всё до них — имя папки.

    Раньше ключ резался `split(":")` ровно на три части, поэтому письмо из
    папки с двоеточием в имени давало ValueError: разбор не доходил ни до
    чтения mbox, ни до чанков, а внешний `except` в `async_parse_files`
    превращал это в «попробуем в следующий раз» — то есть навсегда.
    """
    path = f"email_{folder}:{_START}:{_STOP}"
    mbox = _FakeMbox(_MBOX)
    fails = _RecordingFailCb()
    meta = FileMetadata(path=path, size=_STOP - _START, mtime_seconds=1_700_000_000)

    chunks = [
        chunk
        async for chunk in _process_email_chunks({folder: mbox}, meta, fail_cb=fails)
    ]

    assert mbox.seeks == [_START], (
        "письмо ищут по началу из ключа, а имя папки — всё, что левее смещений"
    )
    assert mbox.reads == [_STOP - _START]
    assert [chunk["path"] for chunk in chunks] == [f"{path}:body"], (
        "путь чанка обязан сохранять ключ письма целиком, вместе с двоеточиями"
    )
    assert _BODY in chunks[0]["text"], "прочитано именно письмо, а не соседние байты"
    assert chunks[0]["meta"]["original_name"] == _SUBJECT
    assert fails.calls == [], "письмо разобрано — жаловаться не на что"


@pytest.mark.asyncio
async def test_oversized_email_from_folder_with_colon_is_skipped_not_crashed(
    config: Config, monkeypatch
):
    """Отсечение по размеру считает письмо, а не поля ключа.

    Гейт по размеру срабатывает ДО чтения mbox, поэтому он первым и натыкался
    на ValueError от `split(":")`: письмо из папки с двоеточием не отсекалось
    по размеру, а падало. Здесь оно должно быть тихо помечено permanent=True
    (меньше оно уже не станет) и не прочитано вовсе.
    """
    monkeypatch.setattr(config.email_scan_cfg, "max_message_size", 1024)
    folder = "Входящие:Архив 2024"
    path = f"email_{folder}:1000:60000"
    mbox = _FakeMbox(_MBOX)
    fails = _RecordingFailCb()
    meta = FileMetadata(path=path, size=59_000, mtime_seconds=1_700_000_000)

    chunks = [
        chunk
        async for chunk in _process_email_chunks({folder: mbox}, meta, fail_cb=fails)
    ]

    assert chunks == []
    assert mbox.seeks == [], "отсечённое по размеру письмо не должно искаться"
    # Без этого чтение с нулевой позиции (без seek) утекло бы незамеченным:
    # разбор мусора тоже не дал бы чанков, и chunks == [] осталось бы верным.
    assert mbox.reads == [], "отсечённое по размеру письмо не должно читаться"
    assert fails.calls == [(path, True)], (
        "permanent=True: письмо не станет меньше, возвращаться к нему незачем"
    )


# ==========================================================================
# Тот же ключ, вторая половина контракта: разбор в ручке get_file_path
# ==========================================================================


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param(
            "email_INBOX:100:250:body",
            ("INBOX", "100", "250", "body"),
            id="plain-folder",
        ),
        pytest.param(
            "email_Входящие:Архив 2024:100:250:body",
            ("Входящие:Архив 2024", "100", "250", "body"),
            id="colon-in-folder",
        ),
        pytest.param(
            "email_Проекты:2024:Q1:100:250:body",
            ("Проекты:2024:Q1", "100", "250", "body"),
            id="digits-in-folder",
        ),
        pytest.param(
            "email_INBOX:100:250:Отчёт:Q3.pdf",
            ("INBOX", "100", "250", "Отчёт:Q3.pdf"),
            id="colon-in-attachment",
        ),
        pytest.param(
            "email_Входящие:Архив 2024:100:250:Отчёт:Q3.pdf",
            ("Входящие:Архив 2024", "100", "250", "Отчёт:Q3.pdf"),
            id="colon-in-both",
        ),
    ],
)
def test_email_path_pattern_keeps_colons_in_folder_and_attachment(
    key: str, expected: tuple[str, str, str, str]
):
    """Опорные точки разбора ключа — два числовых поля посередине.

    Ручка `get_file_path` берёт из ключа папку, смещения и имя части. Слева
    (`split`) имя отъедала папка с двоеточием — «Входящие:Архив 2024» давала
    prefix='email_Входящие' и `int('Архив 2024')`, то есть 400 на письме,
    которое индексация к тому моменту уже разбирала успешно. Справа
    (`rsplit`) точно так же отъедало бы имя вложения с двоеточием.
    """
    match = _EMAIL_PATH_RE.match(key)

    assert match is not None, "ключ разбирается, а не отвергается как битый"
    assert (
        match["prefix"],
        match["start"],
        match["stop"],
        match["name"],
    ) == expected
    assert not match["prefix"].startswith("email_"), (
        "шаблон обязан снимать префикс сам: cfg.email_files отдаёт голые имена"
    )


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("email_INBOX:Архив 2024:body", id="non-numeric-start"),
        pytest.param("email_INBOX:100:двести:body", id="non-numeric-stop"),
        pytest.param("email_INBOX:100:250", id="no-part-name"),
        pytest.param("email_INBOX", id="folder-only"),
    ],
)
def test_email_path_pattern_rejects_malformed_keys(key: str):
    """Битый ключ шаблон не разбирает — ручка отвечает 400, а не 500.

    Раньше несовпадение вылезало как `ValueError` из `int()` внутри
    обработчика; теперь его ловит сам шаблон, и веток с int-ошибкой не
    осталось.
    """
    assert _EMAIL_PATH_RE.match(key) is None
