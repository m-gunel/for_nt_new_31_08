"""Переранжирование гибридной выдачи кросс-энкодером (src/server/rerank.py, /search,
src/utils/openvino_reranker.py, модель в models/reranker)."""

import logging
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import openvino as ov
import pytest

from src.config import Config, RarankerModelConfig
from src.server.model import SearchRequest, SearchResponse
from src.server.rerank import fuse, passage, rerank_response
from src.server.router import search as search_endpoint
from src.utils.openvino_reranker import WEIGHT_PARTS_SUFFIX, OpenVINOReranker, read_ir
from tests.test_search_documents import _frame


def _response(n: int, pinned: tuple[str, ...] = ()) -> SearchResponse:
    """Ответ гибрида из n документов /d/0.txt … по убыванию оценки."""
    frame = _frame([(f"/d/{i}.txt", 1.0 - i / 100) for i in range(n)])
    return SearchResponse.from_dataframe(frame, pinned=pinned)


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


def test_keep_top_leaves_the_hybrid_first_place_alone():
    """keep=1: первое место гибрида остаётся, модель переставляет остальные."""
    items = _response(4).results
    order = fuse(items, [0.0, 1.0, 2.0, 3.0], alpha=1.0, keep=1)
    assert _paths(order) == ["/d/0.txt", "/d/3.txt", "/d/2.txt", "/d/1.txt"]


def test_keep_top_counts_after_pinned():
    """Закреплённый по коду из имени — первым, следом — первый документ гибрида."""
    items = _response(4, pinned=("/d/2.txt",)).results
    model = {"/d/0.txt": 0.0, "/d/1.txt": 1.0, "/d/2.txt": 9.0, "/d/3.txt": 5.0}
    order = fuse(items, [model[i.absolute_path] for i in items], alpha=1.0, keep=1)
    assert _paths(order) == ["/d/2.txt", "/d/0.txt", "/d/3.txt", "/d/1.txt"]


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


def _index(frame, reranker=None, k=20, alpha=1.0, keep=0):
    calls = []

    async def search(query, top_k, **kw):
        calls.append(top_k)
        return frame

    cfg = SimpleNamespace(
        reranker=reranker,
        reranker_k=k,
        reranker_model_cfg=RarankerModelConfig(
            enabled=reranker is not None, alpha=alpha, keep_top=keep
        ),
    )
    return SimpleNamespace(search=search, cfg=cfg), calls


@pytest.mark.asyncio
async def test_search_reranks_a_deeper_pool_down_to_limit():
    frame = _frame([(f"/d/{i}.txt", 1.0 - i / 100) for i in range(20)])
    reranker = _Reranker()
    index, calls = _index(frame, reranker)
    out = await search_endpoint(SearchRequest(query="вклад", limit=10), index)
    assert calls == [20], "индексатор отдаёт reranker_k документов"
    assert len(reranker.calls[0][1]) == 20
    assert _paths(out.results) == [f"/d/{i}.txt" for i in range(19, 9, -1)]


@pytest.mark.asyncio
async def test_search_keep_top_leaves_the_hybrid_first_place():
    frame = _frame([(f"/d/{i}.txt", 1.0 - i / 100) for i in range(20)])
    index, _ = _index(frame, _Reranker(), keep=1)
    out = await search_endpoint(SearchRequest(query="вклад", limit=10), index)
    assert _paths(out.results) == ["/d/0.txt"] + [
        f"/d/{i}.txt" for i in range(19, 10, -1)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["vector", "text"])
async def test_diagnostic_modes_are_not_reranked(query_type):
    frame = _frame([(f"/d/{i}.txt", 1.0 - i / 100) for i in range(20)])
    reranker = _Reranker()
    index, calls = _index(frame, reranker)
    out = await search_endpoint(
        SearchRequest(query="вклад", limit=10, query_type=query_type), index
    )
    assert calls == [10] and reranker.calls == []
    assert _paths(out.results) == [f"/d/{i}.txt" for i in range(10)]


@pytest.mark.asyncio
async def test_disabled_reranker_changes_nothing():
    frame = _frame([(f"/d/{i}.txt", 1.0 - i / 100) for i in range(20)])
    index, calls = _index(frame, reranker=None)
    out = await search_endpoint(SearchRequest(query="вклад", limit=10), index)
    assert calls == [10]
    assert out == SearchResponse.from_dataframe(frame, limit=10)


def test_reranker_defaults_are_the_measured_ones():
    """Умолчания — лучший вариант замера на наборе hr: bge, чистый реранк 20 документов."""
    rcfg = RarankerModelConfig()
    assert rcfg.enabled is True
    assert (rcfg.alpha, rcfg.keep_top) == (1.0, 0)
    assert Path(rcfg.model_path).name == "reranker"
    assert Config.model_fields["reranker_k"].default == 20


def _tiny_ir(tmp_path: Path) -> tuple[Path, np.ndarray]:
    """IR на 1000 весов: y = x + w — чтобы видеть, что веса прочитаны целиком и верно."""
    import openvino.opset13 as ops

    w = np.arange(1000, dtype=np.float32)
    x = ops.parameter([1000], ov.Type.f32, name="x")
    model = ov.Model([ops.add(x, ops.constant(w))], [x])
    xml = tmp_path / "openvino_model.xml"
    ov.save_model(model, xml, compress_to_fp16=False)
    return xml, w


def test_read_ir_compiles_whole_weights_by_path(tmp_path):
    xml, _ = _tiny_ir(tmp_path)
    assert read_ir(ov.Core(), xml) == xml


def test_read_ir_joins_weight_parts(tmp_path):
    """Части openvino_model.bin.part-NN (так модель лежит в репозитории) дают ту же модель."""
    xml, w = _tiny_ir(tmp_path)
    data = xml.with_suffix(".bin").read_bytes()
    for n, i in enumerate(range(0, len(data), 1500)):
        (tmp_path / f"openvino_model.bin{WEIGHT_PARTS_SUFFIX}{n:02d}").write_bytes(
            data[i : i + 1500]
        )
    xml.with_suffix(".bin").unlink()
    core = ov.Core()
    compiled = core.compile_model(read_ir(core, xml), "CPU")
    out = compiled({"x": np.zeros(1000, dtype=np.float32)})[compiled.outputs[0]]
    assert np.array_equal(out, w)


def test_read_ir_without_weights_says_what_is_missing(tmp_path):
    xml, _ = _tiny_ir(tmp_path)
    xml.with_suffix(".bin").unlink()
    with pytest.raises(FileNotFoundError, match="нет весов реранкера"):
        read_ir(ov.Core(), xml)


def test_bundled_model_ranks_the_answer_above_unrelated_text():
    """Модель из models/reranker (части весов в репозитории) загружается и различает пары."""
    rr = OpenVINOReranker(RarankerModelConfig().model_path)
    try:
        good, bad = rr.score(
            "сроки хранения кассовых документов",
            [
                "Кассовые документы хранятся в течение пяти лет.",
                "Борщ, котлеты, компот.",
            ],
        )
    finally:
        rr.shutdown()
    assert good > bad + 5
