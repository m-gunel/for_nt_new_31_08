import os
import re
from collections.abc import Iterable
from pathlib import Path

# Мусор, исключаемый из обхода. Сверяется с ИМЕНЕМ одного компонента пути
# (см. check_path), а не с путём целиком. Все скрытые имена покрывает `\..+`,
# поэтому остальные альтернативы — только для имён без точки.
exclude_pattern = re.compile(
    r"""^(?:                 # начало строки, одна из групп
        \..+                 # любые скрытые файлы или папки (в т.ч. .venv,
                             # .git, .idea, .DS_Store, .Trash-1000, а также
                             # собственные каталоги демона .files-search и
                             # .ragsearch — свой индекс не индексируем)
        |node_modules        # зависимости Node.js
        |__pycache__         # кэш питона
        |venv                # виртуальное окружение без точки: без него
                             # venv/lib/site-packages уезжает в индекс
                             # тысячами .txt из состава пакетов
        |env                 # альтернатива виртуальному окружению
        |site-packages       # установленные пакеты, если venv назван иначе
        |snap                # ~/snap — данные snap-приложений
        |dist                # артефакты сборки
        |build               # артефакты сборки
        |Thumbs\.db          # Windows служебные файлы
        |~.*                 # временные файлы office
    )$""",
    # Регистр не важен: «Node_Modules», «VENV», «thumbs.db» — тот же мусор.
    re.VERBOSE | re.IGNORECASE,
)


def check_path(p: Path, roots: Iterable[str | Path] = ()) -> bool:
    """Не лежит ли путь в исключаемом месте.

    С `roots` сверяются только компоненты ниже корня, как в полном обходе:
    сам корень и его предки не проверяются, иначе под корнем вида ~/Build
    терялось бы каждое событие watcher'а. Путь под несколькими корнями
    пропускается, если его пропускает хотя бы один. Без корней и вне их
    сверяются все компоненты вплоть до «/».
    """
    inside = False
    for root in roots:
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            continue
        inside = True
        if not any(exclude_pattern.match(part) for part in parts):
            return True
    if inside:
        return False
    if exclude_pattern.match(p.name):
        return False
    for x in p.parents:
        if exclude_pattern.match(x.name):
            return False
    return True


def read_refusal(
    path: str, roots: Iterable[str], follow_symlinks: bool = True
) -> str | None:
    """Почему файл не отдаётся: None — отдаётся, иначе "outside" или "excluded".

    Читается то, до чего доходит обход iter_file_metadata: файл внутри корня, и
    ни одно имя ниже корня не подпадает под exclude_pattern. path — abspath без
    раскрытия ссылок: так обход хранит файлы, найденные за ссылкой, поэтому
    такой путь годится, только если обход по ссылкам идёт. После realpath тот же
    файл сверяется по настоящему пути. Мост повторяет правило в _sandbox_refusal.
    """
    real = os.path.realpath(path)
    inside = False
    for root in roots:
        base = os.path.abspath(os.path.expanduser(root))
        pairs = [(real, os.path.realpath(base))]
        if follow_symlinks:
            pairs.append((path, base))
        for candidate, top in pairs:
            top = top.rstrip(os.sep)
            if candidate != top and not candidate.startswith(top + os.sep):
                continue
            inside = True
            below = candidate[len(top) :].split(os.sep)
            if not any(exclude_pattern.match(part) for part in below):
                return None
    return "excluded" if inside else "outside"
