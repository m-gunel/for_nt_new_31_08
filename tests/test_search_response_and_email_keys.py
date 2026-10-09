"""Одна строка данных не должна гасить целую функцию.

Колонка `meta` в схеме чанка nullable (src/common/doc_meta.py), и строка с
`meta=None` не должна ронять весь POST /search в 500.

Ключ письма — `email_<папка>:<start>:<stop>`, а имя IMAP-папки само может
содержать двоеточие («Входящие:Архив 2024»). Ключ разбирают и индексация
(`_process_email_chunks`), и ручка `POST /api/v1/get_file_path`
(`_EMAIL_PATH_RE`). `_process_email_chunks` зовётся напрямую: снаружи
`async_parse_files` гасит исключение письма в `fail_cb(permanent=False)`, и
сбой разбора неотличим от штатного повтора.

Модель эмбеддингов не поднимается: mbox подставной, а сплиттер подменён —
настоящий меряет длину токенизатором OpenVINO.
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

_FILE_PATH = "/home/user/Документы/Договор поставки.pdf"


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
    версии, а колонка из одних None доезжает до `from_dataframes` как None при
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

    Строка без meta идёт первой и отдельным кадром: `from_dataframes`
    склеивает кадры по порядку, а колонка из одних None доезжает до модели как
    None при любой версии polars.
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
    """Письмо без метаданных не падает и не становится «файлом».

    Тип определяется по пути, а не по meta; титул — сам ключ письма.
    """
    key = "email_Входящие:Архив 2024:100:250:body"
    frame = _frame_with_null_meta_column(key, "текст письма без метаданных", 0.5)

    response = SearchResponse.from_dataframes([frame])

    (item,) = response.results
    assert item.type == "email"
    assert item.title == key
    assert item.id == key


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
    """Подставной mbox с интерфейсом aiofiles: `seeks`/`reads` показывают,
    по какому смещению искали письмо."""

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
    """Сплиттер-заглушка: настоящий `cfg.embed_splitter` поднимает OpenVINO.

    Значение cached_property лежит в `__dict__` инстанса, и monkeypatch.setitem
    подменяет его с откатом.
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
        "INBOX",  # контроль: обычная папка
        "Входящие:Архив 2024",
        "Проекты:2024:Q1",
    ],
)
async def test_email_key_offsets_are_the_last_two_fields(
    one_chunk_splitter: Config, folder: str
):
    """Смещения письма — два последних поля ключа, всё до них — имя папки.

    Иначе письмо из папки с двоеточием падает на разборе, а внешний `except`
    в `async_parse_files` превращает это в вечный повтор.
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

    Гейт по размеру срабатывает до чтения mbox: письмо из папки с двоеточием
    помечается permanent=True (меньше оно не станет) и не читается вовсе.
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
    # Иначе чтение с нулевой позиции (без seek) прошло бы незамеченным:
    # разбор мусора тоже не даёт чанков.
    assert mbox.reads == [], "отсечённое по размеру письмо не должно читаться"
    assert fails.calls == [(path, True)], (
        "permanent=True: письмо не станет меньше, возвращаться к нему незачем"
    )


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

    Ручка `get_file_path` берёт из ключа папку, смещения и имя части. Разбор
    слева ломается на папке с двоеточием, справа — на имени вложения с ним.
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
    """Битый ключ шаблон не разбирает — ручка отвечает 400, а не 500."""
    assert _EMAIL_PATH_RE.match(key) is None
