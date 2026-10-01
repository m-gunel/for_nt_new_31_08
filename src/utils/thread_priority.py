"""Понижение приоритета фоновых потоков индексации.

Требование БТ — «фоновая индексация без влияния на производительность ОС».
Приоритет понижается точечно, на уровне потоков (Linux позволяет setpriority
по TID), а не всего процесса: поисковые запросы и HTTP-сервер должны
отвечать с обычным приоритетом.

Вне Linux (macOS, где TID-адресация setpriority не работает) — no-op:
функция создана как initializer для ThreadPoolExecutor и не имеет права
ронять поток пула.
"""

import os
import threading

from src.utils.logging import get_logger

lg = get_logger(__name__)

# На сколько понижать. 10 — заметная уступка интерактивным задачам без
# полного голодания индексации (19 на нагруженной машине почти останавливает
# фон).
INDEXING_NICE_DELTA = 10


def lower_current_thread_priority(delta: int = INDEXING_NICE_DELTA) -> bool:
    """Понизить nice текущего потока. True — если реально понизили."""
    try:
        tid = threading.get_native_id()
        current = os.getpriority(os.PRIO_PROCESS, tid)
        os.setpriority(os.PRIO_PROCESS, tid, min(19, current + delta))
        return True
    except (AttributeError, OSError, PermissionError):
        # macOS/ограниченные окружения: setpriority по TID недоступен.
        return False


def _pool_initializer():
    if lower_current_thread_priority():
        lg.debug(
            "поток %s: приоритет понижен для фоновой индексации",
            threading.current_thread().name,
        )


def background_pool_initializer():
    """Initializer для ThreadPoolExecutor'ов, занятых только индексацией."""
    _pool_initializer()
