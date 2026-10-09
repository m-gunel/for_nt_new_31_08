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


def test_typical_document_chunks_are_unchanged(tmp_path):
    """Обычные таблицы и заголовки: правки длинной шапки и потока заголовков их не трогают."""
    header1 = (
        "<w:tr>"
        + _tc("Услуга", '<w:vMerge w:val="restart"/>')
        + _tc("Стоимость, руб.", '<w:gridSpan w:val="2"/>')
        + "</w:tr>"
    )
    header2 = (
        "<w:tr>"
        + _tc("", "<w:vMerge/>")
        + _tc("В отделении")
        + _tc("Онлайн")
        + "</w:tr>"
    )
    rows = "".join(
        "<w:tr>" + _tc(u) + _tc(a) + _tc(b) + "</w:tr>"
        for u, a, b in [
            ("Открытие счёта", "0", "0"),
            ("Перевод в другой банк", "50", "25"),
            ("Выписка по счёту", "100", "бесплатно"),
        ]
    )
    simple = "".join(
        "<w:tr>" + _tc(a) + _tc(b) + "</w:tr>"
        for a, b in [("Тариф", "Ставка"), ("Базовый", "1%"), ("Премиальный", "0,5%")]
    )
    body = (
        p("Тарифы банка для частных клиентов", style="Title")
        + p("Глава 1. Расчётное обслуживание", style="H1")
        + p(
            "Тарифы действуют с первого числа месяца и применяются ко всем счетам клиента."
        )
        + p("Таблица 1. Стоимость операций")
        + f"<w:tbl>{header1}{header2}{rows}</w:tbl>"
        + p("1.1. Переводы", style="H2")
        + f"<w:tbl>{simple}</w:tbl>"
        + p("Комиссия списывается в день операции.")
    )
    path = _docx(tmp_path / "тарифы.docx", body, styles=HEADINGS)
    chunks = [
        (c["kind"], c.get("section"), c["text"])
        for c in DocxParser().iter_chunks(str(path))
    ]
    chapter = "Глава 1. Расчётное обслуживание"
    assert chunks == [
        (
            "text",
            None,
            "Тарифы банка для частных клиентов\n\nГлава 1. Расчётное обслуживание\n"
            "Тарифы действуют с первого числа месяца и применяются ко всем счетам "
            "клиента.\nТаблица 1. Стоимость операций",
        ),
        (
            "table",
            chapter,
            "Таблица 1. Стоимость операций\n"
            "Услуга | Стоимость, руб. / В отделении | Стоимость, руб. / Онлайн\n"
            "Открытие счёта | 0 | 0\nПеревод в другой банк | 50 | 25\n"
            "Выписка по счёту | 100 | бесплатно",
        ),
        (
            "table",
            f"{chapter} › 1.1. Переводы",
            "1.1. Переводы\nТариф | Ставка\nБазовый | 1%\nПремиальный | 0,5%",
        ),
        ("text", f"{chapter} › 1.1. Переводы", "Комиссия списывается в день операции."),
    ]


_PASSPORT = (
    "Настоящий регламент определяет порядок взаимодействия подразделений банка "
    "при рассмотрении заявок клиентов на получение кредитных продуктов, "
    "устанавливает сроки, ответственных лиц и требования к комплекту документов, "
    "а также порядок контроля и отчётности. "
)


def test_long_first_table_row_is_not_repeated_per_word(tmp_path, config):
    """Первая строка таблицы — абзац больше бюджета: он идёт один раз, а не в каждом слове."""
    count = config.ov_model.count_tokens
    long_row = _PASSPORT * 16 + "Паспорт утверждён правлением банка."
    rows = [("1. Общие положения", long_row)] + [
        (f"{i}. Раздел {i}", f"Подразделение {i} направляет пакет документов "
         f"в кредитный комитет в течение {i} рабочих дней и фиксирует отправку.")
        for i in range(2, 22)
    ]  # fmt: skip
    table = "".join("<w:tr>" + _tc(a) + _tc(b) + "</w:tr>" for a, b in rows)
    path = _docx(tmp_path / "паспорт.docx", f"<w:tbl>{table}</w:tbl>")
    chunks = [c["text"] for c in DocxParser().iter_chunks(str(path))]
    assert count(long_row) > 450  # шапка сама больше бюджета чанка
    assert len(chunks) <= 25  # без правки — сотни чанков «шапка + слово»
    assert all(count(t) <= 450 for t in chunks)
    joined = "\n".join(chunks)
    assert "Паспорт утверждён правлением банка." in joined  # шапка целиком в индексе
    for a, b in rows[1:]:
        assert f"{a} | {b}" in joined
    # В каждом чанке — начало шапки: строка не теряет контекст таблицы.
    assert all(t.startswith("1. Общие положения | Настоящий регламент") for t in chunks)


