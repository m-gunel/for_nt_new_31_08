"""Конвертер форматов через Р7-Офис (x2t из комплекта desktopeditors).

Р7-Офис стоит на АРМ по политике, а catdoc/antiword/LibreOffice в SberOS нет;
x2t читает легаси-форматы (.doc, .xls), иначе индексатору недоступные.

Особенности x2t:
  - LD_LIBRARY_PATH должен включать каталог x2t и его родителя, иначе не
    резолвятся libascdocumentscore и соседние .so;
  - x2t пишет временные файлы в рабочий каталог, а в /opt/r7-office у демона
    прав нет;
  - код возврата ненадёжен (успех — и 0, и 80, на несуществующем входе
    процесс падает по сигналу): вход проверяем сами, успех — только
    непустой результат.
"""

import hashlib
import os
import subprocess

from src.utils import get_logger

lg = get_logger(__name__)

# x2t в пакете sber-r7-office; второй путь — на случай переименования
# корневого каталога продукта.
_X2T_CANDIDATES = (
    "/opt/r7-office/desktopeditors/converter/x2t",
    "/opt/r7-office/r7-office/desktopeditors/converter/x2t",
)

CONVERT_TIMEOUT_S = 120

# Содержимое (sha256), на котором x2t уже не уложился в таймаут: второй таймаут
# подряд — свойство файла, а не нагрузки, и дальше его не повторяем.
_timed_out: set[str] = set()


class R7Busy(Exception):
    """x2t не запустился или не уложился в таймаут — на следующем проходе может выйти."""


def find_x2t() -> str | None:
    """Путь к x2t или None, если Р7-Офиса на машине нет.

    LQRAG_R7_X2T переопределяет путь целиком — этим пользуются тесты и
    машины с нестандартной раскладкой Р7.
    """
    override = os.environ.get("LQRAG_R7_X2T")
    if override:
        return override if os.path.isfile(override) else None
    for candidate in _X2T_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def convert_via_r7(
    src: str,
    dst: str,
    timeout_s: int = CONVERT_TIMEOUT_S,
    *,
    raise_busy: bool = False,
) -> bool:
    """Конвертировать файл командой x2t. True — в dst готовый результат.

    Отказоустойчива во всех случаях, включая полное отсутствие Р7-Офиса:
    возвращает False и пишет предупреждение, исключений наружу не бросает.
    Исключение одно и только по запросу: raise_busy=True — сбой запуска x2t
    и первый таймаут на этом содержимом бросают R7Busy (временный сбой).
    Что делать с пропущенным файлом, решает вызывающий парсер (0 чанков —
    «постоянный» пропуск индексатора, исключение — повторная попытка).
    """
    x2t = find_x2t()
    if not x2t:
        lg.warning("Р7-Офис не найден — конвертация %s невозможна", src)
        return False
    try:
        if not os.path.isfile(src) or os.path.getsize(src) == 0:
            lg.warning("конвертация %s: вход отсутствует или пуст", src)
            return False
        if os.path.exists(dst):
            os.remove(dst)
    except OSError as exc:
        # Вход исчез между проверками, на месте dst каталог или нет прав.
        lg.warning("конвертация %s: вход или выход недоступен (%s)", src, exc)
        return False
    converter_dir = os.path.dirname(x2t)
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(
        (converter_dir, os.path.dirname(converter_dir), env.get("LD_LIBRARY_PATH", ""))
    ).rstrip(":")
    try:
        subprocess.run(
            [x2t, src, dst],
            cwd=os.path.dirname(dst) or ".",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
        ok = os.path.isfile(dst) and os.path.getsize(dst) > 0
    except (OSError, subprocess.SubprocessError) as exc:
        lg.warning("Р7-Офис: конвертация %s не состоялась (%s)", src, exc)
        if raise_busy and _worth_retry(src, exc):
            raise R7Busy(f"конвертация не состоялась ({exc})") from exc
        return False
    if not ok:
        lg.warning("Р7-Офис: пустой результат конвертации %s", src)
    return ok


def _worth_retry(src: str, exc: Exception) -> bool:
    """Сбой запуска — всегда; таймаут — только первый на этом содержимом."""
    if not isinstance(exc, subprocess.TimeoutExpired):
        return True
    try:
        with open(src, "rb") as f:
            key = hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        return True
    if key in _timed_out:
        return False
    _timed_out.add(key)
    return True
