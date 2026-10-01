"""POST /api/v1/read_file: полный текст файла парсерами демона.

Хендлер зовётся напрямую (как update_config в test_config_reload): ручке
нужны только scan_paths для границы доступа и глобальный конфиг парсеров.
Границы передаются подменным объектом, чтобы не мутировать session-конфиг,
общий для всех тестов.
"""

from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi import HTTPException

from src.server.model import ReadFileRequest
from src.server.router import read_file
from src.utils.r7_office import find_x2t

FIXTURE_XLS = Path(__file__).parent / "fixtures" / "legacy.xls"


def _cfg_with_folders(*folders):
    return SimpleNamespace(scan_paths=list(folders))


@pytest.mark.asyncio
async def test_read_file_returns_text(config, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    # Демон индексирует только свои форматы: txt — единственный текстовый.
    sample = docs / "заметка.txt"
    sample.write_text("Контрольная фраза текстового файла", encoding="utf-8")
    response = await read_file(
        ReadFileRequest(absolute_path=str(sample)), _cfg_with_folders(str(docs))
    )
    assert "Контрольная фраза" in response.text
    assert response.truncated is False


@pytest.mark.asyncio
async def test_read_file_pdf_via_bundled_pymupdf(config, tmp_path):
    """Ради этой ручки всё и затевалось: pdf в поставке недоступен мосту."""
    docs = tmp_path / "docs"
    docs.mkdir()
    pdf_path = docs / "sample.pdf"
    # Base-шрифты PyMuPDF латинские — контрольная фраза тоже латиницей.
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "pdf control phrase 402")
        document.save(str(pdf_path))
    response = await read_file(
        ReadFileRequest(absolute_path=str(pdf_path)), _cfg_with_folders(str(docs))
    )
    assert "pdf control phrase 402" in response.text


@pytest.mark.asyncio
@pytest.mark.skipif(
    find_x2t() is None or not FIXTURE_XLS.exists(),
    reason="Р7-Офис не установлен или нет фикстуры legacy.xls",
)
async def test_read_file_legacy_xls_via_r7(config, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    legacy = docs / "старина.xls"
    legacy.write_bytes(FIXTURE_XLS.read_bytes())
    response = await read_file(
        ReadFileRequest(absolute_path=str(legacy)), _cfg_with_folders(str(docs))
    )
    joined = response.text
    assert "Контрольная фраза листа Отчёт" in joined


@pytest.mark.asyncio
async def test_read_file_rejects_outside_and_symlink_escape(config, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "вне-папок.txt"
    outside.write_text("не должно читаться", encoding="utf-8")

    with pytest.raises(HTTPException) as caught:
        await read_file(
            ReadFileRequest(absolute_path=str(outside)), _cfg_with_folders(str(allowed))
        )
    assert caught.value.status_code == 400

    # Симлинк из индексируемой папки наружу не должен расширять песочницу.
    link = allowed / "побег.txt"
    link.symlink_to(outside)
    with pytest.raises(HTTPException) as caught:
        await read_file(
            ReadFileRequest(absolute_path=str(link)), _cfg_with_folders(str(allowed))
        )
    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_read_file_rejects_hidden_unknown_missing(config, tmp_path):
    allowed = tmp_path / "d"
    allowed.mkdir()

    hidden = allowed / ".тайна.md"
    hidden.write_text("скрытое", encoding="utf-8")
    with pytest.raises(HTTPException) as caught:
        await read_file(
            ReadFileRequest(absolute_path=str(hidden)), _cfg_with_folders(str(allowed))
        )
    assert caught.value.status_code == 400

    unknown = allowed / "данные.bin"
    unknown.write_bytes(b"\x00\x01\x02")
    with pytest.raises(HTTPException) as caught:
        await read_file(
            ReadFileRequest(absolute_path=str(unknown)), _cfg_with_folders(str(allowed))
        )
    assert caught.value.status_code == 415

    with pytest.raises(HTTPException) as caught:
        await read_file(
            ReadFileRequest(absolute_path=str(allowed / "нет-файла.md")),
            _cfg_with_folders(str(allowed)),
        )
    assert caught.value.status_code == 404
