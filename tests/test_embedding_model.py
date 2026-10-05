"""Модель эмбеддингов: параметры из её файлов, веса частями и пересборка индекса при смене модели.

До этого энкодер был заточен под rubert-tiny2: CLS-пулинг зашит, префиксы не читались,
размерность угадывалась по имени каталога (у неугаданного имени — 1024, и запись в
LanceDB падала на схеме). А индекс, посчитанный старой моделью, после смены модели
оставался как есть: LSM считает файлы готовыми, и векторы разных моделей смешивались.
"""

import json
from pathlib import Path

import numpy as np
import openvino as ov
import openvino.opset13 as ops
import pytest

from src.config import model_embed_dim
from src.indexer.indexer import EMBEDDING_MODEL_FILE, Index
from src.utils.lance_wrapper import LanceWrapper
from src.utils.openvino_encoder import OpenVINOEmbedder, read_model

# Фикстуры поднимают Index на временном хранилище — переиспользуем их.
from tests.test_indexing_live import (  # noqa: F401
    _chunk_table,
    index,
    isolated_config,
)

FRIDA = Path("models/rubert-mini-frida")


def test_weights_in_parts_load_like_whole_file(tmp_path):
    """GitHub не принимает файлы больше 100 МБ — веса лежат частями, read_model их склеивает."""
    rng = np.random.default_rng(0)
    weight = rng.standard_normal((64, 32)).astype(np.float32)
    x = ops.parameter([1, 64], ov.Type.f32, name="x")
    model = ov.Model([ops.matmul(x, ops.constant(weight), False, False)], [x])
    whole = tmp_path / "whole" / "model.xml"
    whole.parent.mkdir()
    ov.save_model(model, whole, compress_to_fp16=False)
    data = whole.with_suffix(".bin").read_bytes()

    parted = tmp_path / "parted" / "model.xml"
    parted.parent.mkdir()
    parted.write_bytes(whole.read_bytes())
    third = len(data) // 3 + 1
    for i in range(3):
        (parted.parent / f"model.bin.part{i:02d}").write_bytes(
            data[i * third : (i + 1) * third]
        )
    assert not parted.with_suffix(".bin").exists()

    core = ov.Core()
    inp = rng.standard_normal((1, 64)).astype(np.float32)
    want = core.compile_model(read_model(core, whole), "CPU")(inp)[0]
    got = core.compile_model(read_model(core, parted), "CPU")(inp)[0]
    np.testing.assert_allclose(got, want, rtol=1e-6)


def test_weights_are_the_checked_ones():
    """Веса в репозитории — ровно тот файл, что проверяли на стенде."""
    import hashlib

    data = (FRIDA / "openvino/openvino_model_qint8_quantized.bin").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == "a54d8c4eb60db1dd3b4dc7ecf412a0b58aa0c897e18ccfb2f33bb4fa1f40e696"


def test_pooling_and_prefixes_come_from_model_files(tmp_path):
    frida = OpenVINOEmbedder.__new__(OpenVINOEmbedder)
    frida._path = str(FRIDA)
    assert frida.pooling == "mean"
    assert frida.prefix == {"query": "search_query: ", "document": "search_document: "}
    assert frida._apply_prefix(["Отчёт ЦБ"], "document") == [
        "search_document: отчёт цб"
    ]

    # Каталог без описаний — как у rubert-tiny2_002 до дообучения: CLS и без префиксов.
    bare = OpenVINOEmbedder.__new__(OpenVINOEmbedder)
    bare._path = str(tmp_path)
    assert bare.pooling == "cls"
    assert bare.prefix is None


def test_mean_pooling_ignores_padding():
    frida = OpenVINOEmbedder.__new__(OpenVINOEmbedder)
    frida._path = str(FRIDA)
    hidden = np.array([[[1.0, 1.0], [3.0, 3.0], [100.0, 100.0]]])
    mask = np.array([[1, 1, 0]])
    np.testing.assert_allclose(frida._pool(hidden, mask), [[2.0, 2.0]])


