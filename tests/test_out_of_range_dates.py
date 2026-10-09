"""Файл или письмо с датой вне диапазона хранилищ не обрывает проход.

mtime в LSM — беззнаковые 32 бита (`<QL`), даты в Lance — timestamp("ns")
(1677–2262). Дата до 1970 года или после 2106-го (копии с SMB, tar-архивы,
`touch -d`, письма с кривым Date) не должна ронять ни отметку в LSM, ни запись
батча: иначе проход обрывается на этом файле каждый раз, и всё, что стоит в
очереди после него, не индексируется.
"""

import os
import struct
from datetime import UTC, datetime

import aiofiles
import pyarrow as pa
import pytest
import pytest_asyncio as pa_fixture
from pytest import fixture

from src.common import FileMetadata, chunk_with_meta_schema
from src.common.doc_meta import (
    datetime_from_timestamp,
    doc_meta_schema,
    storable_datetime,
)
from src.config import Config, EmailConfig
from src.indexer.indexer import Index
from src.parsers import _process_email_chunks
from src.parsers.metadata import file_metadata
from src.thunderbird.lazy_iter_mbox import iter_emails_as_metadata
from src.utils.lsm_adapter import (
    LSMTable,
    _decode_file_meta,
    _encode_file_meta,
    check_meta_changed,
)
from tests.mbox_fixture import make_plain_email, make_profile

# 1969-12-31; 1601-01-01 (нулевой FILETIME с SMB); сразу за 2106-02-07;
# 2302 год (APFS зажимает до 2262-04-12).
BAD_MTIMES = [-86400, -11644473600, 2**32, 2**32 + 100, 10_500_000_000]


@pytest.mark.parametrize("mtime", BAD_MTIMES)
def test_lsm_encodes_out_of_range_mtime(mtime):
    data = _encode_file_meta(FileMetadata("/home/u/старый.txt", 10, mtime))

    stored = _decode_file_meta("/home/u/старый.txt", data)
    assert len(data) == 12, "формат записи LSM не меняется"
    assert 0 <= stored.mtime_seconds <= 2**32 - 1
    assert stored.size == 10


def test_lsm_format_unchanged_for_normal_mtime():
    """Существующие записи читаются как прежде: байты те же, что у `<QL`."""
    meta = FileMetadata("/home/u/отчёт.txt", 123, 1_700_000_000)
    assert _encode_file_meta(meta) == struct.pack("<QL", 123, 1_700_000_000)


@pytest.mark.asyncio
@pytest.mark.parametrize("mtime", [0, 1_700_000_000, 2**32 - 1])
async def test_lsm_record_of_previous_version_needs_no_reindex(tmp_path, mtime):
    """Состояние LSM, записанное прежней версией, читается без переиндексации."""
    path = tmp_path / "indexed.lsm"
    meta = FileMetadata("/home/u/отчёт.txt", 123, mtime)
    with LSMTable(path, encoder=_encode_file_meta, decoder=_decode_file_meta) as old:
        # Байты прежнего кодировщика, мимо _encode_file_meta.
        old.lsm[meta.path.encode()] = struct.pack("<QL", meta.size, meta.mtime_seconds)

    with LSMTable(path, encoder=_encode_file_meta, decoder=_decode_file_meta) as table:
        assert await table.async_get(meta.path) == meta
        assert not await check_meta_changed(table, meta), "файл не менялся"
        assert await check_meta_changed(table, meta._replace(size=124))


@pytest.mark.asyncio
@pytest.mark.parametrize("mtime", BAD_MTIMES)
async def test_out_of_range_mtime_is_not_changed_on_next_pass(tmp_path, mtime):
    """Иначе файл переразбирался бы и эмбеддился на каждом проходе."""
    meta = FileMetadata("/home/u/старый.txt", 10, mtime)
    with LSMTable(
        tmp_path / "indexed.lsm", encoder=_encode_file_meta, decoder=_decode_file_meta
    ) as table:
        await table.async_set(meta.path, meta)

        assert not await check_meta_changed(table, meta)
        assert await check_meta_changed(table, meta._replace(size=11))


