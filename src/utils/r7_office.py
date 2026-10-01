"""Конвертер форматов через Р7-Офис (x2t из комплекта desktopeditors).

Р7-Офис — стандартный офисный пакет АРМ Сбера: его наличие на целевой машине
гарантировано политикой, в отличие от catdoc/antiword/LibreOffice, которых
в поставке SberOS нет вообще. Встроенный конвертер x2t умеет читать легаси-
форматы (.doc, .xls), которые иначе индексатору недоступны.

Особенности вызова x2t, без которых он не работает или падает:

  - LD_LIBRARY_PATH обязан включать каталог самого x2t и его родителя —
    иначе не резолвятся libascdocumentscore и соседние .so;
  - рабочий каталог процесса должен быть доступен на запись: x2t создаёт
    временные файлы в текущем каталоге, а в /opt/r7-office прав у демона нет;
  - код возврата ненадёжен: успешная конвертация даёт и 0, и 80, а на
    несуществующем входе процесс роняет сигналом. Обёртка проверяет вход
    сама и судит об успехе по факту существования непустого результата;
  - пустой результат при отработавшем вызове означает «не смог разобрать
    вход» — это тоже отказ, а не успех.
"""

import os
import subprocess

from src.utils import get_logger

lg = get_logger(__name__)

# Каталоги конвертера внутри пакета sber-r7-office. Структура стабильно
# повторяет ONLYOFFICE (converter/x2t рядом с движком); второй шаблон —
# страховка на случай переименования корневого каталога продукта.
_X2T_CANDIDATES = (
    "/opt/r7-office/desktopeditors/converter/x2t",
    "/opt/r7-office/r7-office/desktopeditors/converter/x2t",
)

CONVERT_TIMEOUT_S = 120


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


def convert_via_r7(src: str, dst: str, timeout_s: int = CONVERT_TIMEOUT_S) -> bool:
    """Конвертировать файл командой x2t. True — в dst готовый результат.

    Отказоустойчива во всех случаях, включая полное отсутствие Р7-Офиса:
    возвращает False и пишет предупреждение, исключений наружу не бросает.
    Что делать с пропущенным файлом, решает вызывающий парсер (0 чанков —
    «постоянный» пропуск индексатора, исключение — повторная попытка).
    """
    x2t = find_x2t()
    if not x2t:
        lg.warning("Р7-Офис не найден — конвертация %s невозможна", src)
        return False
    if not os.path.isfile(src) or os.path.getsize(src) == 0:
        lg.warning("конвертация %s: вход отсутствует или пуст", src)
        return False
    if os.path.exists(dst):
        os.remove(dst)
    converter_dir = os.path.dirname(x2t)
    env = dict(os.environ)
    # Родительский каталог держит общие библиотеки движка.
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
    except (OSError, subprocess.SubprocessError) as exc:
        lg.warning("Р7-Офис: конвертация %s не состоялась (%s)", src, exc)
        return False
    ok = os.path.isfile(dst) and os.path.getsize(dst) > 0
    if not ok:
        lg.warning("Р7-Офис: пустой результат конвертации %s", src)
    return ok
