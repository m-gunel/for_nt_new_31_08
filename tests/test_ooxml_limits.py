"""Предел распаковки OOXML (src/parsers/ooxml_limits.py) и его применение читателями.

Лимиты в тестах понижены через monkeypatch: архивы маленькие, а правило то же.
Один тест — на настоящих лимитах: docx в сотни КиБ с document.xml больше 64 МиБ.
"""

import io
import os
import zipfile

import pytest

import mcp_server
from src.parsers import ooxml_limits
from src.parsers.base import SkipFile
from src.parsers.docx import DocxParser
from src.parsers.metadata import ooxml_properties
from src.parsers.pptx import PptxParser
from src.parsers.xlsx import XlsxParser

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PARA = b"<w:p><w:r><w:t>a</w:t></w:r></w:p>"


def _zip(parts: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    buf.seek(0)
    return buf


def _document(paras: int) -> bytes:
    head = f'<w:document xmlns:w="{W}"><w:body>'.encode()
    return head + PARA * paras + b"</w:body></w:document>"


@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setattr(ooxml_limits, "MAX_XML_BYTES", 10_000)
    monkeypatch.setattr(ooxml_limits, "MAX_UNPACKED_BYTES", 40_000)


def _forbid(monkeypatch, target: str):
    def boom(*_a, **_k):
        raise AssertionError(f"{target} не должен вызываться")

    monkeypatch.setattr(target, boom)


# --- правило ---------------------------------------------------------------


def test_rule_part_total_ratio_and_streamed(small_limits):
    reason = ooxml_limits.oversize_reason
    assert reason(_zip({"word/document.xml": _document(10)})) is None
    # XML-часть больше предела — отказ, даже при честном сжатии.
    big_xml = os.urandom(6_000).hex().encode()  # 12 000 Б, сжатие ~2:1
    assert "word/document.xml" in reason(_zip({"word/document.xml": big_xml}))
    # Медиа больше предела части — не XML, в дерево не идёт: только общий объём.
    assert reason(_zip({"ppt/media/v.bin": os.urandom(15_000)})) is None
    # Общий объём.
    parts = {f"ppt/media/i{n}.bin": os.urandom(9_000) for n in range(5)}
    assert "после распаковки" in reason(_zip(parts))
    # Лист xlsx обходится потоком: в общий объём не входит, большой — допустим ...
    sheet = "xl/worksheets/sheet1.xml"
    streamed = ooxml_limits.XLSX_STREAMED
    assert reason(_zip({sheet: os.urandom(30_000).hex().encode()}), streamed) is None
    # ... но не с коэффициентом сжатия zip-бомбы.
    assert "сжатие" in reason(_zip({sheet: b"<c/>" * 50_000}), streamed)


def test_rule_streams_worksheet_with_any_name(small_limits):
    """Лист под нестандартным именем openpyxl тоже обходит потоком."""
    big = os.urandom(30_000).hex().encode()
    book = _zip({"xl/worksheets/data.xml": big})
    assert ooxml_limits.oversize_reason(book, ooxml_limits.XLSX_STREAMED) is None


def test_rule_ignores_broken_zip_and_rewinds():
    f = io.BytesIO(b"PK\x03\x04 not a zip")
    f.seek(5)
    assert ooxml_limits.oversize_reason(f) is None
    assert f.tell() == 0
    ok = _zip({"word/document.xml": _document(1)})
    assert ooxml_limits.oversize_reason(ok) is None and ok.tell() == 0


# --- читатели демона ----------------------------------------------------------


def test_docx_zip_bomb_is_skipped_without_docx2txt(monkeypatch):
    """Настоящие лимиты: document.xml больше 64 МиБ при сжатом файле в сотни КиБ."""
    paras = ooxml_limits.MAX_XML_BYTES // len(PARA) + 1000
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        with z.open("word/document.xml", "w", force_zip64=True) as part:
            part.write(f'<w:document xmlns:w="{W}"><w:body>'.encode())
            for _ in range(paras // 10_000 + 1):
                part.write(PARA * 10_000)
            part.write(b"</w:body></w:document>")
    assert len(buf.getvalue()) < 2 * 1024 * 1024
    buf.seek(0)
    _forbid(monkeypatch, "src.parsers.docx.process")  # docx2txt
    _forbid(monkeypatch, "src.parsers.docx.parse_docx")
    _forbid(monkeypatch, "src.parsers.docx.ooxml_properties")
    with pytest.raises(SkipFile) as err:
        DocxParser().parse_io(buf)
    assert err.value.permanent is True
    assert "word/document.xml" in err.value.reason


def test_docx_memory_error_does_not_fall_back_to_docx2txt(monkeypatch):
    def oom(*_a, **_k):
        raise MemoryError

    monkeypatch.setattr("src.parsers.docx.parse_docx", oom)
    _forbid(monkeypatch, "src.parsers.docx.process")
    with pytest.raises(MemoryError):
        DocxParser().parse_io(_zip({"word/document.xml": _document(3)}))


def test_ooxml_properties_does_not_unpack_oversized_parts(monkeypatch):
    monkeypatch.setattr(ooxml_limits, "MAX_XML_BYTES", 10_000)
    core = (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:creator>Автор</dc:creator></cp:coreProperties>"
    ).encode()
    doc = _document(1000).replace(b"</w:body>", b"<w:lastRenderedPageBreak/></w:body>")
    read = []
    real_read = zipfile.ZipFile.read

    def spy(self, name, *a, **k):
        read.append(name)
        return real_read(self, name, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "read", spy)
    meta, _ = ooxml_properties(
        _zip({"docProps/core.xml": core, "word/document.xml": doc}), "docx"
    )
    assert "word/document.xml" not in read
    assert meta["author"] == "Автор" and meta.get("page_count") is None


def test_pptx_oversize_is_skipped_before_python_pptx(small_limits, monkeypatch):
    _forbid(monkeypatch, "src.parsers.pptx.Presentation")
    deck = _zip({"ppt/media/v.bin": os.urandom(50_000)})
    with pytest.raises(SkipFile) as err:
        PptxParser().parse_io(deck)
    assert err.value.permanent is True


def test_xlsx_oversize_is_skipped_before_openpyxl(small_limits, monkeypatch):
    _forbid(monkeypatch, "src.parsers.xlsx.openpyxl.load_workbook")
    bomb = _zip({"xl/sharedStrings.xml": b"<sst>" + b" " * 50_000 + b"</sst>"})
    with pytest.raises(SkipFile):
        XlsxParser().parse_io(bomb)
    # Сверх общего потолка — отказ и при честном сжатии.
    many = {f"xl/part{n}.xml": os.urandom(4_000).hex().encode() for n in range(6)}
    with pytest.raises(SkipFile):
        XlsxParser().parse_io(_zip(many))


def test_xlsx_big_shared_strings_with_normal_ratio_is_parsed(small_limits):
    """Общие строки выгрузки на сотни тысяч строк больше MAX_XML_BYTES, но не бомба."""
    shared = os.urandom(6_000).hex().encode()  # 12 000 Б, сжатие ~2:1
    assert ooxml_limits.oversize_reason(
        _zip({"xl/sharedStrings.xml": shared}),
        ooxml_limits.XLSX_STREAMED,
        ooxml_limits.XLSX_LARGE,
    ) is None


def test_docx_big_document_with_normal_ratio_falls_back_to_docx2txt(
    small_limits, monkeypatch
):
    """Большой document.xml без признаков бомбы читается docx2txt, как до лимитов."""
    monkeypatch.setattr("src.parsers.docx_ooxml.MAX_XML_BYTES", 10_000)
    words = " ".join(os.urandom(3).hex() for _ in range(1_500))
    doc = (
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>{words}</w:t></w:r></w:p>'
        "</w:body></w:document>"
    ).encode()
    assert len(doc) > 10_000
    parsed = DocxParser().parse_io(_zip({"word/document.xml": doc}))
    assert words.split()[-1] in parsed.blocks[0].text


def test_docx_media_does_not_count_to_unpacked_total(small_limits):
    """Картинки docx не читает ни parse_docx, ни docx2txt."""
    media = {f"word/media/image{n}.bmp": os.urandom(9_000) for n in range(6)}
    package = _zip({"word/document.xml": _document(3), **media})
    reason = ooxml_limits.oversize_reason(
        package, ooxml_limits.DOCX_UNREAD, ooxml_limits.DOCX_LARGE
    )
    assert reason is None


def test_xlsx_big_sheet_with_normal_ratio_is_still_parsed(tmp_path, monkeypatch):
    """Листы openpyxl читает потоком: большой лист без признаков бомбы разбирается."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Показатель", "Значение"])
    for n in range(2000):
        ws.append([f"строка {n} {os.urandom(4).hex()}", n * 1.5])
    path = tmp_path / "big.xlsx"
    wb.save(path)
    with zipfile.ZipFile(path) as z:
        sheet = z.getinfo("xl/worksheets/sheet1.xml").file_size
    monkeypatch.setattr(ooxml_limits, "MAX_XML_BYTES", sheet // 2)
    texts = [c["text"] for c in XlsxParser().iter_chunks(str(path))]
    assert any("строка 1999" in t for t in texts)


# --- мост ---------------------------------------------------------------------


def test_bridge_refuses_oversized_part(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "MAX_XML_BYTES", 10_000)
    path = tmp_path / "bomb.docx"
    path.write_bytes(_zip({"word/document.xml": _document(1000)}).getvalue())
    with pytest.raises(mcp_server.ToolError) as err:
        mcp_server._read_text(str(path), ".docx")
    assert "word/document.xml" in str(err.value)


def test_bridge_reads_big_part_with_normal_ratio(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "MAX_XML_BYTES", 10_000)
    words = " ".join(os.urandom(3).hex() for _ in range(1_500))
    doc = (
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>{words}</w:t></w:r></w:p>'
        "</w:body></w:document>"
    ).encode()
    path = tmp_path / "long.docx"
    path.write_bytes(_zip({"word/document.xml": doc}).getvalue())
    assert words.split()[-1] in mcp_server._read_text(str(path), ".docx")


def test_bridge_refuses_oversized_package(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "MAX_UNPACKED_BYTES", 40_000)
    slides = {f"ppt/slides/slide{n}.xml": b"<p/>" + b" " * 9_000 for n in range(1, 6)}
    path = tmp_path / "deck.pptx"
    path.write_bytes(_zip(slides).getvalue())
    with pytest.raises(mcp_server.ToolError):
        mcp_server._read_text(str(path), ".pptx")
