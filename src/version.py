"""Единственный источник версии продукта: её показывают /docs и пакет.

Остальные места повторяют значение, чтобы названная пользователем версия
однозначно указывала на сборку: debian/changelog и pyproject.toml сверяет
`make deb`, pyproject.toml и uv.lock — tests/test_version_sync.py.
"""

__version__ = "1.0.12"
