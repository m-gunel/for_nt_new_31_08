from datetime import datetime
from pathlib import Path

import polars as pl
from rich.progress import Progress

from src.config import Config
from src.parsers import ChunkWithMeta, DocumentMetadata, get_parsers_registry
from src.thunderbird.read_contacts import CONTACTS_SCHEMA, read_contacts
from src.thunderbird.read_messages import (
    MESSAGE_SCHEMA,
    _convert_to_chunk_with_meta,
    collect_batches,
    read_messages,
)
from src.utils.batch_embeddings import embed_documents_into_batches
from src.utils.tools import (
    iter_chunks_from_parquet,
    merge_batches,
    wrap_with_converter,
    write_to_pq,
)

allowed_extensions = list(get_parsers_registry())


def filter_files(path: str) -> bool:
    *_, ext = Path(path).name.split(".")
    if ext in {"xlsx", "xls"}:
        return path.startswith("/Users/admin/projects/work/files-search/data/to_index")
    return ext.lower() in allowed_extensions


cfg = Config()
cfg.file_scan_cfg.filter_files = filter_files


with Progress() as prog:
    write_to_pq(
        collect_batches(
            read_messages(
                db_path="file:/Users/admin/projects/work/mail/global-messages-db.sqlite?mode=ro&immutable=1",
                min_date=datetime(2023, 12, 1),
                split_threads=False,
                progress=prog,
            ),
            batch_size=1024 * 10,
            schema=MESSAGE_SCHEMA,
        ),
        "data/messages_full.pq",
    )
    write_to_pq(
        collect_batches(
            read_contacts(
                db_path="file:/Users/admin/projects/work/mail/global-messages-db.sqlite?mode=ro&immutable=1",
                progress=prog,
            ),
            batch_size=1024 * 10,
            schema=CONTACTS_SCHEMA,
        ),
        "data/contacts.pq",
    )

    data = iter_chunks_from_parquet(
        "data/messages_full.pq",
        splitter=cfg.embed_splitter,
        batch_size=64,
        keep_columns=["c0body", "id", "date", "conversationID"],
        text_field_to_split="c0body",
        progress=prog,
        task_name="Embedding messages...",
    )

    embed = embed_documents_into_batches(
        wrap_with_converter(data, _convert_to_chunk_with_meta),
        embedder=cfg.embed_passages,
        batch_size=64,
    )
    write_to_pq(
        merge_batches(
            embed,
            cnt=10,
        ),
        filename="data/body_vecs_3.pq",
    )

    msgs = pl.scan_parquet("data/messages_full.pq")
    threads = (
        msgs.sort("date")
        .group_by("conversationID", maintain_order=True)
        .agg(
            pl.col.c1subject.first(),
            pl.col.date.min(),
            pl.len(),
            "from_",
            pl.col.to.list.sum(),
            pl.col.cc.list.sum(),
        )
        .with_columns(
            people=pl.concat_list("from_", "to", "cc").list.unique(maintain_order=True)
        )
        .drop("from_", "to", "cc")
        .collect()
    )
    write_to_pq(
        embed_documents_into_batches(
            (
                ChunkWithMeta(
                    text=x["c1subject"],
                    meta=DocumentMetadata(
                        th_message_id=x["conversationID"],
                        date_created=x["date"],
                    ),
                )
                for x in prog.track(
                    threads.iter_rows(named=True),
                    description="Embedding threads...",
                    total=len(threads),
                )
            ),
            embedder=cfg.embed_passages,
        ),
        filename="data/threads_vecs_2.pq",
    )
