"""Батчер эмбеддингов: размер батча по умолчанию — безопасный, как в конфиге.

Большой батч вешает индексацию (CPU 0 %, грабля AGENTS.md); вызов без
batch_size не должен молча получать батч крупнее embed_model_cfg.batch_size.
"""

import asyncio
import inspect

import numpy as np
import pyarrow as pa
import pytest

from src.config import EmbeddingModelConfig
from src.utils.batch_embeddings import (
    async_embed_documents_into_batches,
    embed_documents_into_batches,
)

SAFE = EmbeddingModelConfig().batch_size
TEXT_ONLY = pa.schema([pa.field("text", pa.string())])


@pytest.mark.parametrize(
    "func", [async_embed_documents_into_batches, embed_documents_into_batches]
)
def test_default_batch_size_matches_config(func):
    assert inspect.signature(func).parameters["batch_size"].default == SAFE


def test_async_batcher_without_batch_size_keeps_batches_small():
    sizes: list[int] = []

    async def embedder(texts: list[str]) -> np.ndarray:
        sizes.append(len(texts))
        return np.zeros((len(texts), 4), dtype=np.float32)

    async def rows():
        for i in range(2 * SAFE + 3):
            yield {"text": f"чанк {i}"}

    async def run():
        return [
            b
            async for b in async_embed_documents_into_batches(
                rows(), embedder, schema=TEXT_ONLY
            )
        ]

    batches = asyncio.run(run())
    assert sizes == [SAFE, SAFE, 3]
    assert sum(len(b) for b in batches) == 2 * SAFE + 3
