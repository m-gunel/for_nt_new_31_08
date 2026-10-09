"""Сбои Р7-Офиса при разборе .doc/.xls: временный сбой — повтор, а не «без текста».

x2t не запускается: subprocess.run в обёртке подменён. Машина — как АРМ SberOS:
лёгких конвертеров .doc нет (shutil.which → None), x2t «есть» (LQRAG_R7_X2T).
"""

import io
import shutil
import subprocess

import pytest

import src.utils.r7_office as r7
from src.parsers.base import SkipFile
from src.parsers.docx import DocParser
from src.parsers.xlsx import XlsxParser

OLE = b"\xd0\xcf\x11\xe0" + b"\0" * 64


@pytest.fixture
def x2t(tmp_path, monkeypatch):
    """Счётчик запусков x2t; поведение задаёт тест через calls["do"]."""
    fake = tmp_path / "x2t"
    fake.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setenv("LQRAG_R7_X2T", str(fake))
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(r7, "_timed_out", set(), raising=False)
    calls = {"n": 0, "do": None}

    def run(argv, **kwargs):
        calls["n"] += 1
        if calls["do"] == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        if calls["do"] == "oserror":
            raise OSError(11, "Resource temporarily unavailable")
        # иначе x2t «отработал» и ничего не записал

    monkeypatch.setattr("src.utils.r7_office.subprocess.run", run)
    return calls


def test_doc_runs_x2t_once_when_it_fails(x2t):
    assert DocParser().parse_io(io.BytesIO(OLE)).blocks == []
    assert x2t["n"] == 1


def test_doc_timeout_is_retried_once_then_skipped(x2t):
    x2t["do"] = "timeout"
    with pytest.raises(SkipFile) as err:
        DocParser().parse_io(io.BytesIO(OLE))
    assert err.value.permanent is False
    # Тот же файл снова не уложился: это свойство файла — больше не повторяем.
    assert DocParser().parse_io(io.BytesIO(OLE)).blocks == []
    assert x2t["n"] == 2


def test_xls_spawn_failure_is_temporary(x2t):
    x2t["do"] = "oserror"
    with pytest.raises(SkipFile) as err:
        XlsxParser().parse_io(io.BytesIO(OLE))
    assert err.value.permanent is False


def test_xls_timeout_is_temporary(x2t):
    x2t["do"] = "timeout"
    with pytest.raises(SkipFile) as err:
        XlsxParser().parse_io(io.BytesIO(OLE))
    assert err.value.permanent is False


def test_convert_default_contract_still_returns_false(x2t, tmp_path):
    x2t["do"] = "timeout"
    src = tmp_path / "book.xls"
    src.write_bytes(OLE)
    assert r7.convert_via_r7(str(src), str(tmp_path / "book.xlsx")) is False


@pytest.mark.parametrize("error", [MemoryError, OSError])
def test_xlsx_sheet_temporary_failure_is_not_swallowed(tmp_path, monkeypatch, error):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["a", 1])
    path = tmp_path / "b.xlsx"
    wb.save(path)

    def broken(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("src.parsers.xlsx._sheet", broken)
    with pytest.raises(error):
        list(XlsxParser().iter_chunks(str(path)))


def test_xlsx_broken_sheet_is_still_skipped(tmp_path, monkeypatch):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["a", 1])
    path = tmp_path / "b.xlsx"
    wb.save(path)

    def broken(*_args, **_kwargs):
        raise ValueError("битый лист")

    monkeypatch.setattr("src.parsers.xlsx._sheet", broken)
    assert list(XlsxParser().iter_chunks(str(path))) == []
