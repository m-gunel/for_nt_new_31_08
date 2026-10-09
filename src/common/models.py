from typing import Literal

from pydantic import BaseModel, Field


class IndexingStatus(BaseModel):
    files_to_index: int = Field(
        ...,
        description=(
            "Всего элементов в очереди на индексацию: файлы + письма. "
            "Разбивка — в queued_files и queued_emails"
        ),
    )
    queued_files: int = Field(
        0,
        description="Кол-во файлов в очереди на индексацию",
    )
    queued_emails: int = Field(
        0,
        description="Кол-во писем в очереди на индексацию",
    )
    files_indexed: int = Field(
        0,
        description=(
            "Во время прохода (type=index) — сколько элементов его очереди "
            "обработано: total_files − files_to_index. В остальных состояниях — "
            "итог последнего непустого прохода; 0, если его ещё не было, и "
            "после сброса"
        ),
    )
    total_files: int = Field(
        0, description="Кол-во файлов, собранных на этапе preindex"
    )
    is_indexing: bool
    progress: float = Field(
        0,
        description=(
            "Во время прохода (type=index) — доля обработанного, от 0 до 1. В "
            "остальных состояниях — 1.0 после хотя бы одного непустого прохода, "
            "иначе 0"
        ),
    )
    # Значения намеренно не расширяются: клиенты (в том числе MCP-мост)
    # различают состояния по этому набору, а «деактивировано» отдаётся
    # отдельным полем active — оно ничего не ломает у тех, кто его не читает.
    type: Literal["preindex", "index", "update", "idle"] = "update"
    active: bool = Field(
        True,
        description=(
            "Включена ли индексация. False — демон работает, но не сканирует: "
            "поиск по уже собранному индексу продолжает отвечать. Отличает "
            "«деактивировано» от «простаиваю», которые оба дают is_indexing=false"
        ),
    )
