"""Таблицы и пачки строк — готовыми чанками в бюджете токенов, для всех форматов.

Таблица идёт серией чанков: в каждом — название, шапка «a | b | c» и строки «x | y | z»,
общий table_id и номера строк. Поиск находит конкретную строку, а в контекст приходит её
шапка; таблица не смешивается с соседней прозой.
"""

from collections.abc import Callable, Iterator

from .doc_model import Block


class Budget:
    """Длина в токенах: дешёвая оценка по символам, токенизатор — только у границы."""

    # 1,6 символа на токен — ниже замеренного минимума 1,83 у rubert-tiny2 на таблицах ЦБ.
    SAFE_CHARS_PER_TOKEN = 1.6

    def __init__(self, limit: int, count_tokens: Callable[[str], int]):
        self.limit, self.count = limit, count_tokens

    def fits(self, text: str) -> bool:
        return (
            len(text) <= self.limit * self.SAFE_CHARS_PER_TOKEN
            or self.count(text) <= self.limit
        )


def _repeatable(prefix: str, budget: Budget) -> bool:
    """Повтор prefix в каждом чанке оставляет строкам хотя бы половину бюджета."""
    return Budget(budget.limit // 2, budget.count).fits(prefix)


def _shorten(text: str, budget: Budget) -> str:
    """Начало text с « …», которое по оценке символами влезает в половину бюджета."""
    n = int(budget.limit // 2 * budget.SAFE_CHARS_PER_TOKEN) - 2
    return text[: max(n, 1)].rstrip() + " …"


def split_parts(
    prefix: str, parts: list[str], budget: Budget, sep: str = "\n"
) -> list[str]:
    """Части по чанкам, каждый с повтором prefix; гигантская часть режется по словам."""
    out: list[str] = []
    cur: list[str] = []
    for p in parts:
        if cur and not budget.fits(prefix + sep.join(cur + [p])):
            out.append(prefix + sep.join(cur))
            cur = [p]
        else:
            cur.append(p)
    if cur or not out:
        out.append(prefix + sep.join(cur))
    res = []
    for t in out:
        if budget.fits(t):
            res.append(t)
            continue
        head = (prefix.rstrip() + " …") if prefix else ""
        if prefix and not _repeatable(prefix, budget):
            # С prefix почти во весь бюджет в каждый кусок влезало бы по слову.
            head = _shorten(prefix.rstrip(), budget)
        step = max(50, int(budget.limit * budget.SAFE_CHARS_PER_TOKEN) - len(head))
        words = [w[k : k + step] for w in t.split(" ") for k in range(0, len(w), step)]
        piece: list[str] = []
        for w in words:
            if piece and not budget.fits(" ".join(piece + [w])):
                res.append(" ".join(piece))
                piece = [head, w] if head else [w]
            else:
                piece.append(w)
        if piece:
            res.append(" ".join(piece))
    return res


def pack_lines(
    prefix: str, lines: list[tuple[str, int | None]], budget: Budget, **place
) -> Iterator[Block]:
    """Строки пачками в бюджете, prefix — в каждой пачке; номера строк — в row_start/row_end."""
    pack: list[tuple[str, int | None]] = []

    def flush() -> Iterator[Block]:
        for text in split_parts(prefix, [t for t, _ in pack], budget):
            yield Block(
                text, row_start=pack[0][1], row_end=pack[-1][1], whole=True, **place
            )

    for line in lines:
        if pack and not budget.fits(prefix + "\n".join(t for t, _ in pack + [line])):
            yield from flush()
            pack = []
        pack.append(line)
    if pack:
        yield from flush()


def table_blocks(
    header: list[str],
    body: list[tuple[list[str], int]],
    budget: Budget,
    *,
    title: str | None = None,
    table_id: str | None = None,
    **place,
) -> list[Block]:
    """Таблица → серия чанков: название, шапка и строки группами в бюджете.

    header — подписи столбцов (уровни уже склеены «верх / низ»), body — (ячейки строки,
    номер строки в таблице). Пустые ячейки и повторы подписей шапки опускаются.
    """
    head = " | ".join(dict.fromkeys(h for h in header if h))
    prefix = "".join(x + "\n" for x in (title, head) if x)
    lines = [(" | ".join(c for c in cells if c), n) for cells, n in body]
    lines = [x for x in lines if x[0]]
    if lines and prefix and not _repeatable(prefix, budget):
        # Шапка-простыня (абзац в первой строке, многоуровневая шапка широкой
        # формы): иначе строкам не остаётся бюджета. Целиком — один раз первой
        # строкой, в каждом чанке — её начало, как длинная подпись строки в xlsx.
        lines.insert(0, (prefix.rstrip("\n"), lines[0][1]))
        prefix = _shorten(prefix.rstrip("\n"), budget) + "\n"
    return list(
        pack_lines(
            prefix,
            lines,
            budget,
            kind="table",
            table_id=table_id,
            **place,
        )
    )
