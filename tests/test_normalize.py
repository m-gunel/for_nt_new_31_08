"""Нормализация текста после разбора (src/parsers/normalize.py): по правилу на тест."""

import unicodedata

import pytest

from src.parsers.normalize import FIELD_MAX, normalize_field, normalize_text


def test_nfc_and_line_endings():
    decomposed = unicodedata.normalize("NFD", "йод")
    assert decomposed != "йод"
    assert normalize_text(decomposed + "\r\nстрока\rещё") == "йод\nстрока\nещё"


@pytest.mark.parametrize("space", [" ", " ", " "])
def test_non_breaking_spaces_become_spaces(space):
    # В числах пробел остаётся пробелом: «27 821,6» ищется как обычный текст.
    assert normalize_text(f"27{space}821,6 млрд{space}руб.") == "27 821,6 млрд руб."


@pytest.mark.parametrize("char", ["​", "‌", "‍", "⁠", "﻿", "­", "‎"])
def test_format_characters_are_removed(char):
    assert normalize_text(f"сло{char}во") == "слово"


def test_soft_hyphen_at_line_end_joins_the_word():
    # Мягкий перенос — признак разрыва внутри слова: склеиваем, а не оставляем «пере\nнос».
    assert normalize_text("пере­\nнос строки") == "перенос строки"
    assert normalize_text("пере­ \n  нос") == "перенос"


def test_vertical_tab_is_a_line_break():
    # pptx отдаёт разрыв строки внутри абзаца символом \x0b.
    assert normalize_text("первая\x0bвторая") == "первая\nвторая"


def test_pdf_control_char_between_letters_is_a_hyphen():
    # Старые «Вестники»: дефис извлекается управляющим символом.
    assert normalize_text("№385\x1aП", pdf=True) == "№385-П"
    assert normalize_text("финан\x18\nсовых", pdf=True) == "финан-\nсовых"
    # Не между буквами — просто мусор.
    assert normalize_text("Глава 10. \x07Норматив", pdf=True) == "Глава 10. Норматив"


def test_control_chars_outside_pdf_are_removed():
    assert normalize_text("№385\x1aП") == "№385П"
    assert normalize_text("a\x00b\x7fc\x85d") == "abcd"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (" пункт списка", "• пункт списка"),
        ("ПК1  11", "ПК1 ≥ 11"),
        ("1  С  20", "1 ≤ С ≤ 20"),
        ("К", "ΔК"),
        (" i", "Σ i"),
        ("a  b", "a × b"),
        ("(x)", "(x)"),  # куски больших скобок формул — шум
        (" маркер", "• маркер"),  # прочее в области — маркеры списков
    ],
)
def test_symbol_font_characters(raw, clean):
    assert normalize_text(raw) == clean


def test_whitespace_rules():
    text = "a\tb   c  \nстрока  \n\n\n\nконец"
    assert normalize_text(text) == "a b c\nстрока\n\nконец"


def test_emoji_in_text_are_kept():
    assert normalize_text("Привет 👋") == "Привет 👋"


def test_field_is_one_clean_line():
    assert normalize_field("  Отчёт за\n2024 год 📊\x07 ") == "Отчёт за 2024 год"
    assert normalize_field("") is None
    assert normalize_field(None) is None
    long = normalize_field("слово " * 100)
    assert len(long) == FIELD_MAX and long.endswith("…")
