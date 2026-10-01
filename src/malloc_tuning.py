"""Настройка аллокатора glibc: лимит арен и возврат свободных страниц ОС.

Демон многопоточный (пулы Python, потоки OpenVINO, tokio у Lance, rayon у
polars), а glibc выдаёт каждому потоку свою арену и удерживает в ней
освобождённую память. Снаружи это выглядит как растущий RSS. Отсюда две ручки:

* `RAG_MALLOC_ARENA_MAX` — сколько арен максимум (по умолчанию 2);
* `RAG_MALLOC_TRIM` — вызывать ли `malloc_trim(0)` после освобождения крупных
  буферов, чтобы отдать страницы ОС.

Вне glibc обе превращаются в no-op. `mallopt` действует только на арены,
созданные после вызова, поэтому строгий вариант — выставить `MALLOC_ARENA_MAX`
в окружении до старта процесса; если она задана, модуль ничего не делает.

Модуль лежит в `src/`, а не в `src/utils/`: импорт `src.utils` тянет OpenVINO
и Lance и создаёт пул потоков, а лимит арен нужно выставить до этого.
"""

from __future__ import annotations

import ctypes
import logging
import os

lg = logging.getLogger("app.malloc_tuning")

# glibc malloc.h: параметр mallopt, ограничивающий число арен.
M_ARENA_MAX = -8

GLIBC_SONAME = "libc.so.6"
ENV_ARENA_MAX = "RAG_MALLOC_ARENA_MAX"
ENV_TRIM = "RAG_MALLOC_TRIM"
ENV_GLIBC_ARENA_MAX = "MALLOC_ARENA_MAX"

DEFAULT_ARENA_MAX = "2"
DEFAULT_TRIM = "1"

_DISABLED_VALUES = {"", "0", "off", "no", "false"}


def _load_libc() -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL(GLIBC_SONAME)
    except OSError:
        return None


_libc = _load_libc()
_trim_enabled = os.environ.get(ENV_TRIM, DEFAULT_TRIM).lower() not in _DISABLED_VALUES
_trim_broken = False
_state = "не применялась"


def is_glibc() -> bool:
    """Доступен ли glibc — от этого зависят обе ручки."""
    return _libc is not None


def apply_arena_limit() -> str:
    """Ограничить число арен glibc. Возвращает описание того, что вышло.

    Вызывать первым делом при старте процесса, до импорта тяжёлых библиотек.
    Логирование на этом этапе ещё не настроено, поэтому строку отдаём
    вызывающему, а не пишем в лог.
    """
    global _state
    requested = os.environ.get(ENV_ARENA_MAX, DEFAULT_ARENA_MAX).strip().lower()
    if requested in _DISABLED_VALUES:
        _state = "лимит арен выключен"
        return _state
    if not requested.isdigit():
        _state = f"лимит арен пропущен: {ENV_ARENA_MAX}={requested!r} — не число"
        return _state

    preset = os.environ.get(ENV_GLIBC_ARENA_MAX)
    if preset:
        # Переменная окружения сильнее: glibc применил её ещё до старта Python.
        _state = f"арены ограничены окружением, {ENV_GLIBC_ARENA_MAX}={preset}"
        return _state
    if _libc is None:
        _state = "не glibc — лимит арен неприменим"
        return _state

    if _libc.mallopt(ctypes.c_int(M_ARENA_MAX), ctypes.c_int(int(requested))) == 1:
        _state = f"арены ограничены через mallopt: {requested}"
    else:
        _state = f"mallopt(M_ARENA_MAX, {requested}) отклонён glibc"
    return _state


def safe_malloc_trim() -> bool:
    """Вернуть ОС свободную память кучи. True — если trim реально выполнен.

    Выключено или не glibc — тихо ничего не делает. Сбой гасит дальнейшие
    попытки, но не роняет вызывающий код: индексация важнее гигиены памяти.
    """
    global _trim_broken
    if not _trim_enabled or _libc is None or _trim_broken:
        return False
    try:
        _libc.malloc_trim(ctypes.c_size_t(0))
    except OSError:
        _trim_broken = True
        lg.warning("malloc_trim завершился ошибкой — вызовы отключены", exc_info=True)
        return False
    return True


def memory_state() -> str:
    """Одна строка о фактическом состоянии обеих ручек — для лога и отчётов."""
    if _libc is None:
        trim = "trim неприменим (не glibc)"
    elif _trim_enabled:
        trim = "trim включён"
    else:
        trim = "trim выключен"
    return f"{_state}; {trim}"
