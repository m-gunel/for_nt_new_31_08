"""Понижение приоритета фоновых потоков индексации.

БТ: «фоновая индексация без влияния на производительность ОС». Понижается
nice отдельных потоков (Linux позволяет setpriority по TID), а не процесса:
поиск и HTTP-сервер отвечают с обычным приоритетом.

Инференс эмбеддингов этим не покрыт: его считают потоки плагина OpenVINO,
общие для поиска и индексации (см. _InferChannel в openvino_encoder.py).

Вне Linux — no-op: функция служит initializer'ом ThreadPoolExecutor и не
должна ронять поток пула.
"""

import os
import threading

from src.utils.logging import get_logger

lg = get_logger(__name__)

# 10 — заметная уступка интерактивным задачам; 19 на нагруженной машине
# почти останавливает индексацию.
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
