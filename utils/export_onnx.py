# pip install "sentence-transformers[onnx]>=3.2.0" onnxruntime
import os
import platform
import subprocess

from sentence_transformers import (
    SentenceTransformer,
    export_dynamic_quantized_onnx_model,
)


def detect_cpu_features():
    """Detect CPU features to choose the best quantization config"""
    try:
        # Try to detect AVX512 VNNI support
        if platform.system() == "Windows":
            # On Windows, check via wmic or registry
            result = subprocess.run(
                ["wmic", "cpu", "get", "name"], capture_output=True, text=True
            )
            cpu_name = result.stdout.lower()
        else:
            # On Linux/macOS, check /proc/cpuinfo or sysctl
            if platform.system() == "Darwin":  # macOS
                result = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True,
                    text=True,
                )
                cpu_name = result.stdout.lower()
            else:  # Linux
                with open("/proc/cpuinfo", "r") as f:
                    cpu_name = f.read().lower()

        # Check for AVX512 VNNI support (Intel Ice Lake and newer)
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

        if ice_lake_plus or "avx512" in cpu_name:
            return "avx512_vnni"
        else:
            return "avx2"

    except Exception:
        # Default to AVX2 for compatibility
        return "avx2"


def get_optimal_model_path(output_dir):
    """Get the optimal model path based on CPU capabilities"""
    cpu_config = detect_cpu_features()
    onnx_dir = os.path.join(output_dir, "onnx")

    if cpu_config == "avx512_vnni":
        avx512_path = os.path.join(onnx_dir, "model_avx512_vnni.onnx")
        if os.path.exists(avx512_path):
            return "onnx/model_avx512_vnni.onnx"

    # Fallback to AVX2
    return "onnx/model_avx2.onnx"


def export_and_quantize_sentence_transformer(model_id, output_dir="./exported_model"):
    """
    Export a single optimized qint8 ONNX model for Intel x86 processors
    """
    print(f"Loading model: {model_id}")

    # Step 1: Create output directory
    print("Creating output directory...")
    os.makedirs(output_dir, exist_ok=True)

    # Step 2: Export base ONNX model using the direct export method
    print("Exporting base ONNX model...")
    from pathlib import Path

    from optimum.exporters.onnx import main_export

    # Export with higher opset version
    main_export(
        model_name_or_path=model_id,
        output=Path(output_dir),
        task="feature-extraction",
        opset=14,  # Use opset 14 to support scaled_dot_product_attention
        framework="pt",
        device="cpu",
        fp16=False,
        optimize=None,
    )

    # Step 3: Now load as SentenceTransformer with ONNX backend
    print("Loading exported model as SentenceTransformer...")
    onnx_model = SentenceTransformer(output_dir, backend="onnx")

    # Step 4: Export both AVX2 and AVX512_VNNI quantized models for Intel x86
    print("Applying graph optimization and INT8 quantization for Intel x86...")

    quantization_configs = ["avx2", "avx512_vnni"]

    for config in quantization_configs:
        print(f"  - Creating {config} quantized model...")
        export_dynamic_quantized_onnx_model(
            model=onnx_model,
            quantization_config=config,  # type: ignore[arg-type]
            model_name_or_path=output_dir,
        )

    # Clean up - keep only the quantized models
    onnx_dir = os.path.join(output_dir, "onnx")
    target_files = {
        "model_quint8_avx2.onnx": "model_avx2.onnx",
        "model_qint8_avx512_vnni.onnx": "model_avx512_vnni.onnx",
    }

    print("Cleaning up unnecessary files...")

    # Remove the original unquantized model.onnx from the root directory
    original_model_path = os.path.join(output_dir, "model.onnx")
    if os.path.exists(original_model_path):
        original_size_mb = os.path.getsize(original_model_path) / (1024 * 1024)
        os.remove(original_model_path)
        print(
            f"  - Removed original unquantized model.onnx ({original_size_mb:.1f} MB)"
        )

    # Clean up onnx directory and rename target files
    for file in os.listdir(onnx_dir):
        if file.endswith(".onnx"):
            if file in target_files:
                # Rename the target file to simple name
                new_name = target_files[file]
                os.rename(
                    os.path.join(onnx_dir, file), os.path.join(onnx_dir, new_name)
                )
                print(f"  - Kept and renamed: {file} -> {new_name}")
            else:
                # Remove other ONNX files
                os.remove(os.path.join(onnx_dir, file))
                print(f"  - Removed: {file}")

    # Show both models
    print("\nExport complete! Optimized models saved:")
    for target_file in target_files.values():
        model_path = os.path.join(onnx_dir, target_file)
        if os.path.exists(model_path):
            size_mb = os.path.getsize(model_path) / (1024 * 1024)
            print(f"  - {model_path} ({size_mb:.1f} MB)")

    print("  - Both models are INT8 quantized for faster inference and smaller size")

    # Detect and recommend the best model for current CPU
    recommended_config = detect_cpu_features()
    print(f"  - Recommended for your CPU: {recommended_config}")

    return onnx_model


def test_exported_model(output_dir, test_sentences=None, use_cls_pooling=False):
    """Test the exported model to verify it works correctly"""
    if test_sentences is None:
        test_sentences = ["This is a test sentence.", "Another example sentence."]

    print("\nTesting exported model...")

    try:
        # Get the optimal model path based on CPU
        optimal_model_file = get_optimal_model_path(output_dir)
        print(f"Using optimal model: {optimal_model_file}")

        # Test the optimal quantized model
        if use_cls_pooling:
            # Create model with CLS pooling instead of mean pooling
            from sentence_transformers import models

            # Load tokenizer and transformer
            transformer = models.Transformer(output_dir)
            # Use CLS token pooling (first token) - get embedding dimension from config
            embedding_dim = 312  # Default for most BERT models, adjust as needed
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
    """
    Load the optimal ONNX model based on CPU capabilities

    Args:
        output_dir: Directory containing the exported models
        use_cls_pooling: If True, use CLS token pooling instead of mean pooling

    Returns:
        SentenceTransformer model with optimal configuration
    """
    optimal_model_file = get_optimal_model_path(output_dir)
    cpu_config = detect_cpu_features()

    print(f"Loading optimal model for CPU: {cpu_config}")
    print(f"Model file: {optimal_model_file}")

    if use_cls_pooling:
        from sentence_transformers import models

        # Load tokenizer and transformer with CLS pooling
        transformer = models.Transformer(output_dir)
        # Use CLS token pooling (first token) - get embedding dimension from config
        embedding_dim = 312  # Default for most BERT models, adjust as needed
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


# Example usage
if __name__ == "__main__":
    # Choose a model - popular options:
    # model_choices = [
    #     "sentence-transformers/all-MiniLM-L6-v2",  # Fast, 22MB
    #     "sentence-transformers/all-mpnet-base-v2",  # Higher quality
    #     "BAAI/bge-small-en-v1.5",  # Good balance
    # ]

    model_id = "./data/models/rubert-tiny2"  # Change index for different models
    output_directory = "./my_exported_model"

    # Export and quantize
    exported_model = export_and_quantize_sentence_transformer(
        model_id, output_directory
    )

    # Test the exported model with different pooling strategies
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
