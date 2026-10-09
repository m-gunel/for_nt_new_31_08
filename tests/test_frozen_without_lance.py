"""Сборка без модуля lance (pylance).

`LanceTable.to_lance()` начинается с `import lance`, а main.spec долго
исключал пакет из бандла. Без модуля дообновление индексов должно
пропускаться с предупреждением в журнале.
"""

import logging
import re
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from src.common.doc_meta import doc_meta_schema
from src.utils.lance_wrapper import LanceWrapper

ROOT = Path(__file__).resolve().parents[1]
DIM = 8


class _NoLance:
    """Как в бандле без пакета: `import lance` даёт ModuleNotFoundError."""

    def find_spec(self, name, path=None, target=None):
        if name == "lance" or name.startswith("lance."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


@pytest.fixture
def without_lance(monkeypatch):
    # Уже импортированный lance убираем на время теста, а teardown вернёт те же
    # объекты: нативное расширение повторно не инициализируется. Подмена
    # sys.modules['lance'] = None не годится: lancedb сам проверяет
    # `"lance" in sys.modules` и с ней не работает.
    for name in [m for m in sys.modules if m == "lance" or m.startswith("lance.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_NoLance(), *sys.meta_path])


def _rows(paths: list[str]) -> pa.Table:
    rng = np.random.default_rng(seed=1)
    vectors = rng.standard_normal((len(paths), DIM)).astype(np.float32)
    return pa.table(
        {
            "idx": pa.array(range(len(paths)), pa.int64()),
            "text": pa.array([f"текст {p}" for p in paths]),
            "meta": pa.array([None] * len(paths), pa.struct(doc_meta_schema)),
            "path": pa.array(paths),
            "vector": pa.FixedSizeListArray.from_arrays(vectors.reshape(-1), DIM),
        }
    )


def test_optimize_indices_without_lance_warns_once(tmp_path, without_lance, caplog):
    files = LanceWrapper(str(tmp_path / "lance"), DIM, "files")
    files.add_sync(_rows(["/home/u/отчёт.txt"]))

    with caplog.at_level(logging.WARNING):
        files.optimize_indices()
        files.optimize_indices()

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "lance" in r.getMessage()
    ]
    assert len(warnings) == 1, (
        "без модуля lance индексы не дообновляются — об этом нужна одна строка "
        f"WARNING, а не DEBUG на каждый цикл: {caplog.text!r}"
    )


def test_spec_does_not_exclude_lance():
    """optimize_indices работает только с пакетом lance в бандле."""
    spec = (ROOT / "main.spec").read_text(encoding="utf-8")
    excludes = re.search(r"excludes=\[(.*?)\]", spec, re.S)
    assert excludes is not None
    assert "'lance'" not in excludes.group(1) and '"lance"' not in excludes.group(1)
    # Без Makefile (контекст docker-сборки CI) проверяется только main.spec.
    makefile = ROOT / "Makefile"
    if makefile.is_file():
        text = makefile.read_text(encoding="utf-8")
        assert not re.search(r"--exclude-module[= ]lance\b", text)
