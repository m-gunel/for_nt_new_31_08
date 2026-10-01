"""Разбор pptx (src/parsers/pptx.py) на презентациях, собранных python-pptx в тесте."""

from pathlib import Path

from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Inches

from src.parsers import file_chunks


def _box(shapes, text: str, top: int = 1):
    box = shapes.add_textbox(Inches(1), Inches(top), Inches(6), Inches(1))
    box.text_frame.text = text
    return box


def _deck(path: Path) -> Path:
    prs = Presentation()
    blank = prs.slide_layouts[6]
    s1 = prs.slides.add_slide(prs.slide_layouts[5])  # только заголовок
    s1.shapes.title.text = "Кредиты для бизнеса"
    group = s1.shapes.add_group_shape()
    _box(group.shapes, "Ставка от 12 процентов годовых для малого бизнеса", 2)
    _box(group.shapes, "Срок кредита до пяти лет без залога", 3)
    s1.notes_slide.notes_text_frame.text = "Подчеркнуть отсутствие залога при выдаче"
    s2 = prs.slides.add_slide(blank)
    _box(s2.shapes, "Тарифы по отделениям")
    table = s2.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(6), Inches(2)).table
    for r, row in enumerate(
        [
            ["Отделение", "Код", "Телефон"],
            ["Москва, Тверская 1", "ZX01", "495-111"],
            ["Казань, Баумана 5", "ZX02", "843-222"],
        ]
    ):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    s3 = prs.slides.add_slide(blank)
    steps = _box(s3.shapes, "Подать заявку онлайн").text_frame
    steps.add_paragraph().text = "Получить решение за день"
    steps.add_paragraph().text = "Подписать договор в отделении"
    for para in steps.paragraphs:
        para._p.get_or_add_pPr().append(
            para._p.makeelement(qn("a:buAutoNum"), {"type": "arabicPeriod"})
        )
    prs.save(str(path))
    return path


def test_groups_tables_notes_and_slide_numbers(tmp_path):
    rows = list(file_chunks(_deck(tmp_path / "кредиты.pptx")))
    joined = "\n".join(r["text"] for r in rows)
    # Текст фигур внутри группы прежний парсер терял.
    assert "Ставка от 12 процентов годовых для малого бизнеса" in joined
    assert "Срок кредита до пяти лет без залога" in joined
    notes = [r for r in rows if r["kind"] == "notes"]
    assert [n["page_start"] for n in notes] == [1]
    assert notes[0]["text"].endswith(
        "Кредиты для бизнеса\nЗаметки: Подчеркнуть отсутствие залога при выдаче"
    )
    tables = [r for r in rows if r["kind"] == "table"]
    assert (
        len(tables) == 1
        and tables[0]["page_start"] == 2
        and tables[0]["table_id"] == "s2t1"
    )
    assert tables[0]["text"].endswith(
        "Тарифы по отделениям\nОтделение | Код | Телефон\n"
        "Москва, Тверская 1 | ZX01 | 495-111\nКазань, Баумана 5 | ZX02 | 843-222"
    )
    # Заголовок слайда без фигуры-заголовка — короткая первая строка.
    assert tables[0]["section"] == "Тарифы по отделениям"
    assert rows[0]["section"] == "Кредиты для бизнеса"
    assert rows[0]["meta"]["title"] == "Кредиты для бизнеса"
    assert rows[0]["meta"]["page_count"] == 3


def test_auto_numbering_is_restored(tmp_path):
    rows = list(file_chunks(_deck(tmp_path / "шаги.pptx")))
    slide3 = "\n".join(r["text"] for r in rows if r.get("page_start") == 3)
    assert (
        "1. Подать заявку онлайн\n2. Получить решение за день\n3. Подписать договор в отделении"
        in slide3
    )
