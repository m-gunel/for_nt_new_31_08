"""Разбор docx по OOXML (src/parsers/docx_ooxml.py) на docx, собранных в тесте.

Части пакета пишутся руками: так видно, какой элемент Word даёт какой текст.
"""

import zipfile
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.parsers import file_chunks
from src.parsers.doc_model import chunk_document, prepare
from src.parsers.docx import DocParser, DocxParser

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = f'xmlns:w="{W}"'


def _docx(
    path: Path, body: str, *, styles: str = "", numbering: str = "", footnotes: str = ""
) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        z.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>',
        )
        if styles:
            z.writestr(
                "word/styles.xml",
                f'<?xml version="1.0"?><w:styles {NS}>{styles}</w:styles>',
            )
        if numbering:
            z.writestr(
                "word/numbering.xml",
                f'<?xml version="1.0"?><w:numbering {NS}>{numbering}</w:numbering>',
            )
        if footnotes:
            z.writestr(
                "word/footnotes.xml",
                f'<?xml version="1.0"?><w:footnotes {NS}>{footnotes}</w:footnotes>',
            )
    return path


def p(
    text: str,
    *,
    style: str | None = None,
    num: tuple[int, int] | None = None,
    runs: str | None = None,
) -> str:
    ppr = ""
    if style or num:
        inner = f'<w:pStyle w:val="{style}"/>' if style else ""
        if num:
            inner += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
        ppr = f"<w:pPr>{inner}</w:pPr>"
    return f"<w:p>{ppr}{runs if runs is not None else f'<w:r><w:t>{text}</w:t></w:r>'}</w:p>"


def _texts(path: Path) -> str:
    return "\n".join(c["text"] for c in DocxParser().iter_chunks(str(path)))


HEADINGS = (
    '<w:style w:type="paragraph" w:styleId="H1"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="H2"><w:name w:val="heading 2"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
)


def test_word_numbering_is_restored(tmp_path):
    numbering = (
        '<w:abstractNum w:abstractNumId="0">'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>'
        "</w:abstractNum>"
        '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="russianLower"/><w:lvlText w:val="%1)"/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
    )
    body = (
        p("Общие положения", num=(1, 0))
        + p("Термины", num=(1, 1))
        + p("Сфера действия", num=(1, 1))
        + p("Порядок расчёта", num=(1, 0))
        + p("первый подпункт", num=(2, 0))
        + p("второй подпункт", num=(2, 0))
    )
    text = _texts(_docx(tmp_path / "n.docx", body, numbering=numbering))
    for line in ("1. Общие положения", "1.1. Термины", "1.2. Сфера действия", "2. Порядок расчёта",
                 "а) первый подпункт", "б) второй подпункт"):  # fmt: skip
        assert line in text


def test_headings_give_section_path_and_title(tmp_path):
    body = (
        p("Положение о закупках", style="Title")
        + p("Глава 1. Общие положения", style="H1")
        + p("1.1. Термины", style="H2")
        + p("Закупка — приобретение товаров и услуг для нужд банка.")
        + p("Глава 2. Порядок", style="H1")
        + p("Порядок проведения закупки определяет комиссия.")
    )
    path = _docx(tmp_path / "Положение.docx", body, styles=HEADINGS)
    with open(path, "rb") as f:
        doc = prepare(DocxParser().parse_io(f))
    # Маленький сплиттер: раздел у чанка — по его началу, а заголовок — граница абзаца.
    small = RecursiveCharacterTextSplitter(
        chunk_size=80, chunk_overlap=0, length_function=len
    )
    section = {
        c["text"].split("\n")[-1]: c.get("section") for c in chunk_document(doc, small)
    }
    assert section["Закупка — приобретение товаров и услуг для нужд банка."] == (
        "Глава 1. Общие положения › 1.1. Термины"
    )
    assert (
        section["Порядок проведения закупки определяет комиссия."] == "Глава 2. Порядок"
    )
    assert list(file_chunks(path))[0]["meta"]["title"] == "Положение о закупках"


