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
from src.utils.openvino_encoder import HFTokenizer, OpenVINOEmbedder, read_model

# Фикстуры поднимают Index на временном хранилище — переиспользуем их.
from tests.test_indexing_live import (  # noqa: F401
    _chunk_table,
    index,
    isolated_config,
)

E5 = Path("models/multilingual-e5-small")


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


def test_e5_parts_reassemble_to_the_checked_weights():
    """Части в репозитории — ровно тот файл, что проверяли на стенде."""
    import hashlib

    parts = sorted((E5 / "openvino").glob("openvino_model_qint8_quantized.bin.part*"))
    assert len(parts) == 3 and all(p.stat().st_size < 50 * 2**20 for p in parts)
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in parts)).hexdigest()
    assert digest == "de1b89d9da41a6a4edf7a976d87fa0dc4ab54fafc754f8b9d08bbf257c16505b"


def test_pooling_and_prefixes_come_from_model_files(tmp_path):
    e5 = OpenVINOEmbedder.__new__(OpenVINOEmbedder)
    e5._path = str(E5)
    assert e5.pooling == "mean"
    assert e5.prefix == {"query": "query: ", "document": "passage: "}
    assert e5._apply_prefix(["Отчёт ЦБ"], "document") == ["passage: отчёт цб"]

    # Каталог без описаний — как у rubert-tiny2_002 до дообучения: CLS и без префиксов.
    bare = OpenVINOEmbedder.__new__(OpenVINOEmbedder)
    bare._path = str(tmp_path)
    assert bare.pooling == "cls"
    assert bare.prefix is None


def test_mean_pooling_ignores_padding():
    e5 = OpenVINOEmbedder.__new__(OpenVINOEmbedder)
    e5._path = str(E5)
    hidden = np.array([[[1.0, 1.0], [3.0, 3.0], [100.0, 100.0]]])
    mask = np.array([[1, 1, 0]])
    np.testing.assert_allclose(e5._pool(hidden, mask), [[2.0, 2.0]])


def test_last_token_pooling_skips_padding(tmp_path):
    """jina берёт вектор последнего настоящего токена, а не последней позиции батча."""
    (tmp_path / "1_Pooling").mkdir()
    (tmp_path / "1_Pooling/config.json").write_text(
        json.dumps({"pooling_mode_lasttoken": True})
    )
    emb = OpenVINOEmbedder.__new__(OpenVINOEmbedder)
    emb._path = str(tmp_path)
    assert emb.pooling == "lasttoken"
    hidden = np.arange(12, dtype=np.float32).reshape(2, 3, 2)
    mask = np.array([[1, 1, 1], [1, 1, 0]])
    np.testing.assert_allclose(emb._pool(hidden, mask), [hidden[0, 2], hidden[1, 1]])


def _tiny_model_with_tokenizer_json(path: Path) -> None:
    """Каталог модели без OpenVINO-токенизатора: tokenizer.json и IR, где вектор токена — строка таблицы."""
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    vocab = {"<pad>": 0, "<eos>": 1, "a": 2, "b": 3, "c": 4}
    tok = Tokenizer(models.WordLevel(vocab, unk_token="<pad>"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.post_processor = processors.TemplateProcessing(
        single="$A <eos>", special_tokens=[("<eos>", 1)]
    )
    tok.save(str(path / "tokenizer.json"))
    (path / "tokenizer_config.json").write_text(json.dumps({"pad_token": "<pad>"}))
    (path / "config.json").write_text(
        json.dumps({"hidden_size": 4, "max_position_embeddings": 8})
    )
    (path / "1_Pooling").mkdir()
    (path / "1_Pooling/config.json").write_text(
        json.dumps({"word_embedding_dimension": 4, "pooling_mode_lasttoken": True})
    )
    table = np.arange(20, dtype=np.float32).reshape(5, 4) + 1
    ids = ops.parameter([-1, -1], ov.Type.i64, name="input_ids")
    mask = ops.parameter([-1, -1], ov.Type.i64, name="attention_mask")
    model = ov.Model([ops.gather(ops.constant(table), ids, 0)], [ids, mask])
    (path / "openvino").mkdir()
    ov.save_model(model, path / "openvino/model.xml", compress_to_fp16=False)


def test_tokenizer_json_when_no_openvino_tokenizer(tmp_path):
    """jina: OpenVINO-токенизатор не умеет ignore_merges — энкодер берёт tokenizer.json."""
    pytest.importorskip("tokenizers")
    _tiny_model_with_tokenizer_json(tmp_path)
    emb = OpenVINOEmbedder(str(tmp_path), openvino="openvino/model.xml")
    try:
        assert isinstance(emb.tokenizer, HFTokenizer)
        assert emb.count_tokens("a b") == 3  # два слова и <eos>
        # У «c» короче: дополнение <pad> справа. Последний настоящий токен у обоих — <eos>,
        # поэтому векторы совпадают; возьми пулинг последнюю позицию, у «c» был бы <pad>.
        vecs = emb.encode(["a b", "c"], task="document")
        np.testing.assert_allclose(vecs[0], vecs[1], rtol=1e-6)
    finally:
        emb.shutdown()


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


def test_config_takes_dimension_from_e5(config):
    assert config.embed_dim == 384
    assert config.ov_model.encode(["проверка"]).shape == (1, 384)


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
    await store.add_batch(_chunk_table("/tmp/a.txt", "договор", index.cfg.embed_dim))
    assert await index.migrate_embedding_model() is False  # метка появилась
    assert await index.migrate_embedding_model() is False  # и совпала
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
    """Индекс 1.0.9 и раньше: метки нет, векторы rubert-tiny2 на 312 чисел."""
    old = LanceWrapper(index.cfg.ld_db_path, 312, table_name="files")
    await old.add_batch(_chunk_table("/tmp/a.txt", "договор", 312))
    index.files_store.invalidate_table_cache()
    assert index.files_store.vector_dim() == 312

    assert await index.migrate_embedding_model() is True
    assert index.files_store.vector_dim() is None
    # Таблица создаётся заново уже под новую модель.
    await index.files_store.add_batch(
        _chunk_table("/tmp/b.txt", "счёт", index.cfg.embed_dim)
    )
    assert index.files_store.vector_dim() == index.cfg.embed_dim == 384


@pytest.mark.asyncio
async def test_unlabeled_index_of_same_dimension_is_trusted(index: Index):  # noqa: F811
    store = index.files_store
    await store.add_batch(_chunk_table("/tmp/a.txt", "договор", index.cfg.embed_dim))
    assert not _label(index).exists()

    assert await index.migrate_embedding_model() is False
    assert _rows(store) == 1
    assert _label(index).exists()
