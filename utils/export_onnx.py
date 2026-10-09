# pip install "sentence-transformers[onnx]>=3.2.0" onnxruntime
import os
import platform
import subprocess

from sentence_transformers import (
    SentenceTransformer,
    export_dynamic_quantized_onnx_model,
)


def detect_cpu_features():
    """Конфиг квантования под текущий CPU: avx512_vnni или avx2."""
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["wmic", "cpu", "get", "name"], capture_output=True, text=True
            )
            cpu_name = result.stdout.lower()
        else:
            if platform.system() == "Darwin":
                result = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True,
                    text=True,
                )
                cpu_name = result.stdout.lower()
            else:  # Linux
                with open("/proc/cpuinfo", "r") as f:
                    cpu_name = f.read().lower()

        # AVX512 VNNI есть у Intel начиная с Ice Lake.
        ice_lake_plus = any(
            x in cpu_name
            for x in [
                "ice lake",
                "tiger lake",
                "rocket lake",
                "alder lake",
                "sapphire rapids",
                "emerald rapids",
                "granite rapids",
            ]
        )

        # Флаги есть только в /proc/cpuinfo. Подстрока «avx512» есть и у avx512f
        # без VNNI (Skylake-SP), где qint8-модель без reduce_range может
        # насыщаться; AVX-VNNI (Alder Lake) считает те же int8 без насыщения.
        flags = set()
        for line in cpu_name.splitlines():
            if line.startswith("flags"):
                flags.update(line.partition(":")[2].split())

        if ice_lake_plus or flags & {"avx512_vnni", "avx_vnni"}:
            return "avx512_vnni"
        else:
            return "avx2"

    except Exception:
        return "avx2"


def get_optimal_model_path(output_dir):
    cpu_config = detect_cpu_features()
    onnx_dir = os.path.join(output_dir, "onnx")

    if cpu_config == "avx512_vnni":
        avx512_path = os.path.join(onnx_dir, "model_avx512_vnni.onnx")
        if os.path.exists(avx512_path):
            return "onnx/model_avx512_vnni.onnx"

    return "onnx/model_avx2.onnx"


def export_and_quantize_sentence_transformer(model_id, output_dir="./exported_model"):
    """ONNX-экспорт и две int8-модели для x86 в <output_dir>/onnx/.

    model_avx2.onnx (quint8) и model_avx512_vnni.onnx (qint8); какую брать на
    текущем CPU, решает get_optimal_model_path.
    """
    print(f"Loading model: {model_id}")

    print("Creating output directory...")
    os.makedirs(output_dir, exist_ok=True)

    print("Exporting base ONNX model...")
    from pathlib import Path

    from optimum.exporters.onnx import main_export

    main_export(
        model_name_or_path=model_id,
        output=Path(output_dir),
        task="feature-extraction",
        opset=14,  # с 14-го поддерживается scaled_dot_product_attention
        framework="pt",
        device="cpu",
        fp16=False,
        optimize=None,
    )

    print("Loading exported model as SentenceTransformer...")
    onnx_model = SentenceTransformer(output_dir, backend="onnx")

    print("Applying graph optimization and INT8 quantization for Intel x86...")

    quantization_configs = ["avx2", "avx512_vnni"]

    for config in quantization_configs:
        print(f"  - Creating {config} quantized model...")
        export_dynamic_quantized_onnx_model(
            model=onnx_model,
            quantization_config=config,  # type: ignore[arg-type]
            model_name_or_path=output_dir,
        )

    onnx_dir = os.path.join(output_dir, "onnx")
    target_files = {
        "model_quint8_avx2.onnx": "model_avx2.onnx",
        "model_qint8_avx512_vnni.onnx": "model_avx512_vnni.onnx",
    }

    print("Cleaning up unnecessary files...")

    original_model_path = os.path.join(output_dir, "model.onnx")
    if os.path.exists(original_model_path):
        original_size_mb = os.path.getsize(original_model_path) / (1024 * 1024)
        os.remove(original_model_path)
        print(
            f"  - Removed original unquantized model.onnx ({original_size_mb:.1f} MB)"
        )

    for file in os.listdir(onnx_dir):
        if file.endswith(".onnx"):
            if file in target_files:
                new_name = target_files[file]
                os.rename(
                    os.path.join(onnx_dir, file), os.path.join(onnx_dir, new_name)
                )
                print(f"  - Kept and renamed: {file} -> {new_name}")
            else:
                os.remove(os.path.join(onnx_dir, file))
                print(f"  - Removed: {file}")

    print("\nExport complete! Optimized models saved:")
    for target_file in target_files.values():
        model_path = os.path.join(onnx_dir, target_file)
        if os.path.exists(model_path):
            size_mb = os.path.getsize(model_path) / (1024 * 1024)
            print(f"  - {model_path} ({size_mb:.1f} MB)")

    print("  - Both models are INT8 quantized for faster inference and smaller size")

    recommended_config = detect_cpu_features()
    print(f"  - Recommended for your CPU: {recommended_config}")

    return onnx_model


