import os

import datasets
import polars as pl
from datasets import Dataset
from optimum.intel import OVQuantizationConfig
from sentence_transformers import (
    SentenceTransformer,
    export_static_quantized_openvino_model,
)

# Monkey patch the dataset loading function
original_load_dataset = datasets.load_dataset


def patched_load_dataset(path, *args, **kwargs):
    if os.path.exists(path) and os.path.isdir(path):
        # If it's a local directory, use load_from_disk
        return datasets.load_from_disk(path)
    else:
        # Otherwise use original function
        return original_load_dataset(path, *args, **kwargs)


# Apply the patch
datasets.load_dataset = patched_load_dataset


posts = (
    pl.read_parquet("data/posts.pq", columns=["Текст"])
    .rename({"Текст": "text"})
    .with_columns(pl.col("text").str.replace_all("_x000D_", "\n").str.strip_chars())
    .sample(256)
)

ds = Dataset.from_polars(posts)
ds.save_to_disk("./data/local_calibration_dataset")

# model export to openvino
model_path = "data/models/user2-small"
model = SentenceTransformer(model_path, backend="openvino")

quant_cfg = OVQuantizationConfig(
    preset="performance",  # агрессивный фьюзинг/скорость
    # per_channel=True,  # пер-канал веса
    # overflow_correction=True,  # стабильные скейлы
    # calibration_sampling_size=2048,  # >= 2k примеров
    # smooth_quant_alpha=None,  # без SQ для энкодера
)


export_static_quantized_openvino_model(
    model=model,
    quantization_config=quant_cfg,
    push_to_hub=False,
    create_pr=False,
    model_name_or_path=model_path,
    dataset_name="./data/local_calibration_dataset",
    column_name="text",  # Column containing text data
    dataset_split="train",  # Use the entire dataset
    dataset_config_name="default",
)
