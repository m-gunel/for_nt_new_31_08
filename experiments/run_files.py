from pathlib import Path

from rich.progress import Progress

from src.config import Config, set_config
from src.parsers import get_parsers_registry, parse_files
from src.utils.batch_embeddings import embed_documents_into_batches
from src.utils.tools import write_to_pq

allowed_extentions = list(get_parsers_registry())


def filter_files(path: str | Path) -> bool:
    """Filter files based on allowed extensions."""
    *_, ext = Path(path).name.split(".")
    if ext in {"xlsx", "xls"}:
        return path.startswith("/Users/admin/projects/work/files-search/data/to_index")
    return ext.lower() in allowed_extentions


cfg = set_config(Config())
cfg.file_scan_cfg.filter_files = filter_files

if __name__ == "__main__":
    with Progress() as progress:
        task = progress.add_task(description="Processing")

        write_to_pq(
            embed_documents_into_batches(
                parse_files(cfg.file_scanner.scan(progress=progress)),
                embedder=cfg.embed_passages,
                text_field="text",
                batch_size=64,
            ),
            filename="data/test_vecs.pq",
        )