def test_exported_model(output_dir, test_sentences=None, use_cls_pooling=False):
    if test_sentences is None:
        test_sentences = ["This is a test sentence.", "Another example sentence."]

    print("\nTesting exported model...")

    try:
        optimal_model_file = get_optimal_model_path(output_dir)
        print(f"Using optimal model: {optimal_model_file}")

        if use_cls_pooling:
            from sentence_transformers import models

            transformer = models.Transformer(output_dir)
            # CLS-пулинг (первый токен); размерность — из конфига модели.
            embedding_dim = transformer.get_word_embedding_dimension()
            pooling = models.Pooling(
                embedding_dim,
                pooling_mode_cls_token=True,
                pooling_mode_mean_tokens=False,
                pooling_mode_max_tokens=False,
            )

            model = SentenceTransformer(
                modules=[transformer, pooling],
                backend="onnx",
                model_kwargs={"file_name": optimal_model_file},
            )
            print("Using CLS pooling strategy")
        else:
            model = SentenceTransformer(
                output_dir,
                backend="onnx",
                model_kwargs={"file_name": optimal_model_file},
            )
            print("Using default mean pooling strategy")

        embeddings = model.encode(test_sentences)
        print(f"Model output shape: {embeddings.shape}")
        print("✅ Model test successful!")

        return model

    except Exception as e:
        print(f"❌ Model test failed: {e}")
        print(
            "This might be expected for some models - the main export was successful!"
        )
        return None


def load_optimal_model(output_dir, use_cls_pooling=False):
    """ONNX-модель под текущий CPU; use_cls_pooling — CLS-пулинг вместо mean."""
    optimal_model_file = get_optimal_model_path(output_dir)
    cpu_config = detect_cpu_features()

    print(f"Loading optimal model for CPU: {cpu_config}")
    print(f"Model file: {optimal_model_file}")

    if use_cls_pooling:
        from sentence_transformers import models

        transformer = models.Transformer(output_dir)
        # CLS-пулинг (первый токен); размерность — из конфига модели.
        embedding_dim = transformer.get_word_embedding_dimension()
        pooling = models.Pooling(
            embedding_dim,
            pooling_mode_cls_token=True,
            pooling_mode_mean_tokens=False,
            pooling_mode_max_tokens=False,
        )

        model = SentenceTransformer(
            modules=[transformer, pooling],
            backend="onnx",
            model_kwargs={"file_name": optimal_model_file},
        )
        print("Using CLS pooling strategy")
    else:
        model = SentenceTransformer(
            output_dir,
            backend="onnx",
            model_kwargs={"file_name": optimal_model_file},
        )
        print("Using default mean pooling strategy")

    return model


if __name__ == "__main__":
    model_id = "./data/models/rubert-tiny2"
    output_directory = "./my_exported_model"

    exported_model = export_and_quantize_sentence_transformer(
        model_id, output_directory
    )

    print("\n" + "=" * 50)
    print("Testing with mean pooling (default):")
    test_exported_model(output_directory, use_cls_pooling=False)

    print("\n" + "=" * 50)
    print("Testing with CLS pooling:")
    test_exported_model(output_directory, use_cls_pooling=True)

    print("\n" + "=" * 50)
    print("✅ Complete! Usage examples:")
    print("\n1. Load optimal model automatically (mean pooling):")
    print("from sentence_transformers import SentenceTransformer")
    print(f"model = SentenceTransformer('{output_directory}', backend='onnx')")
    print("embeddings = model.encode(['Your text here'])")

    print("\n2. Load optimal model with automatic CPU detection:")
    print("from utils.export_onnx import load_optimal_model")
    print(f"model = load_optimal_model('{output_directory}')")
    print("embeddings = model.encode(['Your text here'])")

    print("\n3. Load with CLS pooling:")
    print("from utils.export_onnx import load_optimal_model")
    print(f"model = load_optimal_model('{output_directory}', use_cls_pooling=True)")
    print("embeddings = model.encode(['Your text here'])")