def test_wide_form_with_three_level_header(tmp_path, config):
    """Форма ЦБ: 16 столбцов, шапка в три уровня — несколько чанков, а не тысячи."""
    count = config.ov_model.count_tokens
    top = (
        "Сведения о кредитах, предоставленных юридическим лицам и индивидуальным "
        "предпринимателям, в разрезе видов экономической деятельности, тыс. руб."
    )
    groups = [
        "Кредиты, предоставленные в рублях, с учётом просроченной задолженности",
        "Кредиты, предоставленные в иностранной валюте и драгоценных металлах",
        "Просроченная задолженность по основному долгу и процентам",
        "Резервы на возможные потери по ссудам, сформированные фактически",
    ]
    leaf = [
        "всего",
        "в том числе субъектам МСП",
        "сроком до 1 года",
        "сроком свыше 1 года",
    ]
    span = '<w:gridSpan w:val="{}"/>'
    head = (
        "<w:tr>" + _tc(top, span.format(16)) + "</w:tr>"
        + "<w:tr>" + "".join(_tc(g, span.format(4)) for g in groups) + "</w:tr>"
        + "<w:tr>" + "".join(_tc(x) for _ in groups for x in leaf) + "</w:tr>"
    )  # fmt: skip
    body = "".join(
        "<w:tr>" + "".join(_tc(str(100000 + i * 37 + k)) for k in range(16)) + "</w:tr>"
        for i in range(40)
    )
    path = _docx(
        tmp_path / "форма.docx",
        p("Таблица 3. Структура кредитного портфеля") + f"<w:tbl>{head}{body}</w:tbl>",
    )
    tables = [
        c["text"] for c in DocxParser().iter_chunks(str(path)) if c["kind"] == "table"
    ]
    assert len(tables) <= 15  # без правки — 683 чанка
    assert all(count(t) <= 450 for t in tables)
    assert all(
        t.startswith("Таблица 3. Структура кредитного портфеля\n") for t in tables
    )
    joined = "\n".join(tables)
    assert all(g in joined for g in groups)  # подписи граф — в индексе
    assert "100000 | 100001" in joined and "101457 | 101458" in joined


REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rels(*rels: tuple[str, str], extra: str = "") -> str:
    items = "".join(
        f'<Relationship Id="r{n}" Type="{REL}/{kind}" Target="{target}"/>'
        for n, (kind, target) in enumerate(rels)
    )
    return (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        f'package/2006/relationships">{items}{extra}</Relationships>'
    )


