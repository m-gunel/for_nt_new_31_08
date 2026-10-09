"""Публикация порта: сбой при динамическом порте — ошибка, а не предупреждение."""

import logging

from src.port_file import publish_port


def _broken_root(tmp_path):
    # Корень — обычный файл: каталог под порт-файл не создать, запись падает.
    root = tmp_path / "not-a-dir"
    root.write_text("x", encoding="utf-8")
    return root


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_unpublished_dynamic_port_is_error(tmp_path, caplog):
    """RAG_PORT=0: без порт-файла мост идёт на 8000 и демон не найдёт."""
    caplog.set_level(logging.WARNING, logger="app")
    assert publish_port(_broken_root(tmp_path), 41234) is None
    errors = _errors(caplog)
    assert errors and "41234" in errors[0].getMessage()


def test_unpublished_fallback_port_is_warning(tmp_path, caplog):
    """На 8000 мост найдёт демон и без файла — достаточно предупреждения."""
    caplog.set_level(logging.WARNING, logger="app")
    assert publish_port(_broken_root(tmp_path), 8000) is None
    assert not _errors(caplog)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
