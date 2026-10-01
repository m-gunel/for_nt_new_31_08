"""Очистка PDF (src/parsers/pdf_clean.py): переносы, колонтитулы, заголовки."""

import pymupdf
import pytest

from src.parsers import file_chunks
from src.parsers.doc_model import Block
from src.parsers.normalize import normalize_text
from src.parsers.pdf_clean import (
    dehyphenate,
    doc_lexicon,
    heading_level,
    split_headings,
    strip_furniture,
)


def _dehyph(text: str) -> str:
    text = normalize_text(text, pdf=True)
    return dehyphenate(text, doc_lexicon(text))


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (
            "законодательство Российской Федера-\nции",
            "законодательство Российской Федерации",
        ),
        ("из-\nза отсутствия", "из-за отсутствия"),
        ("в Северо-\nЗападном округе", "в Северо-Западном округе"),
        ("что-\nто иное", "что-то иное"),
        (
            "кредитно-обесцененных активов; кредитно-\nобесце-\nненные",
            "кредитно-обесцененных активов; кредитно-обесцененные",
        ),
        (
            "организа\x1c\nции",
            "организации",
        ),  # дефис старых «Вестников» — управляющий символ
        ("Указание №138\x19И", "Указание №138-И"),
        ("стресс-тест; стресс-\nтестирование", "стресс-тест; стресс-тестирование"),
    ],
)
def test_dehyphenation(raw, clean):
    assert _dehyph(raw) == clean


def test_hyphenated_form_wins_when_document_uses_it():
    text = "контр-агент упомянут дважды: контр-агент и контр-\nагент"
    assert dehyphenate(text, doc_lexicon(text)).endswith("контр-агент")


BODIES = [
    "Положение устанавливает порядок расчёта резервов.",
    "Кредитные организации формируют резервы ежемесячно.",
    "Расчёт ведётся по каждой ссуде отдельно.",
    "Классификация ссуд пересматривается при изменении риска.",
    "Отчётность представляется в территориальное учреждение.",
    "Положение вступает в силу с даты публикации.",
]


def test_running_headers_and_page_numbers_are_stripped():
    pages = [
        f"ВЕСТНИК БАНКА РОССИИ\n№ 42 (1386) 1 августа 2012\n{body}\n(в млн рублей)\n{i}"
        for i, body in enumerate(BODIES, 1)
    ]
    out = strip_furniture(pages)
    assert out[0].startswith(
        "ВЕСТНИК БАНКА РОССИИ"
    )  # первое вхождение — контекст документа
    assert all("ВЕСТНИК" not in p and "1386" not in p for p in out[1:])
    assert all(body in p for body, p in zip(BODIES, out))
    assert all("(в млн рублей)" in p for p in out)  # строка с единицами — не колонтитул
    assert all(not p.rstrip().endswith(str(i)) for i, p in enumerate(out, 1))


def test_mirrored_headers_on_odd_and_even_pages():
    pages = [
        ("Левый колонтитул выпуска" if i % 2 else "Правый колонтитул выпуска")
        + f"\nТело {i} страницы"
        for i in range(1, 9)
    ]
    out = strip_furniture(pages)
    assert sum("колонтитул" in p for p in out) == 2  # по одному первому вхождению


def test_short_document_needs_header_on_every_page():
    pages = ["Общая шапка\nПервый текст", "Другая шапка\nВторой текст"]
    assert strip_furniture(pages) == pages


@pytest.mark.parametrize(
    ("line", "level"),
    [
        ("Глава 2. Порядок расчёта", 2),
        ("ГЛАВА 1", 2),
        ("Раздел II. Отчётность", 1),
        ("Статья 5. Права клиента", 3),
        ("Статья 5 Федерального закона от 02.12.1990", None),
        ("Приложение 1", 1),
        ("Приложение 2 к Положению Банка России", 1),
        ("Глава 2 устанавливает порядок", None),
        ("глава 3,", None),
        ("Глава 4. " + "очень длинное продолжение " * 10, None),
    ],
)
def test_heading_level(line, level):
    assert heading_level(line) == level


def test_split_headings_by_numbering_and_two_line_heading():
    page = Block(
        "Текст до главы.\nГЛАВА 1\nОБЩИЕ ПОЛОЖЕНИЯ\n1.1. Настоящее Положение…\nГлава 2. Расчёт\nТекст главы 2.",
        page=3,
    )
    out = split_headings(page, [], numbering=True)
    assert [(b.text, b.heading) for b in out] == [
        ("Текст до главы.", None),
        ("ГЛАВА 1 ОБЩИЕ ПОЛОЖЕНИЯ", 2),
        ("1.1. Настоящее Положение…", None),
        ("Глава 2. Расчёт", 2),
        ("Текст главы 2.", None),
    ]
    assert {b.page for b in out} == {3}


def test_split_headings_by_bookmarks():
    page = Block(
        "Введение в отчёт\nСодержимое введения\nОсновные показатели банка\nПоказатели",
        page=1,
    )
    out = split_headings(
        page,
        [(1, "Введение в отчёт"), (2, "Основные показатели банка")],
        numbering=False,
    )
    assert [(b.text, b.heading) for b in out if b.heading] == [
        ("Введение в отчёт", 1),
        ("Основные показатели банка", 2),
    ]


def test_pdf_pipeline_cleans_headers_and_hyphens(tmp_path):
    path = tmp_path / "herald.pdf"
    with pymupdf.open() as doc:
        for i in range(1, 7):
            page = doc.new_page()
            # Base-шрифты PyMuPDF латинские — текст тоже латиницей.
            page.insert_text((72, 60), "BANK HERALD No 42")
            page.insert_text(
                (72, 120), f"Page {i} text: the rule is appli-\ncable to every bank."
            )
            page.insert_text((72, 780), str(i))
        doc.save(str(path))
    joined = "\n".join(r["text"] for r in file_chunks(path))
    assert joined.count("BANK HERALD") == 1
    assert "applicable to every bank" in joined and "appli-" not in joined
