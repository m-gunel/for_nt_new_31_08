# -*- mode: python ; coding: utf-8 -*-
#
# Правится руками: `make spec` перезапишет файл и потеряет фильтр модели ниже.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# Артефакты обучения в поставку не берём: pickle-файлы (их распаковка исполняет
# код), историю обучения и карточку модели с HuggingFace. Рантайму нужны только
# openvino/openvino_model_qint8_quantized.{xml,bin} и openvino_tokenizer.{xml,bin}
# (src/utils/openvino_encoder.py), но режем лишь то, что заведомо лишнее.
# models/tessdata (данные OCR) едет целиком.
_MODEL_EXCLUDE = {
    "rng_state.pth",
    "scheduler.pt",
    "training_args.bin",
    "trainer_state.json",
    "README.md",
}


# Части файла весов (models/reranker/openvino_model.bin.part-NN): в репозитории файл
# разбит из-за лимита GitHub в 100 МБ, в пакет идёт склеенный — OpenVINO читает его
# с диска без лишней копии в памяти (src/utils/openvino_reranker.py).
_PART_MARK = ".part-"
_JOINED_DIR = Path("build") / "joined"


def _model_datas(root="models"):
    """Пары (файл, каталог назначения) для всего каталога модели, кроме мусора."""
    out = []
    parts: dict[Path, list[Path]] = {}
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.name in _MODEL_EXCLUDE:
            continue
        whole, mark, _ = path.name.partition(_PART_MARK)
        if mark:
            parts.setdefault(path.parent / whole, []).append(path)
        else:
            out.append((str(path), str(path.parent)))
    for whole, pieces in parts.items():
        joined = _JOINED_DIR / whole
        joined.parent.mkdir(parents=True, exist_ok=True)
        with open(joined, "wb") as f:
            for piece in sorted(pieces):
                f.write(piece.read_bytes())
        out.append((str(joined), str(whole.parent)))
    return out


datas = _model_datas()

# Офлайн-статика Swagger. В бандле корень — sys._MEIPASS, и src/server/app.py ищет
# её в _MEIPASS/src/server/static. Берём пофайлово, без README.md каталога;
# файлов нет — приложение берёт Swagger с CDN.
def _swagger_datas(root="src/server/static"):
    out = []
    for name in ("swagger-ui-bundle.js", "swagger-ui.css", "favicon.png"):
        path = Path(root) / name
        if path.is_file():
            out.append((str(path), root))
    return out


datas += _swagger_datas()
binaries = []
# lance импортируется лениво, внутри lancedb (LanceTable.to_lance).
hiddenimports = ['lance']
tmp_ret = collect_all('openvino')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('openvino_tokenizers')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# Имя модуля, а не дистрибутива: по «langchain-core» collect_all пакет не
# находит и собирает одни метаданные.
tmp_ret = collect_all('langchain_core')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # lance (pylance) не исключать: LanceWrapper.optimize_indices дописывает
    # новые строки в индексы через table.to_lance().
    excludes=['openvino.torch', 'torch', 'datasets', 'pandas', 'sklearn', 'scipy', 'matplotlib', 'optimum', 'sentence_transformers', 'transformers', 'onnx', 'langchain', 'onnxruntime'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='main',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='main',
)