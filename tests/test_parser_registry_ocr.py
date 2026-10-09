"""Реестр парсеров и OCR: исходы, порча zip, кэш и остановка воркера."""

import io
import json
import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path

import pymupdf
import pytest

from src.common import FileMetadata
from src.indexer.indexer import Index
from src.parsers import _process_path, document_chunks, ocr
from src.parsers.base import SkipFile, get_parser
from src.parsers.metadata import ooxml_properties
from tests.test_indexing_live import index, isolated_config  # noqa: F401

# ---------------------------------------------------------------- T096


def _docx_bytes(method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=method) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats'
            '.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + "Текст договора " * 50
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def _broken_member(data: bytes, name: str, how: str) -> bytes:
    """Испортить данные члена архива при целом центральном каталоге."""
    raw = bytearray(data)
    info = zipfile.ZipFile(io.BytesIO(data)).getinfo(name)
    fname_len, extra_len = struct.unpack(
        "<HH", raw[info.header_offset + 26 : info.header_offset + 30]
    )
    start = info.header_offset + 30 + fname_len + extra_len
    if how == "data":  # блок deflate с зарезервированным типом → zlib.error
        raw[start : start + info.compress_size] = b"\xff" * info.compress_size
    elif how == "method":  # метод сжатия 99 (AES) → NotImplementedError
        raw[info.header_offset + 8 : info.header_offset + 10] = struct.pack("<H", 99)
        cd = raw.find(b"PK\x01\x02")
        while cd != -1:  # та же запись в центральном каталоге
            n = struct.unpack("<H", raw[cd + 28 : cd + 30])[0]
            if raw[cd + 46 : cd + 46 + n] == name.encode():
                raw[cd + 10 : cd + 12] = struct.pack("<H", 99)
            cd = raw.find(b"PK\x01\x02", cd + 4)
    return bytes(raw)


@pytest.mark.parametrize("how", ["data", "method"])
@pytest.mark.asyncio
async def test_corrupt_zip_member_is_permanent_skip(tmp_path, caplog, how):
    path = tmp_path / "договор.docx"
    path.write_bytes(_broken_member(_docx_bytes(), "word/document.xml", how))
    stat = path.stat()
    calls: list[bool] = []

    async def fail_cb(_meta, *, permanent: bool) -> None:
        calls.append(permanent)

    with caplog.at_level("WARNING"):
        async for _ in _process_path(
            FileMetadata(str(path), stat.st_size, int(stat.st_mtime)), fail_cb=fail_cb
        ):
            pass
    assert calls == [True]
    assert all(r.exc_info is None for r in caplog.records), "порча — без стека"
    assert any("повреждено" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc", [zlib.error("bad"), EOFError(), NotImplementedError()])
def test_zip_payload_errors_become_skipfile(tmp_path, monkeypatch, exc):
    path = tmp_path / "a.txt"
    path.write_text("текст")
    parser = get_parser("txt")

    def broken(_file):
        raise exc

    monkeypatch.setattr(parser, "parse_io", broken)
    with pytest.raises(SkipFile) as err:
        list(document_chunks(path, parser))
    assert err.value.permanent


def test_ooxml_properties_survives_corrupt_member():
    data = _broken_member(_docx_bytes(), "word/document.xml", "data")
    assert ooxml_properties(io.BytesIO(data), "docx") == ({}, [])


# ---------------------------------------------------------------- T018


@pytest.mark.asyncio
async def test_reset_index_removes_ocr_cache(index: Index):  # noqa: F811
    cache = Path(index.cfg.lsm_root) / "ocr"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "abc.json").write_text('{"1": "паспорт 4510 123456"}', encoding="utf-8")

    await index.reset_index()

    assert not cache.exists(), "распознанный текст сканов пережил сброс"


# ---------------------------------------------------------------- T056, T103


def _scan_pdf(path: Path) -> Path:
    """Страница-картинка без текстового слоя: needs_ocr → True."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), False)
        pix.clear_with(180)
        page.insert_image(page.rect, pixmap=pix)
        doc.save(str(path))
    return path


_FAKE_TEXT = "Распознанный текст страницы скана договора поставки. " * 3


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    """Воркер-заглушка: сразу «распознаёт» страницу 1; кэш — во временной папке."""
    script = (
        "import sys, json; sys.stdin.read(); "
        f"print(json.dumps({{'page': 1, 'text': {_FAKE_TEXT!r}}}))"
    )
    monkeypatch.setattr(
        ocr, "_worker_cmd", lambda: ([sys.executable, "-c", script], None)
    )
    monkeypatch.setattr(ocr, "_tessdata", lambda cfg: tmp_path)
    cache = tmp_path / "ocr_cache"
    monkeypatch.setattr(ocr, "_cache_file", lambda sha: cache / f"{sha}.json")
    started: list = []
    real_popen = ocr.subprocess.Popen

    def popen(*args, **kwargs):
        started.append(args)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(ocr.subprocess, "Popen", popen)
    return started


async def _parse(path: Path) -> tuple[list, list[bool]]:
    stat = path.stat()
    calls: list[bool] = []

    async def fail_cb(_meta, *, permanent: bool) -> None:
        calls.append(permanent)

    chunks = [
        c
        async for c in _process_path(
            FileMetadata(str(path), stat.st_size, int(stat.st_mtime)), fail_cb=fail_cb
        )
    ]
    return chunks, calls


@pytest.mark.asyncio
async def test_worker_not_started_after_stop(tmp_path, monkeypatch, fake_worker):
    """Остановка пришла, пока поток разбора ещё извлекал текст страниц."""
    import src.parsers.pdf as pdf

    real_needs_ocr = pdf.needs_ocr

    def needs_ocr_then_stop(page, text):
        ocr.kill_running()  # stop_index: cancel() + kill_running()
        return real_needs_ocr(page, text)

    monkeypatch.setattr(pdf, "needs_ocr", needs_ocr_then_stop)
    chunks, calls = await _parse(_scan_pdf(tmp_path / "скан.pdf"))
    assert fake_worker == [], "воркер OCR запущен после остановки индексации"
    assert chunks == [] and calls == [False]


@pytest.mark.asyncio
async def test_new_pass_after_stop_still_runs_ocr(tmp_path, fake_worker):
    ocr.kill_running()  # прошлая остановка не выключает OCR для нового прохода
    chunks, calls = await _parse(_scan_pdf(tmp_path / "скан.pdf"))
    assert len(fake_worker) == 1 and calls == []
    assert any("Распознанный текст" in c["text"] for c in chunks)


@pytest.mark.asyncio
async def test_cache_write_failure_keeps_recognized_text(
    tmp_path, monkeypatch, fake_worker, caplog
):
    def no_space(sha, pages):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ocr, "_save", no_space)
    with caplog.at_level("WARNING"):
        chunks, calls = await _parse(_scan_pdf(tmp_path / "скан.pdf"))
    assert calls == [] and any("Распознанный текст" in c["text"] for c in chunks)
    assert any("кэш не записан" in r.getMessage() for r in caplog.records)


def test_cache_tmp_name_is_unique_per_write(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_cache_file", lambda sha: tmp_path / f"{sha}.json")
    sources: list[str] = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        sources.append(str(src))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    ocr._save("abc", {1: "первый"})
    ocr._save("abc", {1: "второй"})
    assert len(set(sources)) == 2, "два сохранения делят один временный файл"
    assert json.loads((tmp_path / "abc.json").read_text()) == {"1": "второй"}
    assert [p.name for p in tmp_path.iterdir()] == ["abc.json"]
