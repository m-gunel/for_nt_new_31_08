"""jina-embeddings-v5-text-nano-retrieval → OpenVINO IR со сжатыми весами — для эксперимента.

ЛИЦЕНЗИЯ МОДЕЛИ — CC BY-NC 4.0: коммерческое использование запрещено. Только для сравнения
качества; в deb-пакет каталог не попадает (main.spec отсекает models/jina-*), в git — тоже
(.gitignore). Для продукта нужна коммерческая лицензия Jina.

Что делает:
1. Скачивает с Hugging Face официальный ONNX варианта под поиск (адаптер поиска вшит в веса)
   и токенизатор, ревизия зафиксирована. Кода автора модели не запускает.
2. Конвертирует ONNX в OpenVINO IR и сжимает веса nncf (без калибровочных данных):
     int4   — INT4 асимметрично, группы по 128, все слои вместе со словарём (по умолчанию, ~110 МБ);
     int4e8 — INT4 для слоёв трансформера, словарь INT8 (~155 МБ);
     int8   — INT8 асимметрично, все слои (~205 МБ).
   Для сравнения: FP32 — 810 МБ, multilingual-e5-small в INT8 — 119 МБ, rubert-tiny2_002 — 30 МБ.
   Сжатие весов уменьшает файл и память, но не ускоряет счёт: вычислений на токен у модели
   примерно в 49 раз больше, чем у rubert-tiny2_002.
3. Пишет каталог models/jina-v5-nano-retrieval-<режим>/ в раскладке проекта: IR, tokenizer.json,
   1_Pooling (пулинг по последнему токену, вектор 768), префиксы «Query: » и «Document: ».
   Токенизатора OpenVINO нет намеренно: openvino-tokenizers 2026.3 не поддерживает ignore_merges
   у BPE Llama-3, часть русских слов режется иначе. Энкодер в таком каталоге берёт tokenizer.json.
4. Сверяет с оригиналом: энкодер проекта на сжатой модели против готового вектора фразы
   официального ONNX (onnxruntime) на нескольких банковских фразах — печатает косинус.

Запуск из корня проекта:  make jina-model            (JINA_ARGS="--mode int8" — другой режим)
Если huggingface.co из сети недоступен: скачайте файлы репозитория (см. FILES) в одну папку
с той же структурой и передайте её: make jina-model JINA_ARGS="--src /путь/к/папке".
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "jinaai/jina-embeddings-v5-text-nano-retrieval"
REVISION = "ac5d898c8d382b17167c33e5c8af644a3519b47d"  # 15.04.2026
FILES = [
    "onnx/model.onnx",
    "onnx/model.onnx_data",
    "tokenizer.json",
    "tokenizer_config.json",
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "1_Pooling/config.json",
]
COPY = FILES[2:]  # всё, кроме ONNX
SAMPLES = {
    "query": [
        "как открыть вклад для физического лица",
        "сроки хранения документов по кассовым операциям",
        "порядок расчёта полной стоимости кредита",
    ],
    "document": [
        "Вклад открывается на основании договора банковского вклада при предъявлении паспорта.",
        "Кассовые документы хранятся в течение пяти лет, если иное не установлено законом.",
        "Полная стоимость кредита рассчитывается в процентах годовых с точностью до третьего знака.",
        "Отчётность по форме 0409264 представляется ежемесячно не позднее 15-го рабочего дня.",
    ],
}


def log(*a):
    print(*a, flush=True)


def fetch(dst: Path) -> Path:
    from huggingface_hub import snapshot_download

    log(f"скачиваю {REPO}@{REVISION[:7]} в {dst} (~830 МБ)")
    snapshot_download(REPO, revision=REVISION, local_dir=dst, allow_patterns=FILES)
    return dst


def compress(model, mode: str):
    import nncf

    params = {
        "int4": dict(
            mode=nncf.CompressWeightsMode.INT4_ASYM,
            group_size=128,
            ratio=1.0,
            all_layers=True,
        ),
        "int4e8": dict(
            mode=nncf.CompressWeightsMode.INT4_ASYM, group_size=128, ratio=1.0
        ),
        "int8": dict(mode=nncf.CompressWeightsMode.INT8_ASYM),
    }[mode]
    return nncf.compress_weights(model, **params)


def check(src: Path, dst: Path, ir: str) -> tuple[float, float]:
    """Косинус векторов энкодера проекта на сжатой модели с вектором фразы официального ONNX."""
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    sys.path.insert(0, str(ROOT))
    from src.utils.openvino_encoder import OpenVINOEmbedder

    emb = OpenVINOEmbedder(str(dst), openvino=ir)
    tok = Tokenizer.from_file(str(src / "tokenizer.json"))
    sess = ort.InferenceSession(
        str(src / "onnx/model.onnx"), providers=["CPUExecutionProvider"]
    )
    cos = []
    for task, texts in SAMPLES.items():
        got = emb.encode(texts, task=task)
        for text, vec in zip(emb._apply_prefix(texts, task), got):
            ids = np.array([tok.encode(text).ids], dtype=np.int64)
            ref = sess.run(
                ["sentence_embedding"],
                {"input_ids": ids, "attention_mask": np.ones_like(ids)},
            )[0][0]
            cos.append(float(vec @ (ref / np.linalg.norm(ref))))
    emb.shutdown()
    return float(np.mean(cos)), float(np.min(cos))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", choices=("int4", "int4e8", "int8"), default="int4")
    ap.add_argument(
        "--src",
        type=Path,
        help="папка с уже скачанными файлами репозитория (см. FILES)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        help="каталог модели (по умолчанию models/jina-v5-nano-retrieval-<режим>)",
    )
    a = ap.parse_args()

    import openvino as ov

    src = a.src or fetch(ROOT / "data" / "jina-v5-nano-retrieval")
    missing = [f for f in FILES if not (src / f).exists()]
    if missing:
        sys.exit(f"в {src} нет файлов: {', '.join(missing)}")
    dst = a.out or ROOT / "models" / f"jina-v5-nano-retrieval-{a.mode}"
    ir = f"openvino/openvino_model_{a.mode}.xml"
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "openvino").mkdir(parents=True)
    for f in COPY:
        (dst / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src / f, dst / f)

    log("конвертирую ONNX в OpenVINO IR")
    model = ov.convert_model(str(src / "onnx/model.onnx"))
    log(f"сжимаю веса: {a.mode}")
    model = compress(model, a.mode)
    ov.save_model(model, dst / ir, compress_to_fp16=False)
    size = (dst / ir).with_suffix(".bin").stat().st_size / 2**20

    (dst / "README.md").write_text(
        f"# jina-embeddings-v5-text-nano-retrieval, веса {a.mode}\n\n"
        "ЛИЦЕНЗИЯ CC BY-NC 4.0 — коммерческое использование запрещено, только для сравнения.\n\n"
        f"Источник: https://huggingface.co/{REPO}, ревизия {REVISION}, официальный ONNX.\n"
        f"Сделано utils/jina_model.py --mode {a.mode}: OpenVINO IR, nncf.compress_weights. "
        f"Файл весов {size:.0f} МБ. Токенизатор — tokenizer.json (пакет tokenizers), "
        "OpenVINO-токенизатора нет: openvino-tokenizers не поддерживает ignore_merges.\n",
        encoding="utf-8",
    )
    log("сверяю с официальным ONNX")
    mean, low = check(src, dst, ir)
    rel = dst.relative_to(ROOT) if dst.is_relative_to(ROOT) else dst
    log(
        f"\nготово: {rel}\n"
        f"  файл весов: {size:.0f} МБ (FP32 — 810 МБ)\n"
        f"  косинус с оригиналом: средний {mean:.4f}, минимальный {low:.4f}"
        f"{'  — заметное расхождение, попробуйте --mode int4e8 или int8' if low < 0.98 else ''}\n"
        f"  в конфиге демона:\n"
        f"    embed_model_cfg:\n      model_path: {rel}\n      ir_file: {ir}\n"
        "  запуск: make run-jina (конфиг cfg/jina.yml)"
    )


if __name__ == "__main__":
    main()
