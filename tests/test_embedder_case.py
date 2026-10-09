"""Регистр не влияет на вектор: «ДОК 1» и «док 1» — один и тот же запрос.

rubert-tiny2 различает регистр, и запрос ЗАГЛАВНЫМИ находил бы нужный документ
заметно хуже. Поэтому вход модели — в нижнем регистре и у документов, и у
запросов. Хранимый текст, BM25 и подсчёт токенов не меняются.
"""

import numpy as np

from src.utils.openvino_encoder import OpenVINOEmbedder


def test_model_input_is_lowercased():
    embedder = OpenVINOEmbedder.__new__(OpenVINOEmbedder)
    embedder._path = "models/rubert-tiny2_002"  # без префиксов задач
    assert embedder._apply_prefix(["ДОК 1", "Отчёт ЦБ"], "query") == [
        "док 1",
        "отчёт цб",
    ]
    embedder.__dict__["prefix"] = {
        "query": "search_query: ",
        "document": "search_document: ",
    }
    assert embedder._apply_prefix(["ДОК 1"], "document") == ["search_document: док 1"]


def test_upper_and_lower_case_give_the_same_vector(config):
    vectors = config.ov_model.encode(
        ["ДОК 1 ОТЧЁТ БАНКА", "док 1 отчёт банка", "Док 1 Отчёт Банка"]
    )
    assert np.allclose(vectors[0], vectors[1]) and np.allclose(vectors[1], vectors[2])


def test_token_count_is_not_affected(config):
    # Длину чанка сплиттер меряет по исходному тексту — граница чанков не сдвигается.
    upper = "ПОЛОЖЕНИЕ БАНКА РОССИИ О ПОРЯДКЕ РАСЧЁТА РЕЗЕРВОВ"
    assert config.ov_model.count_tokens(upper) != config.ov_model.count_tokens(
        upper.lower()
    )
