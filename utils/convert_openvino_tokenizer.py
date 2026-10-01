from pathlib import Path

from openvino import save_model
from openvino_tokenizers import convert_tokenizer
from transformers import AutoTokenizer

model_id = "data/models/user2-small"
hf_tokenizer = AutoTokenizer.from_pretrained(model_id)
ov_tokenizer, ov_detokenizer = convert_tokenizer(hf_tokenizer, with_detokenizer=True)

tokenizer_dir = Path("data/models/user2-small/")
save_model(ov_tokenizer, tokenizer_dir / "openvino_tokenizer.xml")
save_model(ov_detokenizer, tokenizer_dir / "openvino_detokenizer.xml")
