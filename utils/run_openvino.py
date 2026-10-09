# Запускать из корня репозитория: uv run python -m utils.run_openvino — по пути
# к файлу пакет src не найдётся.
from src.utils.openvino_encoder import OpenVINOEmbedder

if __name__ == "__main__":
    model_dir = "models/rubert-tiny2_002"

    embedder = OpenVINOEmbedder(model_dir, device="CPU")

    texts = ["Пример текста для энкодинга", "Ещё один пример"]

    embeddings = embedder.encode(texts)
    print("Embeddings shape:", embeddings.shape)
