"""Кросс-энкодер для переранжирования → OpenVINO IR + токенизатор пары (models/reranker).

Модели (лицензии разрешают коммерческое использование):
  bge     — BAAI/bge-reranker-v2-m3 (Apache-2.0): XLM-R large, 568 млн; модель продукта —
            лежит в models/reranker (INT8 весов, ~0,55 ГБ). На наборе hr точнее остальных:
            Hit@1 0,788 против 0,682 у mminilm, но ~12 с на поиск против 1,3 с;
  mminilm — cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (Apache-2.0): 118 млн параметров,
            из них 96 млн — словарь XLM-R; считает как модель на 21 млн;
  dity    — DiTy/cross-encoder-russian-msmarco (MIT): RuBERT-base, 178 млн; на hr — как
            mminilm при вчетверо большем времени;
  arga    — ARGA100/ru-reranker-modernbert-small (MIT): 35 млн, ModernBERT, русский.

Что делает:
1. Скачивает с Hugging Face веса, токенизатор и конфиги (ревизия зафиксирована).
2. Конвертирует torch → OpenVINO IR (входы input_ids, attention_mask, у моделей BERT ещё
   token_type_ids — реранкер продукта подаёт модели все выходы токенизатора → один логит) и
   сохраняет веса в выбранном виде:
     fp16  — веса FP16 без квантования: файл вдвое меньше FP32, на x86 счёт в FP32;
     int8w — INT8 только весов (nncf.compress_weights, без калибровки): файл в ~4 раза
             меньше FP32, качество на стенде то же.
   Статического INT8 (веса и активации) здесь нет: ему нужна калибровка на своих парах
   «запрос — фрагмент» (стенд: src/rerank_lab.py ov-convert), и на стенде оно сбивает порядок
   заметнее (отчёт стенда reports/rerank.md). На x86 с VNNI оно обычно быстрее — мерить на АРМ.
3. Токенизатор пары «запрос, текст» — openvino-tokenizers, усечение до --max-len.
4. Пишет каталог models/reranker/ (его ищет reranker_model_cfg.model_path по умолчанию) и
   сверяет оценки IR с исходной моделью на нескольких банковских парах.
5. Режет openvino_model.bin на части openvino_model.bin.part-NN меньше 100 МБ (лимит GitHub;
   --split-mb 0 — не резать): так модель хранится в репозитории, склеивает их сборка
   (main.spec) или реранкер при запуске из исходников.

Запуск из корня проекта:  make reranker-model    (RERANKER_ARGS="--model mminilm --mode fp16")
ModernBERT (arga) сохранён в формате transformers 5 — цель make берёт его на один запуск.
Если huggingface.co из сети недоступен: скачайте файлы (FILES) в папку и передайте её:
make reranker-model RERANKER_ARGS="--src /путь/к/папке".
"""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = {
    "mminilm": (
        "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        "1427fd652930e4ba29e8149678df786c240d8825",
    ),
    "arga": (
        "ARGA100/ru-reranker-modernbert-small",
        "8d4ea05d7c793bc812879ca18e7e310ac4cb228f",
    ),
    "bge": (
        "BAAI/bge-reranker-v2-m3",
        "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
    ),
    # RuBERT-base (DeepPavlov), дообучен на русском MS MARCO; MIT, 178 млн, один логит.
    "dity": (
        "DiTy/cross-encoder-russian-msmarco",
        "9029bab08103ad171724b510d312befa5b476293",
    ),
}
FILES = [
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "vocab.txt",
    "README.md",
]
SAMPLES = [
    (
        "порядок расчета полной стоимости вклада при досрочном расторжении",
        "Документ: Указание 5697-У\nПолная стоимость вклада рассчитывается с учётом процентов, "
        "выплачиваемых при досрочном расторжении договора банковского вклада.",
    ),
    (
        "порядок расчета полной стоимости вклада при досрочном расторжении",
        "Документ: Меню столовой\nБорщ, котлеты, компот.",
    ),
    (
        "сроки хранения кассовых документов",
        "Документ: Положение 630-П\nКассовые документы хранятся в течение пяти лет.",
    ),
    (
        "сроки хранения кассовых документов",
        "Документ: Отчёт МСФО 2023\nЧистая процентная маржа банка составила 4,1%.",
    ),
]


def _logits_module():
    import torch

    class Logits(torch.nn.Module):
        """Кросс-энкодер с одним выходом — логитом пары: так его и экспортируем."""

        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, input_ids, attention_mask, token_type_ids=None):
            # token_type_ids — у моделей BERT (запрос/документ в паре); у XLM-R их нет.
            kw = {} if token_type_ids is None else {"token_type_ids": token_type_ids}
            return self.m(
                input_ids=input_ids, attention_mask=attention_mask, **kw
            ).logits

    return Logits


