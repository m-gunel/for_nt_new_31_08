"""Переранжирование гибридной выдачи кросс-энкодером (src/server/rerank.py, /search)."""

import logging
from types import SimpleNamespace

import pytest

from src.config import RarankerModelConfig
from src.server.model import SearchRequest, SearchResponse
from src.server.rerank import fuse, passage, rerank_response
from src.server.router import search as search_endpoint
from tests.test_search_documents import _frame


def _response(n: int, pinned: tuple[str, ...] = ()) -> SearchResponse:
    """Ответ гибрида из n документов /d/0.txt … по убыванию оценки."""
    frame = _frame([(f"/d/{i}.txt", 1.0 - i / 100) for i in range(n)])
    return SearchResponse.from_dataframes([frame], pinned=pinned)


def _paths(items) -> list[str]:
    return [i.absolute_path for i in items]


class _Reranker:
    """Оценка пары = номер документа из текста фрагмента: последний — лучший."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    async def async_score(self, query, passages):
        self.calls.append((query, passages))
        if self.fail:
            raise RuntimeError("модель упала")
        return [float(p.rsplit(" ", 1)[-1]) for p in passages]


def test_pure_rerank_follows_the_model_and_keeps_pinned_first():
    items = _response(5, pinned=("/d/1.txt",)).results
    model = {
        "/d/0.txt": 0.0,
        "/d/1.txt": 9.0,
        "/d/2.txt": 1.0,
        "/d/3.txt": 3.0,
        "/d/4.txt": 2.0,
    }
    order = fuse(items, [model[i.absolute_path] for i in items], alpha=1.0)
    # /d/1 закреплён кодом из имени: первым он остаётся не из-за оценки модели.
    assert _paths(order) == ["/d/1.txt", "/d/3.txt", "/d/4.txt", "/d/2.txt", "/d/0.txt"]


def test_zero_weight_keeps_the_hybrid_order():
    items = _response(5).results
    assert _paths(fuse(items, [5.0, 4.0, 3.0, 9.0, 1.0], alpha=0.0)) == _paths(items)


def test_fusion_moves_a_document_only_as_far_as_both_ranks_allow():
    """При α=0,5 последний у гибрида, но первый у модели документ поднимается,
    а не прыгает на первое место мимо единогласного лидера."""
    items = _response(4).results
    order = fuse(items, [8.0, 1.0, 2.0, 9.0], alpha=0.5)
    assert _paths(order)[0] == "/d/0.txt"
    assert _paths(order).index("/d/3.txt") < 3


@pytest.mark.asyncio
async def test_rerank_cuts_to_limit_and_reports_the_model_score():
    response = _response(6)
    out = await rerank_response(response, "запрос", _Reranker(), alpha=1.0, limit=3)
    assert _paths(out.results) == ["/d/5.txt", "/d/4.txt", "/d/3.txt"]
    assert out.total == 3
    assert [i.rerank_score for i in out.results] == [5.0, 4.0, 3.0]


@pytest.mark.asyncio
async def test_reranker_failure_keeps_the_hybrid_answer(caplog):
    response = _response(6)
    with caplog.at_level(logging.ERROR):
        out = await rerank_response(
            response, "q", _Reranker(fail=True), alpha=1.0, limit=3
        )
    assert _paths(out.results) == ["/d/0.txt", "/d/1.txt", "/d/2.txt"]
    assert all(i.rerank_score is None for i in out.results)


def test_passage_is_the_best_fragment_with_the_document_line():
    item = _response(1).results[0]
    assert passage(item) == "Документ: 0\nфрагмент 0"


def test_passage_of_a_card_only_document_is_its_card():
    item = _response(1).results[0].model_copy(update={"chunks": []})
    assert passage(item).startswith("Документ: 0")


def _index(frames, reranker=None, k=20, alpha=1.0):
    calls = []

    async def search(query, top_k, **kw):
        calls.append(top_k)
        return frames

    cfg = SimpleNamespace(
        reranker=reranker,
        reranker_k=k,
        reranker_model_cfg=RarankerModelConfig(
            enabled=reranker is not None, alpha=alpha
        ),
    )
    return SimpleNamespace(search=search, cfg=cfg), calls


@pytest.mark.asyncio
async def test_search_reranks_a_deeper_pool_down_to_limit():
    frames = [_frame([(f"/d/{i}.txt", 1.0 - i / 100) for i in range(20)])]
    reranker = _Reranker()
    index, calls = _index(frames, reranker)
    out = await search_endpoint(SearchRequest(query="вклад", limit=10), index)
    assert calls == [20], "индексатор отдаёт reranker_k документов"
    assert len(reranker.calls[0][1]) == 20
    assert _paths(out.results) == [f"/d/{i}.txt" for i in range(19, 9, -1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["vector", "text"])
async def test_diagnostic_modes_are_not_reranked(query_type):
    frames = [_frame([(f"/d/{i}.txt", 1.0 - i / 100) for i in range(20)])]
    reranker = _Reranker()
    index, calls = _index(frames, reranker)
    out = await search_endpoint(
        SearchRequest(query="вклад", limit=10, query_type=query_type), index
    )
    assert calls == [10] and reranker.calls == []
    assert _paths(out.results) == [f"/d/{i}.txt" for i in range(10)]


@pytest.mark.asyncio
async def test_disabled_reranker_changes_nothing():
    frames = [_frame([(f"/d/{i}.txt", 1.0 - i / 100) for i in range(20)])]
    index, calls = _index(frames, reranker=None)
    out = await search_endpoint(SearchRequest(query="вклад", limit=10), index)
    assert calls == [10]
    assert out == SearchResponse.from_dataframes(frames, limit=10)


def test_reranker_is_off_by_default():
    assert RarankerModelConfig().enabled is False
