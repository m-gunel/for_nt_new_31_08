"""Утилиты подготовки модели (utils/*): пути, seed калибровки, выбор ONNX-модели.

Группы torch (sentence-transformers, optimum, datasets) в тестовом окружении
нет: проверяется то, что работает без неё.
"""

import importlib
import io
import runpy
import sys
import types
from pathlib import Path

import polars as pl
import pytest

# utils/ в контекст docker-сборки не копируется, а тесты там гоняются.
pytestmark = pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "utils").is_dir(),
    reason="нет каталога utils/",
)


def test_run_openvino_encodes_with_shipped_model(capsys):
    runpy.run_module("utils.run_openvino", run_name="__main__")
    assert "Embeddings shape: (2, 312)" in capsys.readouterr().out


def _posts(path, count):
    texts = [f"пост {i}_x000D_строка " for i in range(count)]
    pl.DataFrame({"Текст": texts}).write_parquet(path)


def test_calibration_sample_is_reproducible(tmp_path):
    convert = importlib.import_module("utils.convert_openvino")
    path = tmp_path / "posts.pq"
    _posts(path, 1000)

    first = convert.load_calibration(path, "Текст", 256, seed=0)

    assert first.equals(convert.load_calibration(path, "Текст", 256, seed=0))
    assert first.columns == ["text"] and first.height == 256
    assert first["text"][0].endswith("\nстрока")


def test_convert_openvino_refuses_dir_without_fp32_weights(tmp_path):
    # В каталоге поставки только квантованная модель: квантовать его нельзя.
    convert = importlib.import_module("utils.convert_openvino")
    calib = tmp_path / "posts.pq"
    _posts(calib, 1)

    with pytest.raises(SystemExit) as exc:
        convert.main(["--model", "models/rubert-tiny2_002", "--calib", str(calib)])
    assert "FP32" in str(exc.value.code)


def test_convert_openvino_requires_calibration_file(tmp_path):
    convert = importlib.import_module("utils.convert_openvino")
    with pytest.raises(SystemExit) as exc:
        convert.main(["--model", str(tmp_path), "--calib", str(tmp_path / "нет.pq")])
    assert "калибровки" in str(exc.value.code)


def test_convert_tokenizer_requires_model_dir(tmp_path):
    tokenizer = importlib.import_module("utils.convert_openvino_tokenizer")
    with pytest.raises(SystemExit) as exc:
        tokenizer.main(["--model", str(tmp_path / "нет")])
    assert "нет каталога модели" in str(exc.value.code)


@pytest.fixture
def export_onnx(monkeypatch):
    # Модулю на импорте нужны только два имени из sentence_transformers.
    stub = types.ModuleType("sentence_transformers")
    stub.SentenceTransformer = None
    stub.export_dynamic_quantized_onnx_model = None
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)
    sys.modules.pop("utils.export_onnx", None)
    yield importlib.import_module("utils.export_onnx")
    sys.modules.pop("utils.export_onnx", None)


@pytest.mark.parametrize(
    "flags, expected",
    [
        # Skylake-SP: AVX-512 без VNNI — qint8 без reduce_range там насыщается.
        ("sse4_2 avx2 avx512f avx512dq avx512bw avx512vl", "avx2"),
        ("avx2 avx512f avx512_vnni", "avx512_vnni"),
        ("avx2 avx_vnni", "avx512_vnni"),
        ("sse4_2 avx2", "avx2"),
    ],
)
def test_detect_cpu_features_reads_vnni_flags(
    export_onnx, monkeypatch, flags, expected
):
    cpuinfo = (
        f"processor\t: 0\nmodel name\t: Intel(R) Xeon(R) CPU\nflags\t\t: {flags}\n"
    )
    monkeypatch.setattr(export_onnx.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        export_onnx, "open", lambda *a, **k: io.StringIO(cpuinfo), raising=False
    )
    assert export_onnx.detect_cpu_features() == expected


@pytest.mark.parametrize("entry", ["load_optimal_model", "test_exported_model"])
def test_cls_pooling_takes_dimension_from_model(export_onnx, monkeypatch, entry):
    seen = {}

    class Transformer:
        def __init__(self, path):
            pass

        def get_word_embedding_dimension(self):
            return 768

    class Pooling:
        def __init__(self, dim, **kwargs):
            seen["dim"] = dim

    class SentenceTransformer:
        def __init__(self, *args, **kwargs):
            pass

        def encode(self, sentences):
            return types.SimpleNamespace(shape=(len(sentences), seen["dim"]))

    models = types.SimpleNamespace(Transformer=Transformer, Pooling=Pooling)
    monkeypatch.setattr(
        sys.modules["sentence_transformers"], "models", models, raising=False
    )
    monkeypatch.setattr(export_onnx, "SentenceTransformer", SentenceTransformer)
    monkeypatch.setattr(export_onnx, "detect_cpu_features", lambda: "avx2")

    getattr(export_onnx, entry)("unused", use_cls_pooling=True)

    assert seen["dim"] == 768