def test_storable_datetime_bounds():
    assert datetime_from_timestamp(-11644473600) is None, "1601 год вне ns"
    assert datetime_from_timestamp(10**12) is None, "год > 9999 — не исключение"
    assert datetime_from_timestamp(0) is not None, "1970 год сохраняется как есть"
    assert storable_datetime(datetime(2262, 4, 12)) is None
    assert storable_datetime(datetime(1900, 1, 1)) == datetime(1900, 1, 1)
    assert storable_datetime(datetime(2261, 12, 31, tzinfo=UTC)) is not None


def test_file_metadata_with_far_future_mtime_fits_schema(tmp_path):
    path = tmp_path / "из_будущего.txt"
    path.write_text("текст", encoding="utf-8")
    os.utime(path, (10_500_000_000, 10_500_000_000))

    meta = file_metadata(str(path))

    assert meta["date_modified"] is None
    pa.array([meta], pa.struct(doc_meta_schema))  # без ArrowInvalid


@fixture
def mail_root(tmp_path):
    messages = [
        make_plain_email(
            "Из будущего",
            "тело письма с датой 2300 года",
            msg_id="future@fixture",
            date=datetime(2300, 1, 1, tzinfo=UTC),
        ),
        make_plain_email(
            "Из прошлого",
            "тело письма с датой 1601 года",
            msg_id="past@fixture",
            date=datetime(1601, 1, 2, tzinfo=UTC),
        ),
    ]
    return make_profile(tmp_path / "tb-profile", {"INBOX": messages})


@pytest.mark.asyncio
async def test_email_with_out_of_range_date_fits_storage(config: Config, mail_root):
    metas = [
        meta
        for _start, meta in iter_emails_as_metadata(
            str(mail_root / "INBOX"), prefix="email_INBOX:"
        )
        if meta is not None
    ]
    assert len(metas) == 2

    async def fail_cb(meta, *, permanent):
        raise AssertionError(f"письмо {meta.path} не разобрано")

    chunks = []
    async with aiofiles.open(mail_root / "INBOX", "rb") as f:
        for meta in metas:
            _encode_file_meta(meta)  # отметка в LSM без struct.error
            async for chunk in _process_email_chunks({"INBOX": f}, meta, fail_cb):
                chunks.append(chunk)

    assert chunks
    assert all(c["meta"]["date_created"] is None for c in chunks)
    pa.RecordBatch.from_pylist(chunks, schema=chunk_with_meta_schema)


@fixture(params=[-86400, 2**32 + 100, 10_500_000_000])
def bad_mtime_config(config: Config, tmp_path, monkeypatch, request):
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(6):
        (docs / f"f{i}.txt").write_text(
            f"Документ номер {i}. Квартальный отчёт по продажам. " * 40,
            encoding="utf-8",
        )
    bad = [e.name for e in os.scandir(docs)][1]  # второй в порядке обхода
    os.utime(docs / bad, (request.param, request.param))
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "index_refresh_delay", 0.05)
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(config, "email", EmailConfig(enabled=False))
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(docs)])
    # Маленький батч записи: файлы идут разными батчами, как на большом корпусе.
    monkeypatch.setattr(config, "parquet_record_batch_size", 2)
    config.__dict__.pop("lance", None)
    yield config
    config.__dict__.pop("lance", None)
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


@pa_fixture.fixture
async def bad_mtime_index(bad_mtime_config: Config):
    idx = Index(bad_mtime_config)
    async with idx:
        try:
            yield idx
        finally:
            if idx._index_task is not None:
                idx._index_task.cancel()


@pytest.mark.asyncio
async def test_pass_survives_file_with_out_of_range_mtime(bad_mtime_index: Index):
    await bad_mtime_index.scan_all()  # без struct.error и ArrowInvalid

    indexed = bad_mtime_index.lsm_collection.indexed.keys_snapshot()
    assert sorted(os.path.basename(k) for k in indexed) == [
        f"f{i}.txt" for i in range(6)
    ], "файлы после «плохого» обязаны проиндексироваться в том же проходе"
    rows = bad_mtime_index.files_store.table.count_rows()

    await bad_mtime_index.scan_all()

    assert bad_mtime_index.files_store.table.count_rows() == rows, (
        "второй проход не должен переразбирать файл с датой вне диапазона"
    )
