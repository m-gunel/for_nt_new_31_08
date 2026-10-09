"""OpenVINO-токенизатор и детокенизатор из HF-токенизатора модели.

Пишет <model>/openvino_tokenizer.xml (его грузит src/utils/openvino_encoder.py)
и <model>/openvino_detokenizer.xml.
"""

import argparse
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", required=True, help="каталог модели с HF-токенизатором"
    )
    args = parser.parse_args(argv)

    tokenizer_dir = Path(args.model)
    if not tokenizer_dir.is_dir():
        sys.exit(f"нет каталога модели: {tokenizer_dir}")

    # Группа torch нужна только дальше: ошибку в пути видно и без неё.
    from openvino import save_model
    from openvino_tokenizers import convert_tokenizer
    from transformers import AutoTokenizer

    hf_tokenizer = AutoTokenizer.from_pretrained(args.model)
    ov_tokenizer, ov_detokenizer = convert_tokenizer(
        hf_tokenizer, with_detokenizer=True
    )

    save_model(ov_tokenizer, tokenizer_dir / "openvino_tokenizer.xml")
    save_model(ov_detokenizer, tokenizer_dir / "openvino_detokenizer.xml")


if __name__ == "__main__":
    main()
