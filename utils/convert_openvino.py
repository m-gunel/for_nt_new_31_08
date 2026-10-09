"""Статическое int8-квантование модели sentence-transformers в OpenVINO.

Пишет <model>/openvino/openvino_model_qint8_quantized.xml — его грузит
src/utils/openvino_encoder.py. Исходного чекпойнта с FP32-весами и набора
калибровки в репозитории нет, пути к ним задаются явно (цель make openvino).
"""

import argparse
import os
import sys
from pathlib import Path

import polars as pl

CALIBRATION_DIR = "./data/local_calibration_dataset"


def has_fp32_weights(model_dir):
    # В каталоге поставки (models/...) только квантованная модель: квантовать
    # там нечего, а экспорт упал бы глубоко в optimum с невнятной ошибкой.
    path = Path(model_dir)
    return path.is_dir() and (
        any(path.glob("*.safetensors")) or any(path.glob("pytorch_model*.bin"))
    )


def load_calibration(path, column, size, seed):
    # С seed: на другой выборке квантованная модель даёт другие векторы, и
    # повторная конвертация разошлась бы с уже построенным индексом.
    texts = (
        pl.read_parquet(path, columns=[column])
        .rename({column: "text"})
        .with_columns(pl.col("text").str.replace_all("_x000D_", "\n").str.strip_chars())
    )
    return texts.sample(size, seed=seed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        required=True,
        help="каталог исходного чекпойнта с FP32-весами; результат пишется туда же",
    )
    parser.add_argument(
        "--calib", required=True, help="parquet с текстами для калибровки"
    )
    parser.add_argument("--column", default="Текст", help="колонка с текстом")
    parser.add_argument("--size", type=int, default=256, help="число текстов")
    parser.add_argument("--seed", type=int, default=0, help="seed выборки")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.calib):
        sys.exit(f"нет набора калибровки: {args.calib}")
    if not has_fp32_weights(args.model):
        sys.exit(
            f"в {args.model} нет FP32-весов (*.safetensors, pytorch_model*.bin): "
            "нужен исходный чекпойнт, а не каталог поставки"
        )

    # Группа torch нужна только дальше: ошибку в путях видно и без неё.
    import datasets
    from datasets import Dataset
    from optimum.intel import OVQuantizationConfig
    from sentence_transformers import (
        SentenceTransformer,
        export_static_quantized_openvino_model,
    )

    # export_static_quantized_openvino_model грузит калибровку через load_dataset,
    # а тот не читает каталог из save_to_disk — подменяем на load_from_disk.
    original_load_dataset = datasets.load_dataset

    def patched_load_dataset(path, *args, **kwargs):
        if os.path.exists(path) and os.path.isdir(path):
            return datasets.load_from_disk(path)
        else:
            return original_load_dataset(path, *args, **kwargs)

    datasets.load_dataset = patched_load_dataset

    posts = load_calibration(args.calib, args.column, args.size, args.seed)
    ds = Dataset.from_polars(posts)
    ds.save_to_disk(CALIBRATION_DIR)

    model = SentenceTransformer(args.model, backend="openvino")

    quant_cfg = OVQuantizationConfig(
        preset="performance",  # агрессивный фьюзинг/скорость
    )

    export_static_quantized_openvino_model(
        model=model,
        quantization_config=quant_cfg,
        push_to_hub=False,
        create_pr=False,
        model_name_or_path=args.model,
        dataset_name=CALIBRATION_DIR,
        column_name="text",
        dataset_split="train",
        dataset_config_name="default",
    )


if __name__ == "__main__":
    main()
