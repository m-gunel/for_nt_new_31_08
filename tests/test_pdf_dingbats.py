"""Знаки частной области F0xx в PDF: Symbol — буквы и знаки, Wingdings — маркеры."""

import pymupdf

from src.parsers import read_document_text
from src.parsers.pdf import _dingbats_to_bullets

# Код в шрифте → символ, который отдаёт MuPDF (ToUnicode как у Word: код + 0xF000).
_WINGDINGS = {0x76: 0xF076, 0xD8: 0xF0D8, 0x6C: 0xF06C, 0xFC: 0xF0FC}  # ❖ ➢ ● ✓
_SYMBOL = {0x61: 0xF061, 0xB3: 0xF0B3}  # α ≥


def _cmap(pairs: dict[int, int]) -> bytes:
    body = "\n".join(f"<{c:02X}> <{u:04X}>" for c, u in pairs.items())
    return (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        "/CMapName /X def 1 begincodespacerange <00> <FF> endcodespacerange "
        f"{len(pairs)} beginbfchar\n{body}\nendbfchar endcmap CMapName currentdict "
        "/CMap defineresource pop end end"
    ).encode()


def _stream(data: bytes) -> bytes:
    return b"<</Length %d>>stream\n" % len(data) + data + b"\nendstream"


def _pdf(lines: list[list[tuple[str, str]]]) -> bytes:
    """Страница из строк; сегмент — (шрифт, текст): F1 Helvetica, F2 Wingdings, F3 Symbol.

    У F2/F3 текст — hex-коды знаков. Собирается вручную: в PyMuPDF нет
    шрифтов Wingdings и Symbol с кодами частной области.
    """
    ops, y = [], 760
    for segs in lines:
        ops.append(f"BT 72 {y} Td")
        for font, s in segs:
            ops.append(
                f"/{font} 12 Tf " + (f"({s}) Tj" if font == "F1" else f"<{s}> Tj")
            )
        ops.append("ET")
        y -= 20
    font = "<</Type/Font/Subtype/TrueType/BaseFont/%s/ToUnicode %d 0 R>>"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R/F2 6 0 R/F3 7 0 R>>>>>>",
        _stream("\n".join(ops).encode()),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        (font % ("ABCDEF+Wingdings-Regular", 8)).encode(),
        (font % ("Symbol", 9)).encode(),
        _stream(_cmap(_WINGDINGS)),
        _stream(_cmap(_SYMBOL)),
    ]
    out, offsets = b"%PDF-1.4\n", []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return out


def test_wingdings_list_markers_become_bullets(tmp_path):
    path = tmp_path / "list.pdf"
    path.write_bytes(
        _pdf(
            [
                [("F2", "76"), ("F1", " Punkt odin")],
                [("F2", "D8"), ("F1", " Punkt dva")],
                [("F2", "6C"), ("F1", " Punkt tri")],
                [("F1", "Sdelano "), ("F2", "FC")],
                [("F3", "61"), ("F1", " = 0,05 "), ("F3", "B3"), ("F1", " 1")],
            ]
        )
    )
    assert read_document_text(path) == (
        "• Punkt odin\n• Punkt dva\n• Punkt tri\nSdelano •\nα = 0,05 ≥ 1"
    )


def test_symbol_only_page_is_unchanged(tmp_path):
    path = tmp_path / "formula.pdf"
    path.write_bytes(
        _pdf([[("F3", "61"), ("F1", " = 0,05 "), ("F3", "B3"), ("F1", " 1")]])
    )
    assert read_document_text(path) == "α = 0,05 ≥ 1"


def test_page_without_private_use_chars_is_not_inspected():
    class Page:
        def get_text(self, *args):
            raise AssertionError("страница без знаков F0xx не разбирается по шрифтам")

    text = "Обычный текст страницы • с маркером"
    assert _dingbats_to_bullets(Page(), text) is text


def test_plain_pdf_text_is_unchanged(tmp_path):
    path = tmp_path / "plain.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "Plain text, no symbols: 5 > 3")
        doc.save(str(path))
    assert read_document_text(path) == "Plain text, no symbols: 5 > 3"
