"""Минимальные офисные файлы (docx, pptx, xlsx) для тестов чтения без офисных библиотек."""

from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZipFile


def minimal_docx(text: str) -> bytes:
    """Минимальный .docx, который читает docx2txt: zip с word/document.xml."""
    buf = BytesIO()
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p><w:r>'
        f"<w:t>{text}</w:t>"
        "</w:r></w:p></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    with ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def minimal_pptx(slides: list[list[str]]) -> bytes:
    """Минимальный .pptx для local_read_file: слайды с абзацами DrawingML.

    slides — по списку абзацев на слайд; номер слайда равен позиции в списке,
    чтобы тест мог отличить числовую сортировку slide10 от лексикографической.
    """
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        "</Types>"
    )
    buf = BytesIO()
    with ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        for number, paragraphs in enumerate(slides, 1):
            shapes = "".join(
                "<p:sp><p:txBody><a:p><a:r><a:t>%s</a:t></a:r></a:p>"
                "</p:txBody></p:sp>" % escape(paragraph)
                for paragraph in paragraphs
            )
            slide = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                "<p:sld "
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                "<p:cSld><p:spTree>%s</p:spTree></p:cSld></p:sld>" % shapes
            )
            zf.writestr("ppt/slides/slide%d.xml" % number, slide)
    return buf.getvalue()


_SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PK = "http://schemas.openxmlformats.org/package/2006/relationships"
_OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def minimal_xlsx(sheets: list[tuple[str, list[list]]]) -> bytes:
    """Минимальный .xlsx с настоящими workbook.xml и rels (имена листов).

    Ячейка в строке — либо str (общая строка, sharedStrings), либо число
    (числовая ячейка), либо кортеж ("inline", "текст") — inlineStr. Три вида
    покрывают все ветки разбора листа в мосте.
    """
    strings: list[str] = []
    plans: list[list[list[tuple]]] = []
    for _title, rows in sheets:
        plan_rows = []
        for row in rows:
            plan_row = []
            for cell in row:
                if isinstance(cell, tuple):
                    plan_row.append(("inline", cell[1]))
                elif isinstance(cell, str):
                    if cell not in strings:
                        strings.append(cell)
                    plan_row.append(("s", strings.index(cell)))
                else:
                    plan_row.append((None, str(cell)))
            plan_rows.append(plan_row)
        plans.append(plan_rows)

    def override(part: str, kind: str) -> str:
        return (
            '<Override PartName="/%s" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.%s+xml"/>' % (part, kind)
        )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + override("xl/workbook.xml", "sheet.main")
        + "".join(
            override("xl/worksheets/sheet%d.xml" % n, "worksheet")
            for n in range(1, len(sheets) + 1)
        )
        + "</Types>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="%s" xmlns:r="%s">'
        % (_SS, _OD)
        + "".join(
            '<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
            % (escape(title), number, number)
            for number, (title, _rows) in enumerate(sheets, 1)
        )
        + "</workbook>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s">'
        % _PK
        + "".join(
            '<Relationship Id="rId%d" Type="%s/worksheet" '
            'Target="worksheets/sheet%d.xml"/>' % (number, _OD, number)
            for number in range(1, len(sheets) + 1)
        )
        + "</Relationships>"
    )
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="%s"'
        % _SS
        + ' count="%d" uniqueCount="%d">' % (len(strings), len(strings))
        + "".join("<si><t>%s</t></si>" % escape(text) for text in strings)
        + "</sst>"
    )

    def sheet_xml(rows: list[list[tuple]]) -> str:
        lines = []
        for number, row in enumerate(rows, 1):
            cells = []
            column = ord("A")
            for kind, payload in row:
                ref = "%s%d" % (chr(column), number)
                column += 1
                if kind == "s":
                    cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, payload))
                elif kind == "inline":
                    cells.append(
                        '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>'
                        % (ref, escape(payload))
                    )
                else:
                    cells.append('<c r="%s"><v>%s</v></c>' % (ref, payload))
            lines.append('<row r="%d">%s</row>' % (number, "".join(cells)))
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
            % (_SS, "".join(lines))
        )

    buf = BytesIO()
    with ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        for number, rows in enumerate(plans, 1):
            zf.writestr("xl/worksheets/sheet%d.xml" % number, sheet_xml(rows))
    return buf.getvalue()
