"""Сборка FTS переживает параллельный CreateIndex на той же таблице.

LanceDB коммитит индексы оптимистически: проигравший получает «Retryable
commit conflict… Please retry», и без повтора эта ошибка уходила бы из
create_table и create_fts наружу.
"""

import lancedb as ld
import pyarrow as pa
import pytest
from lancedb.index import FTS
from lancedb.table import LanceTable

from src.utils import lance_wrapper
from src.utils.lance_wrapper import LanceWrapper, _is_commit_conflict

DIM = 8

CONFLICT = (
    "lance error: Retryable commit conflict for version 2: This CreateIndex "
    "transaction was preempted by concurrent transaction CreateIndex at "
    "version 2. Please retry."
)


@pytest.fixture
def wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(lance_wrapper, "FTS_CONFLICT_BACKOFF_S", 0.0)
    return LanceWrapper(
        ld_db_path=str(tmp_path / "lance"), embed_dim=DIM, table_name="files"
    )


def _flaky_fts(monkeypatch, conflicts: int | None) -> dict[str, int]:
    """create_index падает конфликтом первые `conflicts` вызовов.

    Патчим класс таблицы, а не экземпляр: _build_fts открывает хендл заново
    на каждую попытку. `conflicts=None` — падать всегда.
    """
    calls = {"n": 0}
    real = LanceTable.create_index

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if conflicts is None or calls["n"] <= conflicts:
            raise RuntimeError(CONFLICT)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(LanceTable, "create_index", flaky)
    return calls


def test_conflicting_commit_is_retried(wrapper, monkeypatch):
    wrapper.create_table()
    calls = _flaky_fts(monkeypatch, conflicts=1)

    wrapper.create_fts(force=True)

    assert calls["n"] == 2, "первая попытка проиграла конфликт, вторая построила"


def test_unrelated_runtime_errors_are_not_retried(wrapper, monkeypatch):
    # Повтор лечит только конфликт конкурентности, прочие ошибки он бы
    # только затянул.
    wrapper.create_table()
    calls = {"n": 0}

    def broken(self, *args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("не конфликт")

    monkeypatch.setattr(LanceTable, "create_index", broken)

    with pytest.raises(RuntimeError, match="не конфликт"):
        wrapper.create_fts(force=True)
    assert calls["n"] == 1


def test_exhausted_retries_raise(wrapper, monkeypatch):
    wrapper.create_table()
    calls = _flaky_fts(monkeypatch, conflicts=None)

    with pytest.raises(RuntimeError, match="Retryable commit conflict"):
        wrapper.create_fts(force=True)
    assert calls["n"] == lance_wrapper.FTS_CONFLICT_TRIES


def test_conflict_with_index_built_by_neighbour_counts_as_built(wrapper, monkeypatch):
    # Две сборки FTS на свежей таблице: сосед закоммитил тот же индекс
    # первым. Повтор с replace=False упал бы на «индекс уже существует».
    calls = {"n": 0}
    real = LanceTable.create_index

    def neighbour_wins(self, *args, **kwargs):
        calls["n"] += 1
        real(self, *args, **kwargs)
        raise RuntimeError(CONFLICT)

    monkeypatch.setattr(LanceTable, "create_index", neighbour_wins)

    wrapper.create_table()

    assert calls["n"] == 1
    assert wrapper.fts_created


def test_real_lance_conflict_is_recognised(tmp_path):
    """Формулировка настоящей LanceDB: после обновления библиотеки повтор не
    должен молча перестать срабатывать."""
    db = ld.connect(str(tmp_path))
    db.create_table("files", pa.table({"text": pa.array([], pa.string())}))
    # Хендл держит снимок без индекса и проигрывает уже закоммиченному.
    stale = db.open_table("files")
    db.open_table("files").create_index("text", config=FTS())

    with pytest.raises(RuntimeError) as caught:
        stale.create_index("text", config=FTS())

    assert _is_commit_conflict(caught.value), (
        f"незнакомая формулировка LanceDB: {caught.value}"
    )