def _word_package(path: Path, *, rels: bool) -> Path:
    """docx в раскладке Word: связи пакета, стили, нумерация, сноски, колонтитулы."""
    numbering = (
        '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    footnotes = '<w:footnote w:id="1"><w:p><w:r><w:t>Протокол № 5.</w:t></w:r></w:p></w:footnote>'
    note = '<w:r><w:t>Регламент утверждён правлением.</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r>'
    table = "".join(
        "<w:tr>" + _tc(a) + _tc(b) + "</w:tr>"
        for a, b in [("Тариф", "Ставка"), ("Базовый", "1%"), ("Премиальный", "0,5%")]
    )
    body = (
        p("Регламент обслуживания клиентов", style="Title")
        + p("Глава 1. Общие положения", style="H1")
        + p("Термины", num=(1, 0))
        + p("Сфера действия", num=(1, 0))
        + p("", runs=note)
        + p("Таблица 1. Ставки")
        + f"<w:tbl>{table}</w:tbl>"
        + p("Глава 2. Порядок", style="H1")
        + p("Заявка рассматривается в течение трёх дней.")
    )
    _docx(path, body, styles=HEADINGS, numbering=numbering, footnotes=footnotes)
    core = (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:creator>Иванов И.И.</dc:creator></cp:coreProperties>"
    )
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("docProps/core.xml", core)
        for part, text in (
            ("header1", "ПАО Банк — внутренний документ"),
            ("footer1", "Страница 1"),
        ):
            tag = "hdr" if part.startswith("header") else "ftr"
            z.writestr(f"word/{part}.xml", f"<w:{tag} {NS}>{p(text)}</w:{tag}>")
        if rels:
            core_rel = (
                '<Relationship Id="c" Type="http://schemas.openxmlformats.org/package/'
                '2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            )
            link = (
                f'<Relationship Id="l" Type="{REL}/hyperlink" '
                'Target="https://example.org/" TargetMode="External"/>'
            )
            z.writestr(
                "_rels/.rels",
                _rels(("officeDocument", "word/document.xml"), extra=core_rel),
            )
            z.writestr(
                "word/_rels/document.xml.rels",
                _rels(
                    ("styles", "styles.xml"),
                    ("numbering", "numbering.xml"),
                    ("footnotes", "footnotes.xml"),
                    ("header", "header1.xml"),
                    ("footer", "footer1.xml"),
                    extra=link,
                ),
            )
    return path


def test_typical_word_package_text_is_unchanged(tmp_path):
    """Обычный docx в раскладке Word: текст, разделы и свойства прежние, со связями и без."""
    chapter1, chapter2 = "Глава 1. Общие положения", "Глава 2. Порядок"
    expected = [
        (
            "text",
            None,
            "ПАО Банк — внутренний документ\nРегламент обслуживания клиентов\n\n"
            f"{chapter1}\n1. Термины\n2. Сфера действия\n"
            "Регламент утверждён правлением.\n[сноска 1] Протокол № 5.\n"
            "Таблица 1. Ставки",
        ),
        (
            "table",
            chapter1,
            "Таблица 1. Ставки\nТариф | Ставка\nБазовый | 1%\nПремиальный | 0,5%",
        ),
        ("text", chapter2, f"{chapter2}\nЗаявка рассматривается в течение трёх дней."),
    ]
    for rels in (True, False):
        path = _word_package(tmp_path / f"регламент-{rels}.docx", rels=rels)
        chunks = [
            (c["kind"], c.get("section"), c["text"])
            for c in DocxParser().iter_chunks(str(path))
        ]
        assert chunks == expected
        meta = list(file_chunks(path))[0]["meta"]
        assert (meta["title"], meta["author"]) == (
            "Регламент обслуживания клиентов",
            "Иванов И.И.",
        )


def test_main_part_and_styles_found_by_relationships(tmp_path):
    """Основная часть не обязана называться word/document.xml: её задаёт _rels/.rels."""
    path = tmp_path / "d2.docx"
    body = p("Глава 1. Итоги", style="H1") + p("Выручка выросла на треть.")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("_rels/.rels", _rels(("officeDocument", "/word/document2.xml")))
        z.writestr("word/_rels/document2.xml.rels", _rels(("styles", "styles2.xml")))
        z.writestr(
            "word/document2.xml",
            f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>',
        )
        z.writestr(
            "word/styles2.xml",
            f'<?xml version="1.0"?><w:styles {NS}>{HEADINGS}</w:styles>',
        )
    chunks = list(DocxParser().iter_chunks(str(path)))
    assert chunks and chunks[0]["section"] == "Глава 1. Итоги"
    assert "Выручка выросла на треть." in chunks[0]["text"]


def test_broken_package_rels_fall_back_to_word_names(tmp_path, monkeypatch):
    def no_docx2txt(*_args, **_kwargs):
        raise AssertionError("битый .rels не должен уводить в docx2txt")

    monkeypatch.setattr("src.parsers.docx.process", no_docx2txt)
    path = _docx(tmp_path / "b.docx", p("Текст документа"))
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("_rels/.rels", "<Relationships")  # обрыв XML
    assert "Текст документа" in _texts(path)


def test_relationship_to_missing_part_falls_back_to_word_names(tmp_path):
    """Цель связи не нашлась в архиве (регистр имени) — стили берутся по имени Word."""
    path = _docx(
        tmp_path / "c.docx",
        p("Глава 1. Итоги", style="H1") + p("Выручка выросла на треть."),
        styles=HEADINGS,
    )
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("_rels/.rels", _rels(("officeDocument", "word/document.xml")))
        z.writestr("word/_rels/document.xml.rels", _rels(("styles", "Styles.xml")))
    chunks = list(DocxParser().iter_chunks(str(path)))
    assert chunks and chunks[0]["section"] == "Глава 1. Итоги"
