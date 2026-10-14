"""Переранжирование ответа гибридного поиска кросс-энкодером (эксперимент, выключено по умолчанию).

Порядок: документы, найденные по коду из имени файла (match name_code), остаются первыми и в
своём порядке — правило кода из имени точнее любой модели. Остальные переставляются
взвешенным RRF двух рангов: гибрида и кросс-энкодера,
    (1 − α) / (K + ранг гибрида) + α / (K + ранг реранкера),
α — вес реранкера (1 — чистый реранк). Слияние рангов, а не только реранкер: на документах с
номерами актов и пунктов совпадение слов BM25 бывает точнее модели.

keep — сколько первых документов гибрида (после закреплённых) не переставлять. По умолчанию 1:
среди однотипных документов (отчёты МСФО разных банков и лет) модель путает банк и период, и с
переставленным первым местом Hit@1 там падал на 0,075; с keep=1 прирост nDCG@10 тот же (стенд,
1982 запроса: +0,011), а первый результат — прежний.

Пара для модели — запрос и лучший фрагмент документа со строкой «Документ: <имя>» сверху, как
строка индекса; у документа без фрагментов (нашёлся одной карточкой) — текст карточки.
"""

from collections.abc import Sequence

from src.common.doc_meta import doc_prefix, document_card_text
from src.utils.logging import get_logger

lg = get_logger(__name__)

RRF_K = 60


def passage(item) -> str:
    if item.chunks:
        return doc_prefix(item.title) + item.chunks[0].content
    meta = item.document.model_dump() if item.document is not None else {}
    return document_card_text({**meta, "original_name": item.title})


def fuse(items: Sequence, scores: Sequence[float], alpha: float, keep: int = 0) -> list:
    """Новый порядок: pinned и первые keep документов гибрида — на своих местах, остальные —
    по взвешенному RRF рангов."""
    pinned = [i for i, item in enumerate(items) if "name_code" in item.match]
    rest = [i for i, item in enumerate(items) if "name_code" not in item.match]
    pinned, rest = pinned + rest[:keep], rest[keep:]
    by_model = sorted(rest, key=lambda i: scores[i], reverse=True)
    model_rank = {i: r for r, i in enumerate(by_model, 1)}
    hybrid_rank = {i: r for r, i in enumerate(rest, 1)}
    fused = sorted(
        rest,
        key=lambda i: (1 - alpha) / (RRF_K + hybrid_rank[i])
        + alpha / (RRF_K + model_rank[i]),
        reverse=True,
    )
    return [items[i] for i in pinned + fused]


async def rerank_response(
    response, query: str, reranker, alpha: float, limit: int, keep: int = 0
):
    """Переставить документы ответа и обрезать до limit. Сбой модели — прежний порядок."""
    items = list(response.results)
    try:
        scores = await reranker.async_score(query, [passage(item) for item in items])
    except Exception as exc:
        # Реранкер — надбавка к выдаче: без него ответ тот же, что раньше.
        lg.error("переранжирование не удалось, выдача без него: %s", exc, exc_info=True)
        ordered = items
    else:
        for item, value in zip(items, scores):
            item.rerank_score = value
        ordered = fuse(items, scores, alpha, keep)
    # limit — документов на источник, как у SearchResponse.from_dataframes: файлы
    # и почта обрезаются каждый до своего limit.
    taken: dict[bool, int] = {}
    results = []
    for item in ordered:
        source = item.type == "file"
        if taken.get(source, 0) < limit:
            taken[source] = taken.get(source, 0) + 1
            results.append(item)
    return response.model_copy(update={"results": results, "total": len(results)})
