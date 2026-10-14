"""Переранжирование гибридной выдачи кросс-энкодером OpenVINO (reranker_model_cfg).

Кросс-энкодер видит запрос и текст документа вместе и оценивает их пару — точнее, чем
сравнение двух независимых векторов, но дороже: модель считает каждую пару заново. Поэтому
он переставляет только первые `reranker_k` документов гибрида, а не ищет сам.

Модель — каталог с openvino_model.xml (IR кросс-энкодера: input_ids, attention_mask и, у
моделей BERT, token_type_ids → один логит) и openvino_tokenizer.xml (токенизатор пары
«запрос, текст» с усечением до max_length). Веса — openvino_model.bin или, как в
репозитории, его части openvino_model.bin.part-NN (каждая меньше 100 МБ — лимит GitHub):
части склеиваются в памяти, а сборка пакета (main.spec) кладёт уже склеенный файл.
Собирается `make reranker-model` (utils/reranker_model.py).
"""

import asyncio
from functools import partial
from pathlib import Path

import numpy as np
import openvino as ov
import openvino.properties as op
import openvino_tokenizers  # noqa: F401  # регистрирует расширение токенизатора

from .logging import get_logger
from .openvino_encoder import _cfg, _InferChannel

lg = get_logger(__name__)

# Части файла весов в репозитории: openvino_model.bin.part-00, -01, …
WEIGHT_PARTS_SUFFIX = ".part-"


def read_ir(core: ov.Core, xml: Path) -> "Path | ov.Model":
    """IR для compile_model: путь, если веса одним файлом, иначе модель из частей.

    Целый файл компилируется по пути (OpenVINO отображает его в память, лишней копии
    нет). Части склеиваются в памяти: на время компиляции это ещё размер весов сверху —
    так бывает только при запуске из исходников, пакет несёт склеенный файл.
    """
    weights = xml.with_suffix(".bin")
    if weights.exists():
        return xml
    parts = sorted(xml.parent.glob(weights.name + WEIGHT_PARTS_SUFFIX + "*"))
    if not parts:
        raise FileNotFoundError(
            f"нет весов реранкера: {weights} или {weights.name}{WEIGHT_PARTS_SUFFIX}*"
        )
    data = bytearray()
    for part in parts:
        data += part.read_bytes()
    # Tensor копирует буфер (shared_memory=False): склейку можно отпустить сразу, и
    # модель не держит ссылок на память, которую освободит сборщик мусора.
    weights_tensor = ov.Tensor(np.frombuffer(data, dtype=np.uint8))
    del data
    return core.read_model(xml.read_text(encoding="utf-8"), weights_tensor)


class OpenVINOReranker:
    def __init__(
        self,
        model_path: str,
        ir_file: str = "openvino_model.xml",
        tokenizer: str = "openvino_tokenizer.xml",
        device: str = "CPU",
        batch_size: int = 8,
    ):
        core = ov.Core()
        # Один stream: у реранкера один канал, и пары одного запроса считаются одним
        # infer-request'ом. С двумя streams эмбеддера (_cfg) каждому достаётся половина
        # потоков, и запрос считается вдвое дольше (стенд: 3,1 с против 1,7 с на 20 пар).
        model_cfg = {**_cfg, op.num_streams(): 1}
        self.model = core.compile_model(
            read_ir(core, Path(model_path) / ir_file), device, model_cfg
        )
        # Токенизатор — в FP32: ядро Unigram-токенизатора XLM-R не принимает
        # FP16-тензоры, которые CPU-плагин на ARM подставляет по умолчанию.
        tok_cfg = {**_cfg, op.hint.inference_precision(): ov.Type.f32}
        self.tokenizer = core.compile_model(
            Path(model_path) / tokenizer, device, tok_cfg
        )
        # Свой канал: оценка пар не встаёт в очередь за батчами индексации и не
        # занимает канал поиска эмбеддера.
        self._channel = _InferChannel("rerank", self.model, self.tokenizer)
        self.batch_size = max(1, batch_size)

    def score(self, query: str, passages: list[str]) -> list[float]:
        """Логиты пар «запрос — текст», в порядке passages. Синхронно, в потоке канала.

        Пары идут пакетами, отсортированными по длине текста: пакет паддится до
        самой длинной пары, и короткие тексты рядом с длинными считались бы зря.
        """
        order = sorted(range(len(passages)), key=lambda i: len(passages[i]))
        scores = [0.0] * len(passages)
        for start in range(0, len(order), self.batch_size):
            idx = order[start : start + self.batch_size]
            # Два входа токенизатора: запросы и тексты пар, по одному на пару.
            logits = self._channel.infer(
                [[query] * len(idx), [passages[i] for i in idx]]
            )
            for i, value in zip(idx, logits.reshape(-1).tolist()):
                scores[i] = float(value)
        return scores

    async def async_score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._channel.executor, partial(self.score, query, passages)
        )

    def shutdown(self):
        self._channel.shutdown()
