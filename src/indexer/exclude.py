import re
from pathlib import Path

# Шаблон для исключения из сканирования всякого мусора. Сверяется с ИМЕНЕМ
# одного компонента пути (см. check_path), а не с путём целиком.
#
# Скрытые имена покрыты одной группой `\..+`, поэтому перечислять отдельно
# .venv/.git/.svn/.hg/.idea/.vscode/.DS_Store/.Trash* не нужно — это были
# восемь мёртвых альтернатив, создававших впечатление, что список исчерпывающий.
# Здесь остаётся только то, что НЕ начинается с точки.
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
    re.VERBOSE,
)


def check_path(p: Path):
    if exclude_pattern.match(p.name):
        return False
    for x in p.parents:
        if exclude_pattern.match(x.name):
            return False
    return True
