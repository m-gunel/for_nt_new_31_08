"""Очистка PDF (src/parsers/pdf_clean.py): переносы и заголовки; колонтитулы остаются."""

import pymupdf
import pytest

from src.parsers import file_chunks
from src.parsers.doc_model import Block, ParsedDoc, chunk_document, full_text, prepare
from src.parsers.normalize import normalize_text
from src.parsers.pdf_clean import (
    dehyphenate,
    doc_lexicon,
    heading_level,
    split_headings,
)
from tests.test_doc_model import SPLITTER, _words


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
        ("ГЛАВА 1\nОБЩИЕ ПОЛОЖЕНИЯ", 2),  # строки страницы как есть
        ("1.1. Настоящее Положение…", None),
        ("Глава 2. Расчёт", 2),
        ("Текст главы 2.", None),
    ]
    assert {b.page for b in out} == {3}
    ocr_page = Block(page.text, kind="ocr", page=3)
    assert {b.kind for b in split_headings(ocr_page, [], numbering=True)} == {"ocr"}


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


def test_pdf_headings_set_section_but_keep_text():
    """Заголовок PDF — только путь раздела: полный текст тот же, что у страниц без разметки."""
    pages = [
        "Текст до главы.\nГЛАВА 1\nОБЩИЕ ПОЛОЖЕНИЯ\n" + _words(30, "общее"),
        "Глава 2. Расчёт\n" + _words(30, "расчёт"),
    ]
    doc = prepare(
        ParsedDoc([Block(t, page=n) for n, t in enumerate(pages, 1)], pdf=True)
    )
    assert [b.heading for b in doc.blocks if b.heading] == [2, 2]
    assert full_text(doc) == "\n\n".join(pages)  # ни склейки строк, ни новых абзацев
    sections = {
        c.get("section") for c in chunk_document(doc, SPLITTER) if "расчёт" in c["text"]
    }
    assert sections == {"Глава 2. Расчёт"}  # глава того же уровня заменяет прежнюю


def test_pdf_pipeline_keeps_running_headers_and_joins_hyphens(tmp_path):
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
    # Карточка документа (kind="card") повторяет название — считаем только чанки.
    joined = "\n".join(r["text"] for r in file_chunks(path) if r["kind"] != "card")
    # В колонтитуле банк, период и номер акта — он остаётся на каждой странице.
    assert joined.count("BANK HERALD") == 6
    assert "applicable to every bank" in joined and "appli-" not in joined
