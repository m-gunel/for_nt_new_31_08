# pip install "optimum[onnxruntime]" transformers onnx onnxruntime
# Optional: pip install sentence-transformers

import argparse
from pathlib import Path

# Fallback: pure ORT quantization if desired
from onnxruntime.quantization import QuantType, quantize_dynamic
from optimum.onnxruntime import ORTModelForFeatureExtraction, ORTOptimizer, ORTQuantizer
from optimum.onnxruntime.configuration import AutoQuantizationConfig, OptimizationConfig
from transformers import AutoTokenizer


def export_optimize_quantize(
    model_id: str, out_dir: str, quant_with_optimum: bool = True
):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1) Export HF model to ONNX (feature-extraction = encoder hidden states)
    #    from_transformers=True triggers conversion to ONNX
    ort_model = ORTModelForFeatureExtraction.from_pretrained(
        model_id, from_transformers=True
    )
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    ort_model.save_pretrained(out)
    tokenizer.save_pretrained(out)
    base_onnx = out / "model.onnx"
    print(f"Saved ONNX and tokenizer to: {out}")

    # 2) Apply graph optimizations (Transformer fusions, etc.)
    optimizer = ORTOptimizer.from_pretrained(model_id, feature=ort_model.pipeline_task)
    opt_config = OptimizationConfig(optimization_level=99)  # enable maximum fusions
    optimized_onnx = out / "model-optimized.onnx"
    optimizer.export(
        onnx_model_path=base_onnx,
        onnx_optimized_model_output_path=optimized_onnx,
        optimization_config=opt_config,
    )
    print(f"Optimized ONNX saved: {optimized_onnx}")

    # 3) Quantize to INT8 dynamically
    quantized_onnx = out / "model-int8.onnx"

    if quant_with_optimum:
        # Optimum wrapper around ORT quantization with CPU presets (e.g., AVX512_VNNI)
        quantizer = ORTQuantizer.from_pretrained(
            model_id, feature=ort_model.pipeline_task
        )
        dqconfig = AutoQuantizationConfig.avx512_vnni(
            is_static=False,  # dynamic quantization
            per_channel=False,  # per-tensor is widely compatible and fast
        )
        quantizer.export(
            onnx_model_path=optimized_onnx,
            onnx_quantized_model_output_path=quantized_onnx,
            quantization_config=dqconfig,
        )
        print(f"Quantized (Optimum) ONNX saved: {quantized_onnx}")
    else:
        # Pure ORT minimal path: quantize the optimized model
        quantize_dynamic(
            model_input=str(optimized_onnx),
            model_output=str(quantized_onnx),
            weight_type=QuantType.QInt8,  # or QuantType.QUInt8
            optimize_model=False,  # graph already optimized above
            per_channel=False,
            op_types_to_quantize=["MatMul", "Gemm"],  # typical for fused Transformer
        )
        print(f"Quantized (ORT) ONNX saved: {quantized_onnx}")

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model_id",
        type=str,
        default="sentence-transformers/all-MiniLM-L6-v2",
        help="HF model id (encoder) to export and quantize.",
    )
    parser.add_argument(
        "--out_dir",
        type=str,
        default="./onnx",
        help="Output directory to store ONNX and tokenizer.",
    )
    parser.add_argument(
        "--no_optimum_quant",
        action="store_true",
        help="Use pure ONNX Runtime quantize_dynamic instead of Optimum wrapper.",
    )
    args = parser.parse_args()

    export_optimize_quantize(
        model_id=args.model_id,
        out_dir=args.out_dir,
        quant_with_optimum=(not args.no_optimum_quant),
    )
