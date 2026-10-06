"""Задержка переранжирования на этой машине — запускать на АРМ SberOS, где работает индексатор.

Замер на MacBook для АРМ не показателен: на Apple M1 у OpenVINO плагин ARM и FP16-вычисления, на
x86 — FP32, а статическое INT8 там ускоряется инструкциями VNNI.

Класс продукта (src/utils/openvino_reranker.py) с настройками продукта (LATENCY, половина ядер,
точность — решает плагин) оценивает пары «запрос — фрагмент» реальной длины
(фрагмент ~300 токенов, как в индексе) столько раз, сколько запросов. Печатает медиану и p95
на запрос для глубины переранжирования --depth (20 — значение reranker_k по умолчанию).

    uv run python utils/reranker_bench.py                       # models/reranker, 20 пар, 50 запросов
    uv run python utils/reranker_bench.py --model-path models/reranker-fp16 --depth 10

Модель собирается make reranker-model. Сравнивать варианты (fp16, int8w) — на одной машине.
"""

import argparse
import asyncio
import os
import platform
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUERY = (
    "Просим пояснить порядок расчета полной стоимости вклада в случае сохранения процентного "
    "дохода при досрочном расторжении договора банковского вклада по требованию вкладчика"
)
# Фрагмент ~300 токенов: регуляторный текст той же длины, что чанки индекса.
FRAGMENT = (
    "Документ: Указание Банка России 5697-У\n"
    + "Полная стоимость вклада рассчитывается кредитной организацией в процентах годовых с "
    "точностью до третьего знака после запятой с учетом всех платежей, предусмотренных "
    "договором банковского вклада, включая проценты, капитализацию и выплаты при досрочном "
    "расторжении договора по требованию вкладчика. " * 6
)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model-path", default=str(ROOT / "models" / "reranker"))
    ap.add_argument("--depth", type=int, default=20)
    ap.add_argument("--queries", type=int, default=50)
    ap.add_argument("--batch", type=int, default=8)
    a = ap.parse_args()

    from src.utils.openvino_reranker import OpenVINOReranker

    rr = OpenVINOReranker(a.model_path, batch_size=a.batch)
    # Разная длина фрагментов, как в выдаче: от половины до полного.
    passages = [
        FRAGMENT[: len(FRAGMENT) // 2 + i * len(FRAGMENT) // (2 * a.depth)]
        for i in range(a.depth)
    ]

    async def run() -> list[float]:
        for _ in range(3):  # прогрев
            await rr.async_score(QUERY, passages)
        times = []
        for _ in range(a.queries):
            t = time.perf_counter()
            await rr.async_score(QUERY, passages)
            times.append((time.perf_counter() - t) * 1000)
        return sorted(times)

    times = asyncio.run(run())
    print(
        f"{platform.machine()} {platform.processor() or ''} ядер {os.cpu_count()}; модель {a.model_path}"
    )
    print(
        f"переранжирование {a.depth} пар (пакеты по {a.batch}): медиана {statistics.median(times):.0f} мс, "
        f"p95 {times[int(0.95 * len(times))]:.0f} мс на запрос ({a.queries} запросов)"
    )
    rr.shutdown()


if __name__ == "__main__":
    main()