@pytest.mark.parametrize(
    ("files", "dim"),
    [
        (
            {"1_Pooling/config.json": {"word_embedding_dimension": 384}},
            384,
        ),  # до sentence-transformers 6
        ({"1_Pooling/config.json": {"embedding_dimension": 312}}, 312),  # с 6
        ({"config.json": {"hidden_size": 768}}, 768),
        ({}, None),
    ],
)
def test_embed_dim_from_model_files(tmp_path, files, dim):
    for name, data in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(json.dumps(data))
    assert model_embed_dim(tmp_path) == dim


def test_config_takes_dimension_from_mini_frida(config):
    assert config.embed_dim == 312
    assert config.ov_model.encode(["проверка"]).shape == (1, 312)


# ---------- пересборка индекса при смене модели ----------


def _label(idx: Index) -> Path:
    return Path(idx.cfg.lsm_root) / EMBEDDING_MODEL_FILE


def _rows(store: LanceWrapper) -> int:
    return store.table.count_rows() if store.vector_dim() is not None else 0


@pytest.mark.asyncio
async def test_fresh_install_just_writes_the_label(index: Index):  # noqa: F811
    assert await index.migrate_embedding_model() is False
    assert json.loads(_label(index).read_text()) == index.cfg.embed_model_signature


@pytest.mark.asyncio
async def test_same_model_keeps_the_index(index: Index):  # noqa: F811
    store = index.files_store
    assert await index.migrate_embedding_model() is False  # чистая установка — метка
    await store.add_batch(_chunk_table("/tmp/a.txt", "договор", index.cfg.embed_dim))
    assert await index.migrate_embedding_model() is False  # та же модель
    assert _rows(store) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_other_model_resets_the_index(index: Index, active):  # noqa: F811
    store = index.files_store
    await store.add_batch(_chunk_table("/tmp/a.txt", "договор", index.cfg.embed_dim))
    _label(index).write_text(
        json.dumps({"model": "rubert-tiny2_002", "dim": 384, "sha256": "x"})
    )
    index.active = active

    assert await index.migrate_embedding_model() is True
    assert store.vector_dim() is None, "векторы старой модели должны уйти"
    assert index.active is active, (
        "смена модели — не ручной сброс: индексация не выключается"
    )
    assert json.loads(_label(index).read_text()) == index.cfg.embed_model_signature
    assert await index.migrate_embedding_model() is False


@pytest.mark.asyncio
async def test_unlabeled_index_of_other_dimension_is_rebuilt(index: Index):  # noqa: F811
    old = LanceWrapper(index.cfg.ld_db_path, 384, table_name="files")
    await old.add_batch(_chunk_table("/tmp/a.txt", "договор", 384))
    index.files_store.invalidate_table_cache()

    assert await index.migrate_embedding_model() is True
    assert index.files_store.vector_dim() is None
    await index.files_store.add_batch(
        _chunk_table("/tmp/b.txt", "счёт", index.cfg.embed_dim)
    )
    assert index.files_store.vector_dim() == index.cfg.embed_dim == 312


@pytest.mark.asyncio
async def test_unlabeled_rubert_index_is_rebuilt_despite_same_dimension(index: Index):  # noqa: F811
    """Обновление с 1.0.9: метки нет, векторы rubert-tiny2 той же размерности 312.

    По размерности его не отличить от индекса mini-frida — сверка по ней оставила бы
    старые векторы, и запросы новой моделью искали бы по ним молча вслепую.
    """
    store = index.files_store
    await store.add_batch(_chunk_table("/tmp/a.txt", "договор", 312))
    assert store.vector_dim() == index.cfg.embed_dim == 312

    assert await index.migrate_embedding_model() is True
    assert store.vector_dim() is None
    assert json.loads(_label(index).read_text())["model"] == "rubert-mini-frida"


@pytest.mark.asyncio
async def test_unlabeled_index_is_trusted_for_the_legacy_model(
    index: Index,  # noqa: F811
    monkeypatch,
):
    """Если модель по-прежнему rubert-tiny2_002, индекс без метки — её, пересобирать нечего."""
    legacy = {"model": "rubert-tiny2_002", "dim": index.cfg.embed_dim, "sha256": "x"}
    monkeypatch.setitem(index.cfg.__dict__, "embed_model_signature", legacy)
    store = index.files_store
    await store.add_batch(_chunk_table("/tmp/a.txt", "договор", index.cfg.embed_dim))

    assert await index.migrate_embedding_model() is False
    assert _rows(store) == 1
    assert json.loads(_label(index).read_text()) == legacy
