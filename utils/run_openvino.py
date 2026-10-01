from src.utils import OpenVINOEmbedder

if __name__ == "__main__":
    model_dir = "models/rubert-tiny2_001"

    embedder = OpenVINOEmbedder(model_dir, device="CPU")

    texts = ["Пример текста для энкодинга", "Ещё один пример"]

    embeddings = embedder.encode(texts)
    print("Embeddings shape:", embeddings.shape)
# res = model(tokenizer(["интеллектуальный поиск"]))
# print(res)
