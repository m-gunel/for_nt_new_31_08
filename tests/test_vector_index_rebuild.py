"""IVF-PQ обязан переобучаться, когда таблица заметно изменилась.

`num_partitions` считается от числа строк: без переобучения на выросшей
таблице каждая проба читает во столько же раз больше векторов, а при малом
числе партиций поиск вырождается в перебор.
"""

import numpy as np
import pyarrow as pa
import pytest

from src.common.doc_meta import doc_meta_schema
from src.utils.lance_wrapper import (
    MIN_ROWS_FOR_VECTOR_INDEX,
    VECTOR_INDEX_REBUILD_FACTOR,
    LanceWrapper,
)

DIM = 32


def _rows(count: int, offset: int = 0) -> pa.Table:
    """`count` строк с непохожими векторами — KMeans нужны разные точки."""
    rng = np.random.default_rng(seed=offset)
    vectors = rng.standard_normal((count, DIM)).astype(np.float32)
    return pa.table(
        {
            "idx": pa.array(range(count), pa.int64()),
            "text": pa.array([f"документ {offset + i}" for i in range(count)]),
            "meta": pa.array([None] * count, pa.struct(doc_meta_schema)),
            "path": pa.array([f"/tmp/doc_{offset + i}.txt" for i in range(count)]),
            "vector": pa.FixedSizeListArray.from_arrays(vectors.reshape(-1), DIM),
        }
    )


@pytest.fixture
def wrapper(tmp_path):
    return LanceWrapper(
        ld_db_path=str(tmp_path / "lance"), embed_dim=DIM, table_name="files"
    )


def _count_builds(wrapper, monkeypatch) -> list[int]:
    """Считать реальные обучения индекса, а не намерения."""
    builds: list[int] = []
    real = type(wrapper.table).create_index

    def spy(self, *args, **kwargs):
        builds.append(kwargs.get("num_partitions"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(type(wrapper.table), "create_index", spy)
    return builds


@pytest.mark.asyncio
async def test_small_table_gets_no_index(wrapper, monkeypatch):
    builds = _count_builds(wrapper, monkeypatch)
    await wrapper.add_batch(_rows(MIN_ROWS_FOR_VECTOR_INDEX - 1))

    wrapper.create_vector_index()

    assert builds == [], "на маленькой таблице полный перебор дешевле индекса"


@pytest.mark.asyncio
async def test_index_is_retrained_when_table_grows(wrapper, monkeypatch):
    builds = _count_builds(wrapper, monkeypatch)
    initial = MIN_ROWS_FOR_VECTOR_INDEX
    await wrapper.add_batch(_rows(initial))
    wrapper.create_vector_index()
    assert len(builds) == 1

    # Рост меньше порога — переобучать незачем.
    await wrapper.add_batch(_rows(initial, offset=initial))
    wrapper.create_vector_index()
    assert len(builds) == 1, "удвоение — не повод платить O(корпус)"

    # Рост в VECTOR_INDEX_REBUILD_FACTOR раз — повод.
    grown = initial * VECTOR_INDEX_REBUILD_FACTOR
    await wrapper.add_batch(_rows(grown - 2 * initial, offset=2 * initial))
    wrapper.create_vector_index()
    assert len(builds) == 2, "на выросшей таблице индекс обязан переобучиться"
    assert builds[1] > builds[0], "число партиций должно вырасти вместе с таблицей"


@pytest.mark.asyncio
async def test_index_is_retrained_after_mass_delete(wrapper, monkeypatch):
    """Сжатие таблицы — тоже повод: партиции набиты мёртвыми PQ-кодами.

    Удаление в Lance мягкое, и отсев идёт после отбора кандидатов.
    """
    builds = _count_builds(wrapper, monkeypatch)
    total = MIN_ROWS_FOR_VECTOR_INDEX * VECTOR_INDEX_REBUILD_FACTOR
    await wrapper.add_batch(_rows(total))
    wrapper.create_vector_index()
    assert len(builds) == 1

    keep = MIN_ROWS_FOR_VECTOR_INDEX
    await wrapper.delete_where(f"idx >= {keep}")
    wrapper.create_vector_index()

    assert len(builds) == 2, "после массового удаления индекс обязан переобучиться"
    assert builds[1] < builds[0], "число партиций должно упасть вместе с таблицей"


@pytest.mark.asyncio
async def test_force_retrains_unconditionally(wrapper, monkeypatch):
    builds = _count_builds(wrapper, monkeypatch)
    await wrapper.add_batch(_rows(MIN_ROWS_FOR_VECTOR_INDEX))
    wrapper.create_vector_index()
    wrapper.create_vector_index()
    assert len(builds) == 1, "без изменений таблицы переобучать нечего"

    wrapper.create_vector_index(force=True)
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_invalidate_forgets_training_size(wrapper, monkeypatch):
    """Иначе после пересоздания таблицы порог считается от чужого размера."""
    await wrapper.add_batch(_rows(MIN_ROWS_FOR_VECTOR_INDEX))
    wrapper.create_vector_index()
    assert wrapper._vector_rows == MIN_ROWS_FOR_VECTOR_INDEX

    wrapper.invalidate_table_cache()

    assert wrapper._vector_rows is None
