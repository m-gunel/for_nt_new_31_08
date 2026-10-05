# multilingual-e5-small в OpenVINO IR

Модель эмбеддингов индексатора. В поставку этот файл не попадает (фильтр `_MODEL_EXCLUDE`
в `main.spec`), он для разработчиков и проверки ИБ.

## Происхождение

- Источник: https://huggingface.co/intfloat/multilingual-e5-small, ревизия
  `614241f622f53c4eeff9890bdc4f31cfecc418b3`, веса скачаны в формате safetensors.
- Лицензия: MIT (Liang Wang, Nan Yang и соавторы), текст — в `debian/copyright`.
- Архитектура: BERT, 12 слоёв × 384, словарь XLM-R на 250 тыс. токенов, вход до 512 токенов.
  Вектор — 384 числа, mean-пулинг (`1_Pooling/config.json`), префиксы `query: ` и
  `passage: ` (`config_sentence_transformers.json`) — энкодер берёт их из этих файлов.

## Конвертация (05.10.2026, стенд `lib-local-rag-eval/work/models/convert.py`)

- OpenVINO 2026.3.1, openvino-tokenizers 2026.3.1.0, optimum-intel 2.2.0, nncf 3.4.0.
- FP32 IR через sentence-transformers (`backend="openvino"`), затем статическое INT8:
  `OVQuantizationConfig(preset=MIXED)` — как у прежней rubert-tiny2_002, у которой в IR
  асимметричные квантователи активаций. Калибровка — 300 фрагментов банковских документов
  стенда в нижнем регистре с префиксом `passage: `.
- Токенизатор: `openvino_tokenizers.convert_tokenizer`, сохранён без FP16 — Unigram-
  токенизатор XLM-R с весами в FP16 рантайм 2026.3 на ARM не исполняет.
- Проверка: OpenVINO FP32 совпадает с исходной torch-моделью (косинус 1,0000 на 64
  запросах и 64 фрагментах); INT8 против FP32 — средний косинус 0,997, минимальный 0,995.

## Веса частями

GitHub не принимает файлы больше 100 МБ, а `openvino_model_qint8_quantized.bin` — 119 МБ.
Он лежит тремя частями `openvino_model_qint8_quantized.bin.part00..02` по 45 МБ;
`read_model` в `src/utils/openvino_encoder.py` склеивает их в памяти. Собрать файл
целиком (например, для сверки):

```bash
cat openvino/openvino_model_qint8_quantized.bin.part* > openvino_model_qint8_quantized.bin
shasum -a 256 openvino_model_qint8_quantized.bin
# de1b89d9da41a6a4edf7a976d87fa0dc4ab54fafc754f8b9d08bbf257c16505b
```

## Качество на стенде

Против rubert-tiny2_002, выдача как в 1.0.9, контрольная часть запросов (650):
nDCG@10 гибрида +0,026 [+0,015…+0,037] (документы без оценки убраны из выдачи),
+0,033 [+0,014…+0,051] (только эталон наборов). Скорость эмбеддинга на Apple M1 —
6,1 фрагмента в секунду против 54 у rubert-tiny2_002 (в 9 раз медленнее).
