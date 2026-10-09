"""Версия продукта одна: src/version.py, pyproject.toml и uv.lock совпадают.

debian/changelog и pyproject.toml сверяет ещё гейт цели `deb` в Makefile.
"""

import tomllib
from pathlib import Path

import pytest

from src.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_version_matches_version_py():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


def test_uv_lock_project_version_matches_version_py():
    lock = ROOT / "uv.lock"
    if not lock.exists():
        pytest.skip("uv.lock нет — сверять не с чем")
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    versions = [
        p.get("version") for p in data["package"] if p["name"] == "files-search-back"
    ]
    assert versions == [__version__], "версия files-search-back в uv.lock"
