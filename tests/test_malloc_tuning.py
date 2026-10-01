"""Ручки аллокатора не должны ломать работу там, где их механизма нет.

Модуль импортируется на любой платформе, поэтому проверяется безопасность:
разбор переменных окружения и то, что вызовы вне glibc тихо возвращают False,
а не бросают исключение.
"""

import importlib

import pytest

import src.malloc_tuning as mt


def reload_with_env(monkeypatch: pytest.MonkeyPatch, **env: str):
    """Перечитать модуль с заданным окружением: обе ручки читаются на импорте."""
    for name in (mt.ENV_ARENA_MAX, mt.ENV_TRIM, mt.ENV_GLIBC_ARENA_MAX):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return importlib.reload(mt)


def test_trim_never_raises(monkeypatch: pytest.MonkeyPatch):
    module = reload_with_env(monkeypatch, RAG_MALLOC_TRIM="1")
    result = module.safe_malloc_trim()
    assert result is module.is_glibc()


def test_trim_disabled_by_env(monkeypatch: pytest.MonkeyPatch):
    module = reload_with_env(monkeypatch, RAG_MALLOC_TRIM="0")
    assert module.safe_malloc_trim() is False
    # Вне glibc состояние «неприменим» честнее, чем «выключен»: механизма нет
    # независимо от переменной. Ветка «выключен» проверяется на целевой ОС.
    expected = "trim выключен" if module.is_glibc() else "trim неприменим"
    assert expected in module.memory_state()


def test_arena_limit_can_be_disabled(monkeypatch: pytest.MonkeyPatch):
    module = reload_with_env(monkeypatch, RAG_MALLOC_ARENA_MAX="0")
    assert module.apply_arena_limit() == "лимит арен выключен"


def test_arena_limit_rejects_garbage(monkeypatch: pytest.MonkeyPatch):
    module = reload_with_env(monkeypatch, RAG_MALLOC_ARENA_MAX="две")
    assert "не число" in module.apply_arena_limit()


def test_env_var_wins_over_mallopt(monkeypatch: pytest.MonkeyPatch):
    """Если glibc уже применил лимит при старте, повторно ничего не делаем."""
    module = reload_with_env(
        monkeypatch, RAG_MALLOC_ARENA_MAX="2", MALLOC_ARENA_MAX="4"
    )
    state = module.apply_arena_limit()
    assert "окружением" in state and "4" in state


def test_apply_arena_limit_is_reported(monkeypatch: pytest.MonkeyPatch):
    module = reload_with_env(monkeypatch, RAG_MALLOC_ARENA_MAX="2")
    module.apply_arena_limit()
    state = module.memory_state()
    assert state and ";" in state


@pytest.fixture(autouse=True)
def _restore_module():
    """Вернуть модуль в исходное состояние — его импортируют другие тесты."""
    yield
    importlib.reload(mt)
