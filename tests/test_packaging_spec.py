"""Имена пакетов в collect_all/--collect-all сборки PyInstaller."""

import ast
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _collect_all_names():
    tree = ast.parse((ROOT / "main.spec").read_text(encoding="utf-8"))
    names = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "collect_all"
    ]
    # Без Makefile (неполная копия проекта) проверяется только main.spec.
    makefile = ROOT / "Makefile"
    if makefile.is_file():
        text = makefile.read_text(encoding="utf-8")
        names += re.findall(r"--collect-all[ =](\S+)", text)
    return names


def test_collect_all_gets_importable_module_names():
    # collect_all ищет пакет по имени модуля: имя дистрибутива (langchain-core)
    # он не находит и молча собирает одни метаданные.
    names = _collect_all_names()
    assert "langchain_core" in names
    assert [name for name in names if importlib.util.find_spec(name) is None] == []