def test_tracked_changes_are_accepted(tmp_path):
    runs = (
        '<w:r><w:t xml:space="preserve">Ставка </w:t></w:r>'
        "<w:del><w:r><w:delText>пять</w:delText></w:r></w:del>"
        "<w:ins><w:r><w:t>семь</w:t></w:r></w:ins>"
        '<w:r><w:t xml:space="preserve"> процентов</w:t></w:r>'
        "<w:moveFrom><w:r><w:t>старое место</w:t></w:r></w:moveFrom>"
        '<w:moveTo><w:r><w:t xml:space="preserve"> годовых</w:t></w:r></w:moveTo>'
    )
    text = _texts(_docx(tmp_path / "r.docx", p("", runs=runs)))
    assert "Ставка семь процентов годовых" in text
    assert "пять" not in text and "старое место" not in text


def _tc(text: str, props: str = "") -> str:
    return f"<w:tc>{f'<w:tcPr>{props}</w:tcPr>' if props else ''}<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


def test_table_with_merged_header_is_a_series_with_header(tmp_path):
    header1 = (
        "<w:tr>"
        + _tc("Показатель", '<w:vMerge w:val="restart"/>')
        + _tc("1 января 2025", '<w:gridSpan w:val="2"/>')
        + "</w:tr>"
    )
    header2 = (
        "<w:tr>" + _tc("", "<w:vMerge/>") + _tc("Рубли") + _tc("Валюта") + "</w:tr>"
    )
    rows = "".join(
        "<w:tr>"
        + _tc(f"Строка номер {i} с длинным названием показателя баланса")
        + _tc(f"{i}00,5")
        + _tc(f"{i}1,2")
        + "</w:tr>"
        for i in range(1, 61)
    )
    body = (
        p("Таблица 4. Структура активов")
        + f"<w:tbl>{header1}{header2}{rows}</w:tbl>"
        + p("Текст после таблицы.")
    )
    chunks = [
        c
        for c in DocxParser().iter_chunks(str(_docx(tmp_path / "t.docx", body)))
        if c["kind"] == "table"
    ]
    assert len(chunks) > 1  # длинная таблица — серия
    head = "Таблица 4. Структура активов\nПоказатель | 1 января 2025 / Рубли | 1 января 2025 / Валюта\n"
    assert all(c["text"].startswith(head) for c in chunks)
    assert {c["table_id"] for c in chunks} == {"t1"}
    assert chunks[0]["row_start"] == 3 and chunks[-1]["row_end"] == 62
    assert (
        "Строка номер 7 с длинным названием показателя баланса | 700,5 | 71,2"
        in "\n".join(c["text"] for c in chunks)
    )


def test_symbol_font_footnote_and_superscript(tmp_path):
    runs = (
        '<w:r><w:t xml:space="preserve">Норматив Н1 </w:t></w:r>'
        '<w:r><w:sym w:font="Symbol" w:char="F0B3"/></w:r>'
        '<w:r><w:t xml:space="preserve"> 8</w:t></w:r>'
        '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>1</w:t></w:r>'
        '<w:r><w:footnoteReference w:id="1"/></w:r>'
    )
    footnotes = '<w:footnote w:id="1"><w:p><w:r><w:t>С учётом надбавок.</w:t></w:r></w:p></w:footnote>'
    text = _texts(_docx(tmp_path / "s.docx", p("", runs=runs), footnotes=footnotes))
    assert "Норматив Н1 ≥ 8^1" in text  # «81» было бы другим числом
    assert "[сноска 1] С учётом надбавок." in text


def test_unexpected_failure_falls_back_to_docx2txt(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("непредвиденная структура")

    monkeypatch.setattr("src.parsers.docx.parse_docx", broken)
    assert "Текст документа" in _texts(_docx(tmp_path / "f.docx", p("Текст документа")))


def test_doc_goes_through_r7_into_docx_parser(tmp_path, monkeypatch):
    converted = _docx(
        tmp_path / "converted.docx",
        p("Глава 1. Итоги", style="H1") + p("Выручка выросла на треть."),
        styles=HEADINGS,
    )
    stub = tmp_path / "x2t"
    stub.write_text(f'#!/bin/sh\ncp "{converted}" "$2"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("LQRAG_R7_X2T", str(stub))
    legacy = tmp_path / "old.doc"
    legacy.write_bytes(
        b"\xd0\xcf\x11\xe0" + b"\0" * 64
    )  # содержимое неважно: «конвертирует» заглушка
    chunks = list(DocParser().iter_chunks(str(legacy)))
    assert chunks and chunks[0]["section"] == "Глава 1. Итоги"
    assert "Выручка выросла на треть." in chunks[0]["text"]
