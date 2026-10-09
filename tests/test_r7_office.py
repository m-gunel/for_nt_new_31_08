"""Обёртка Р7-Офиса (x2t) и её использование парсерами.

Юнит-часть работает без установленного Р7: проверяются поиск конвертера по
LQRAG_R7_X2T, отказ до запуска процесса на несуществующем входе (на нём
x2t падает по SIGSEGV) и цепочка кандидатов конвертера .doc. Живой тест
конверсии включается только там, где Р7-Офис действительно установлен:
в docker-образе CI его нет, и там он честно пропускается.
"""

import os
import shutil
from pathlib import Path

import pytest

from src.parsers.docx import _find_doc_converter
from src.parsers.xlsx import XlsxParser
from src.utils.r7_office import convert_via_r7, find_x2t

FIXTURE = Path(__file__).parent / "fixtures" / "legacy.xls"


def test_find_x2t_honors_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "x2t"
    fake.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setenv("LQRAG_R7_X2T", str(fake))
    assert find_x2t() == str(fake)

    monkeypatch.setenv("LQRAG_R7_X2T", str(tmp_path / "нет-такого"))
    assert find_x2t() is None


def test_convert_refuses_missing_input_before_spawning(tmp_path, monkeypatch):
    # x2t не проверяет существование входа и умирает по SIGSEGV — обёртка
    # обязана отказать ДО запуска процесса.
    monkeypatch.setenv("LQRAG_R7_X2T", "/usr/bin/true")
    missing_src = tmp_path / "входа-нет.xls"
    dst = tmp_path / "out.xlsx"
    assert convert_via_r7(str(missing_src), str(dst)) is False
    assert not dst.exists()


def test_convert_says_no_when_r7_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("LQRAG_R7_X2T", raising=False)
    monkeypatch.setattr(
        "src.utils.r7_office._X2T_CANDIDATES", (str(tmp_path / "пусто"),), raising=True
    )
    assert convert_via_r7("что-угодно.xls", str(tmp_path / "out.xlsx")) is False


def test_doc_converter_prefers_light_tools_and_falls_back_to_r7(tmp_path, monkeypatch):
    original_which = shutil.which

    def fake_which(name):
        if name == "catdoc":
            return "/usr/bin/catdoc"
        return original_which(name) if name != "soffice" else None

    # Лёгкий инструмент важнее тяжёлого конвертера офисного пакета.
    monkeypatch.setattr(shutil, "which", fake_which)
    assert _find_doc_converter()[0] == "catdoc"

    # Машина без catdoc/antiword/soffice/textutil (типовая АРМ SberOS):
    # последним кандидатом остаётся Р7-Офис.
    fake = tmp_path / "x2t"
    fake.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setenv("LQRAG_R7_X2T", str(fake))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _find_doc_converter() == ("r7-x2t", [])

    # Нет вообще ничего — конвертера нет, файл будет пропущен с предупреждением.
    monkeypatch.setenv("LQRAG_R7_X2T", str(tmp_path / "нет"))
    assert _find_doc_converter() is None


def test_xlsx_parser_skips_legacy_xls_without_converter(config, monkeypatch):
    """Деградация без Р7: 0 чанков вместо исключения — индексатор пометит
    файл постоянным пропуском, а не будет ретраить вечной ошибкой."""
    monkeypatch.setenv("LQRAG_R7_X2T", "/путь-которого-нет")
    monkeypatch.setattr(
        "src.utils.r7_office._X2T_CANDIDATES", ("/нет-и-не-будет",), raising=True
    )
    if not FIXTURE.exists():
        pytest.skip("нет фикстуры legacy.xls")
    parser = XlsxParser()
    assert list(parser.iter_chunks(str(FIXTURE))) == []


@pytest.mark.skipif(
    find_x2t() is None or not FIXTURE.exists(),
    reason="Р7-Офис не установлен или нет фикстуры legacy.xls",
)
def test_xlsx_parser_reads_legacy_xls_via_r7(config):
    """Живой прогон: настоящий бинарный .xls конвертируется x2t в .xlsx
    и индексируется штатным путём openpyxl."""
    parser = XlsxParser()
    chunks = list(parser.iter_chunks(str(FIXTURE)))
    assert chunks, "легаси .xls обязан индексироваться через конвертер Р7"
    joined = "\n".join(chunk["text"] for chunk in chunks)
    assert "Контрольная фраза листа Отчёт" in joined


@pytest.fixture
def fake_x2t(tmp_path, monkeypatch):
    """x2t «есть» (find_x2t проверяет только isfile), но запускать его нельзя."""
    fake = tmp_path / "x2t"
    fake.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setenv("LQRAG_R7_X2T", str(fake))

    def no_spawn(*args, **kwargs):
        raise AssertionError("x2t не должен запускаться")

    monkeypatch.setattr("src.utils.r7_office.subprocess.run", no_spawn)
    return fake


def test_convert_returns_false_when_dst_is_directory(tmp_path, fake_x2t):
    """Контракт «исключений наружу не бросает» держится и на подготовке dst."""
    src = tmp_path / "book.xls"
    src.write_bytes(b"\xd0\xcf\x11\xe0")
    dst = tmp_path / "book.xlsx"
    dst.mkdir()
    assert convert_via_r7(str(src), str(dst)) is False


def test_convert_returns_false_when_input_vanishes(tmp_path, fake_x2t, monkeypatch):
    """Гонка: вход удалён между isfile и getsize."""
    src = tmp_path / "book.xls"
    src.write_bytes(b"\xd0\xcf\x11\xe0")
    real_getsize = os.path.getsize

    def vanishing(path):
        if os.fspath(path) == str(src):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_getsize(path)

    monkeypatch.setattr("src.utils.r7_office.os.path.getsize", vanishing)
    assert convert_via_r7(str(src), str(tmp_path / "book.xlsx")) is False


def test_convert_returns_false_when_output_vanishes(tmp_path, monkeypatch):
    """Гонка на проверке результата: dst удалён между isfile и getsize."""
    fake = tmp_path / "x2t"
    fake.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setenv("LQRAG_R7_X2T", str(fake))
    src = tmp_path / "book.xls"
    src.write_bytes(b"\xd0\xcf\x11\xe0")
    dst = tmp_path / "book.xlsx"

    def x2t_writes_dst(argv, **kwargs):
        Path(argv[2]).write_bytes(b"PK")

    real_getsize = os.path.getsize

    def vanishing(path):
        if os.fspath(path) == str(dst):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_getsize(path)

    monkeypatch.setattr("src.utils.r7_office.subprocess.run", x2t_writes_dst)
    monkeypatch.setattr("src.utils.r7_office.os.path.getsize", vanishing)
    assert convert_via_r7(str(src), str(dst)) is False