def fetch(model: str, src: str | None) -> Path:
    if src:
        return Path(src)
    from huggingface_hub import snapshot_download

    repo, revision = MODELS[model]
    out = Path(tempfile.mkdtemp(prefix="reranker-"))
    snapshot_download(repo, revision=revision, local_dir=out, allow_patterns=FILES)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", choices=sorted(MODELS), default="bge")
    ap.add_argument("--mode", choices=("fp16", "int8w"), default="int8w")
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--src", help="папка с файлами модели вместо скачивания")
    ap.add_argument("--out", default=str(ROOT / "models" / "reranker"))
    ap.add_argument(
        "--split-mb",
        type=int,
        default=95,
        help="размер части файла весов в МиБ (0 — одним файлом)",
    )
    a = ap.parse_args()

    import nncf
    import numpy as np
    import openvino as ov
    import openvino_tokenizers as ot
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    Logits = _logits_module()

    src = fetch(a.model, a.src)
    tok = AutoTokenizer.from_pretrained(src)
    # ModernBERT — с eager-вниманием: в варианте sdpa маска скользящего окна (−3,4e38) в
    # FP16-вычислениях ARM-плагина становится −inf, строки паддинга закрыты целиком, и NaN
    # растекается на весь пакет.
    arch = json.loads((src / "config.json").read_text(encoding="utf-8")).get(
        "model_type"
    )
    kw = {"attn_implementation": "eager"} if arch == "modernbert" else {}
    model = Logits(
        AutoModelForSequenceClassification.from_pretrained(src, **kw).eval()
    ).eval()
    enc = tok(
        [q for q, _ in SAMPLES[:2]],
        [d * 40 for _, d in SAMPLES[:2]],
        padding=True,
        truncation=True,
        max_length=a.max_len,
        return_tensors="pt",
    )
    batch = torch.export.Dim("batch", min=1, max=256)
    seq = torch.export.Dim("seq", min=2, max=a.max_len)
    names = ["input_ids", "attention_mask"] + (
        ["token_type_ids"] if "token_type_ids" in enc else []
    )
    with torch.no_grad():
        # torch.export, а не трассировка TorchScript: на transformers 5 она падает.
        ep = torch.export.export(
            model,
            tuple(enc[n] for n in names),
            dynamic_shapes=tuple({0: batch, 1: seq} for _ in names),
        )
        ir = ov.convert_model(ep)
        ref_enc = tok(
            [q for q, _ in SAMPLES],
            [d for _, d in SAMPLES],
            padding=True,
            truncation=True,
            max_length=a.max_len,
            return_tensors="pt",
        )
        ref = model(*(ref_enc[n] for n in names)).reshape(-1).tolist()
    for i, n in enumerate(names):
        ir.inputs[i].get_tensor().set_names({n})
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if a.mode == "int8w":
        ir = nncf.compress_weights(ir, mode=nncf.CompressWeightsMode.INT8_ASYM)
    ov.save_model(ir, out / "openvino_model.xml", compress_to_fp16=a.mode == "fp16")
    # Без сжатия в FP16: у Unigram-токенизатора XLM-R (mminilm) таблица оценок словаря
    # должна остаться FP32, иначе ядро токенизатора падает на чтении.
    ov.save_model(
        ot.convert_tokenizer(tok, number_of_inputs=2, max_length=a.max_len),
        out / "openvino_tokenizer.xml",
        compress_to_fp16=False,
    )
    repo, revision = MODELS[a.model]
    (out / "reranker.json").write_text(
        json.dumps(
            {"repo": repo, "revision": revision, "mode": a.mode, "max_len": a.max_len},
            ensure_ascii=False,
            indent=1,
        )
    )
    for name in ("config.json", "README.md"):
        if (src / name).exists():
            shutil.copy(src / name, out / f"source_{name}")

    import openvino.properties.hint as hints

    core = ov.Core()
    # FP32 для токенизатора — как в src/utils/openvino_reranker.py.
    tk = core.compile_model(
        out / "openvino_tokenizer.xml", "CPU", {hints.inference_precision: ov.Type.f32}
    )
    cm = core.compile_model(out / "openvino_model.xml", "CPU")
    toks = tk([[q for q, _ in SAMPLES], [d for _, d in SAMPLES]])
    got = cm({k: toks[k] for k in names})[cm.outputs[0]].reshape(-1).tolist()
    diff = float(np.max(np.abs(np.array(got) - np.array(ref))))
    same_order = all(
        (got[i] > got[i + 1]) == (ref[i] > ref[i + 1])
        for i in range(0, len(SAMPLES), 2)
    )
    print(f"{repo} → {out} ({a.mode}, max_len {a.max_len})")
    print(f"исходная модель: {[round(x, 3) for x in ref]}")
    print(
        f"OpenVINO:        {[round(x, 3) for x in got]}  max|Δ| {diff:.3f}, порядок пар {'тот же' if same_order else 'ДРУГОЙ'}"
    )
    if not same_order:
        sys.exit("порядок оценок разошёлся с исходной моделью — IR не годится")
    if a.split_mb:
        split_weights(out / "openvino_model.bin", a.split_mb * 1024 * 1024)


def split_weights(path: Path, size: int) -> None:
    """openvino_model.bin → openvino_model.bin.part-00, -01, … (каждая не больше size)."""
    with open(path, "rb") as src:
        n = 0
        while chunk := src.read(size):
            (path.parent / f"{path.name}.part-{n:02d}").write_bytes(chunk)
            n += 1
    path.unlink()
    print(f"веса разбиты на {n} частей по {size // (1024 * 1024)} МиБ")


if __name__ == "__main__":
    main()
