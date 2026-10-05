# -*- mode: python ; coding: utf-8 -*-
#
# ВНИМАНИЕ: файл поддерживается руками, `make spec` его ПЕРЕЗАПИШЕТ и потеряет
# фильтр каталога модели ниже. Поэтому `make build` больше не зависит от цели
# `spec`: она осталась для разового пересоздания болванки.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# Каталог модели приехал из обучения и содержит его артефакты. Заказчику они
# не нужны и вредны:
#   * rng_state.pth, scheduler.pt, training_args.bin — pickle. Их распаковка
#     это исполнение кода, то есть лишняя поверхность атаки в поставке;
#   * trainer_state.json — история обучения: метрики, шаги, гиперпараметры;
#   * README.md — карточка модели с HuggingFace, к продукту отношения не имеет.
#
# В рантайме модели нужны (src/utils/openvino_encoder.py, src/config.py):
# openvino/openvino_model_qint8_quantized.xml с весами — целым .bin или частями
# .bin.partNN, openvino_tokenizer.{xml,bin}, 1_Pooling/config.json (пулинг и
# размерность) и config_sentence_transformers.json (префиксы задач). Остальное
# оставлено намеренно: отсекаем только то, в чём уверены, а не всё, что кажется
# лишним. Каталог models/tessdata
# (языковые данные OCR и их LICENSE) едет целиком тем же фильтром.
_MODEL_EXCLUDE = {
    "rng_state.pth",
    "scheduler.pt",
    "training_args.bin",
    "trainer_state.json",
    "README.md",
}


def _model_datas(root="models"):
    """Пары (файл, каталог назначения) для всего каталога модели, кроме мусора."""
    out = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.name not in _MODEL_EXCLUDE:
            out.append((str(path), str(path.parent)))
    return out


datas = _model_datas()

# Офлайн-статика Swagger. Кладётся по тому же относительному пути, по которому
# её ищет src/server/app.py: в бандле корнем служит sys._MEIPASS, и файлы
# должны оказаться в _MEIPASS/src/server/static.
#
# Забирается ПОФАЙЛОВО и только то, что реально нужно: каталог в репозитории
# содержит ещё README.md, и класть его в поставку незачем. Если файлов нет —
# список пустой, сборка не падает, а приложение работает через CDN.
def _swagger_datas(root="src/server/static"):
    out = []
    for name in ("swagger-ui-bundle.js", "swagger-ui.css", "favicon.png"):
        path = Path(root) / name
        if path.is_file():
            out.append((str(path), root))
    return out


datas += _swagger_datas()
binaries = []
hiddenimports = []
tmp_ret = collect_all('openvino')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('openvino_tokenizers')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('langchain-core')
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
    excludes=['openvino.torch', 'torch', 'datasets', 'pandas', 'sklearn', 'scipy', 'matplotlib', 'optimum', 'sentence_transformers', 'transformers', 'onnx', 'lance', 'langchain', 'onnxruntime'],
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