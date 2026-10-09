"""clean_filename: имя временного файла для POST /get_file_path.

NAME_MAX в ext4/tmpfs/xfs — 255 байт, и длинная тема письма или имя вложения
не должны превращаться в 500 ENAMETOOLONG; «.», «..» и пустое имя — в
IsADirectoryError.
"""

import pytest

from src.utils.filename import clean_filename

NAME_MAX = 255
# Запас под «.eml», который get_file_path дописывает к очищенной теме.
LIMIT = NAME_MAX - len(".eml")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Отчёт по кварталу", "Отчёт по кварталу"),
        ("договор.txt", "договор.txt"),
        ("Re: FW: Встреча в четверг", "Re FW Встреча в четверг"),
        ('  a<b>c:d"e/f\\g|h?i*j\0k.pdf  ', "abcdefghijk.pdf"),
        ("Отчёт v1.2 итог", "Отчёт v1.2 итог"),
        ("message", "message"),
    ],
)
def test_ordinary_names_unchanged(raw, expected):
    """Обычные имена выдаются ровно как раньше."""
    assert clean_filename(raw) == expected


def test_long_attachment_name_fits_and_keeps_extension():
    name = "Приложение 1 к Положению о порядке взаимодействия " * 6 + ".docx"
    assert len(name.encode()) > NAME_MAX
    out = clean_filename(name)
    assert len(out.encode()) <= LIMIT
    assert out.endswith(".docx")
    assert out.startswith("Приложение 1 к Положению")


def test_long_subject_with_eml_suffix_fits_name_max():
    """get_file_path дописывает «.eml» к уже очищенной теме."""
    subject = "Re: " * 62 + "RE: FW: О согласовании изменений в регламент " * 3
    out = clean_filename(subject) + ".eml"
    assert len(out.encode()) <= NAME_MAX


def test_truncation_keeps_whole_characters():
    """Обрезка по байтам не режет двухбайтовую букву пополам: в 251 байт
    помещаются 125 букв «ж», а не 125 с половиной."""
    out = clean_filename("ж" * 300)  # 600 байт, без расширения
    assert out == "ж" * (LIMIT // 2)


def test_long_dotted_tail_is_not_taken_for_extension():
    """Хвост после точки с пробелами — часть имени, а не расширение."""
    name = "ж" * 200 + ".08 по продажам за квартал"
    out = clean_filename(name)
    assert len(out.encode()) <= LIMIT
    assert out == "ж" * (LIMIT // 2)


@pytest.mark.parametrize("raw", [".", "..", "", "   ", "???", "<>", " . "])
def test_directory_like_and_empty_names_get_fallback(raw):
    out = clean_filename(raw)
    assert out not in ("", ".", "..")
    assert out == "file"
