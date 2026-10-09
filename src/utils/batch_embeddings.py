from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from typing import TypeAlias

import numpy as np
import pyarrow as pa

from src.common import ChunkWithMeta, chunk_with_meta_schema

from .logging import get_logger

Embedder: TypeAlias = Callable[[list[str]], list[list[float]]]
AsyncEmbedder: TypeAlias = Callable[[list[str]], Awaitable[np.ndarray]]

lg = get_logger(__name__)

# Как embed_model_cfg.batch_size (индексатор передаёт его явно): с большим
# батчем индексация зависает (CPU 0 %).
DEFAULT_BATCH_SIZE = 8


def _process_batch(
    current_batch: list[ChunkWithMeta],
    embedder: Embedder,
    text_field: str = "text",
    schema: pa.Schema | None = None,
) -> pa.RecordBatch:
    if schema is None:
        schema = chunk_with_meta_schema
    _vecs = embedder([row[text_field] for row in current_batch])
    ndim = len(_vecs[0])
    vecs = pa.FixedSizeListArray.from_arrays(
        np.array(_vecs, dtype=np.float32).ravel(),  # type: ignore
        ndim,
    )
    return pa.RecordBatch.from_pylist(
        current_batch,
        schema=schema,
    ).append_column("vector", vecs)


async def _async_process_batch(
    current_batch: list[ChunkWithMeta],
    embedder: AsyncEmbedder,
    text_field: str = "text",
    schema: pa.Schema | None = None,
    callback: Callable[[list[ChunkWithMeta]], None] | None = None,
) -> pa.RecordBatch:
    if schema is None:
        schema = chunk_with_meta_schema
    lg.debug("encode batch of size %s", len(current_batch))
    _vecs = await embedder([row[text_field] for row in current_batch])
    ndim = len(_vecs[0])
    vecs = pa.FixedSizeListArray.from_arrays(
        np.array(_vecs, dtype=np.float32).ravel(),  # type: ignore
        ndim,
    )
    if callback:
        callback(current_batch)
    return pa.RecordBatch.from_pylist(
        current_batch,
        schema=schema,
    ).append_column("vector", vecs)


def embed_documents_into_batches(
    data: Iterable[ChunkWithMeta],
    embedder: Embedder,
    text_field: str = "text",
    batch_size: int = DEFAULT_BATCH_SIZE,
    schema: pa.Schema | None = None,
):
    current_batch: list[ChunkWithMeta] = []
    for row in data:
        if len(current_batch) >= batch_size:
            yield _process_batch(
                current_batch,
                embedder,
                text_field=text_field,
                schema=schema,
            )
            current_batch.clear()
        current_batch.append(row)
    if current_batch:
        yield _process_batch(
            current_batch,
            embedder,
            text_field=text_field,
            schema=schema,
        )


async def async_embed_documents_into_batches(
    data: AsyncIterable[ChunkWithMeta],
    embedder: AsyncEmbedder,
    text_field: str = "text",
    batch_size: int = DEFAULT_BATCH_SIZE,
    schema: pa.Schema | None = None,
    callback: Callable[[list[ChunkWithMeta]], None] | None = None,
):
    current_batch: list[ChunkWithMeta] = []
    try:
        async for row in data:
            if len(current_batch) >= batch_size:
                yield await _async_process_batch(
                    current_batch,
                    embedder,
                    text_field=text_field,
                    schema=schema,
                    callback=callback,
                )
                current_batch.clear()
            current_batch.append(row)
        if current_batch:
            yield await _async_process_batch(
                current_batch,
                embedder,
                text_field=text_field,
                schema=schema,
                callback=callback,
            )
    except Exception:
        lg.exception("in  processing")
        raise
