"""POST /api/v1/read_file: полный текст файла парсерами демона.

Хендлер зовётся напрямую (как update_config в test_config_reload): ручке
нужны только scan_paths и follow_symlinks для границы доступа и глобальный
конфиг парсеров.
Границы передаются подменным объектом, чтобы не мутировать session-конфиг,
общий для всех тестов.
"""

from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi import HTTPException

from src.indexer import scan_dir
from src.indexer.scan_dir import iter_file_metadata
from src.server.model import ReadFileRequest
from src.server.router import read_file
from src.utils.r7_office import find_x2t

FIXTURE_XLS = Path(__file__).parent / "fixtures" / "legacy.xls"


def _cfg_with_folders(*folders, follow_symlinks=True):
    # follow_symlinks=True — умолчание FileScanConfig, с ним работает поставка.
    return SimpleNamespace(
        scan_paths=list(folders),
        file_scan_cfg=SimpleNamespace(follow_symlinks=follow_symlinks),
    )


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
    """Главный сценарий ручки: pdf мост в поставке сам не читает."""
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
async def test_read_file_rejects_outside_and_traversal(config, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "вне-папок.txt"
    outside.write_text("не должно читаться", encoding="utf-8")

    for raw in (str(outside), str(allowed / ".." / "вне-папок.txt")):
        with pytest.raises(HTTPException) as caught:
            await read_file(
                ReadFileRequest(absolute_path=raw), _cfg_with_folders(str(allowed))
            )
        assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_read_file_reads_what_the_scan_reaches_through_symlinks(
    config, tmp_path, monkeypatch
):
    """Обход идёт по ссылкам и хранит путь через ссылку — поиск отдаёт его.

    Каждый путь, найденный iter_file_metadata при умолчаниях, ручка обязана
    прочитать, в том числе ведущий за ссылкой наружу корня.
    """
    monkeypatch.setattr(scan_dir, "network_mount_points", lambda: ())
    projects = tmp_path / "data" / "projects"
    projects.mkdir(parents=True)
    (projects / "a.txt").write_text("текст проекта", encoding="utf-8")
    home = tmp_path / "home"
    (home / "Документы").mkdir(parents=True)
    (home / "own.txt").write_text("свой файл", encoding="utf-8")
    (home / "Документы" / "Проекты").symlink_to(projects, target_is_directory=True)
    (home / "alias.txt").symlink_to(projects / "a.txt")

    found = sorted(m.path for m in iter_file_metadata(str(home), follow_symlinks=True))
    assert len(found) == 3, found
    for path in found:
        response = await read_file(
            ReadFileRequest(absolute_path=path), _cfg_with_folders(str(home))
        )
        assert response.text, path


@pytest.mark.asyncio
async def test_read_file_keeps_symlinks_closed_when_scan_skips_them(config, tmp_path):
    """follow_symlinks=false: обход по ссылкам не идёт — чтение через них тоже."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "вне-папок.txt"
    outside.write_text("не должно читаться", encoding="utf-8")
    link = allowed / "побег.txt"
    link.symlink_to(outside)
    with pytest.raises(HTTPException) as caught:
        await read_file(
            ReadFileRequest(absolute_path=str(link)),
            _cfg_with_folders(str(allowed), follow_symlinks=False),
        )
    assert caught.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rel",
    [
        "u/.config/gh/hosts.txt",
        "u/.local/share/Trash/files/удалённый.txt",
        "u/.cache/x/doc.txt",
        "u/snap/app/doc.txt",
        "u/node_modules/x/readme.txt",
    ],
)
async def test_read_file_refuses_files_under_hidden_and_service_dirs(
    config, tmp_path, rel
):
    """Корень — как поставочный /home: скрыто не имя файла, а каталог ниже корня.

    Обход такие каталоги пропускает (exclude_pattern), и ручка их не отдаёт.
    """
    root = tmp_path / "home"
    target = root / rel
    target.parent.mkdir(parents=True)
    target.write_text("token=FAKE_SECRET", encoding="utf-8")
    with pytest.raises(HTTPException) as caught:
        await read_file(
            ReadFileRequest(absolute_path=str(target)), _cfg_with_folders(str(root))
        )
    assert caught.value.status_code == 400
    assert "FAKE_SECRET" not in str(caught.value.detail)


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
