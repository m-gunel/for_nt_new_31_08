.PHONY: build deb dist_mcp dist_extras docker_build openvino spec tests reranker-model
# Дефис обязателен: .secret создаёт Jenkins и разработчик, в чистом клоне его
# нет. Без дефиса make отказывается выполнять ЛЮБУЮ цель — включая tests, ruff
# и dist_mcp, которым токен не нужен вовсе.
-include .secret
export

# Флаги dpkg-buildpackage для цели deb; см. комментарий у её вызова.
DEB_BUILD_FLAGS ?= --ignore-builtin-builddeps
# Чем собирать пакет в цели deb. scripts/build_deb_nosudo.sh подставляет
# scripts/deb_rules_build.sh — те же шаги debian/rules без служебных шагов
# dpkg-buildpackage, которым нужна закрытая на SberOS база /var/lib/dpkg/status.
DEB_BUILD_CMD ?= dpkg-buildpackage -us -uc -b $(DEB_BUILD_FLAGS)

# Откуда uv берёт пакеты — так же, как в Dockerfile. uv не читает pip.conf, и
# без этих строк идёт на публичный pypi.org, а из корпоративной сети тот
# недоступен: «invalid peer certificate: UnknownIssuer». Индекс — зеркало
# SberOSC, токен — из .secret (строка OSC_TOKEN=...); без токена — зеркало без
# авторизации. Сертификаты — системные (в них корпоративный CA): UV_SYSTEM_CERTS
# в uv >= 0.11, UV_NATIVE_TLS — прежнее имя той же настройки для старых uv.
# Всё через ?=: заданное в окружении не перебивается.
ifdef OSC_TOKEN
export UV_INDEX_URL ?= https://token:$(OSC_TOKEN)@sberosc.sigma.sbrf.ru/repo/pypi/simple
else
export UV_INDEX_URL ?= https://sberosc.sigma.sbrf.ru/repo/pypi/simple
endif
export UV_SYSTEM_CERTS ?= 1
export UV_NATIVE_TLS ?= 1

spec:
	uv run --active --native-tls pyi-makespec \
		--add-data=./models:./models \
		--collect-all openvino \
		--collect-all openvino_tokenizers \
		--collect-all uvicorn \
		--collect-all langchain-core \
		--exclude-module openvino.torch \
		--exclude-module=torch \
		--exclude-module=datasets \
		--exclude-module=pandas \
		--exclude-module=sklearn \
		--exclude-module=scipy \
		--exclude-module=matplotlib \
		--exclude-module=optimum \
		--exclude-module=sentence_transformers \
		--exclude-module=transformers \
		--exclude-module=onnx \
		--exclude-module=lance \
		--exclude-module=langchain \
		--exclude-module=onnxruntime \
		main.py
		

# Намеренно БЕЗ зависимости от `spec`: main.spec поддерживается руками (в нём
# фильтр каталога модели, отсекающий pickle-артефакты обучения), а pyi-makespec
# его перезаписывает. Раньше `make build` молча затирал эту правку, и локальная
# сборка расходилась с CI, который собирает по закоммиченному main.spec.
# --frozen: ставить ровно то, что записано в uv.lock (все пакеты оттуда — из
# зеркала SberOSC), не пересчитывая зависимости. Без него uv сверяет lock с
# pyproject.toml, видит расхождение (в lock у самого проекта записана давняя
# версия) и пересчитывает всё заново через индекс — а это лишний поход в сеть и
# другие версии пакетов, чем в lock.
build:
	uv sync --active --frozen --group build
	uv run --frozen pyinstaller main.spec
	$(MAKE) dist_mcp dist_extras

# PyInstaller собирает только main.py, а Jenkins зипует каталог dist/ целиком.
# Без этой цели MCP-мост в distro.zip не попадает, и на принимающей стороне
# оказывается индексатор без обвязки. Мост на чистом stdlib, поэтому рядом с
# бинарём ему достаточно системного python3.
#
# Цель обязана вызываться из ОБОИХ путей сборки: `build` собирает локально,
# а CI идёт через `docker_build`. Раньше она висела только на `build`, и
# артефакт Jenkins уезжал в Nexus без моста.
dist_mcp:
	install -d dist/mcp
	install -m 0755 mcp_launcher.sh dist/mcp/
	install -m 0755 scripts/register_mcp.py dist/mcp/
	install -m 0644 mcp_server.py docs/mcp/README.md dist/mcp/

# Конфиг и скрипты обслуживания.
#
# Без cfg/ распакованный distro.zip стартует с пустым file_scan_cfg.paths —
# указывать CONFIG_PATH не на что, и поиск стабильно возвращает ноль. Кладём
# именно cfg/distro.yml, а не example.yml: в последнем задан относительный
# model_path, который в бинаре перебивает верный путь внутрь sys._MEIPASS.
#
# setup.sh возвращает бит +x, который теряет Java-zip Jenkins'а: без него не
# запускается ни бинарь демона, ни лаунчер моста, причём молча.
# uninstall.sh закрывает БТ §7 — остановка демона и полная очистка следов.
dist_extras:
	install -d dist/cfg
	install -m 0644 cfg/distro.yml dist/cfg/
	install -m 0755 scripts/dist_setup.sh dist/setup.sh
	install -m 0755 scripts/uninstall.sh dist/uninstall.sh

# .deb по БТ §4.1 «предустановленный системный пакет». Собирается из ГОТОВОГО
# dist/, своей сборки у пакета нет: сначала `make build` (локально) или
# `make docker_build` (как в CI), потом эта цель.
#
# Собирать пакет ТОЛЬКО этой целью. На рабочих ноутбуках umask 077, исходники
# лежат с правами 0600/0700, и ручная сборка (tar+ar, dpkg-deb -b из своего
# каталога) переносит их в пакет как есть. Так вышло с 1.0.6: юниты 0600, root
# их включил, а systemd --user пользователя прочитать не смог. Права к
# 0644/0755 приводит dh_fixperms, и только в сборке через debhelper.
deb:
	@test -d dist/main || { echo "нет dist/ — сначала make build или make docker_build" >&2; exit 1; }
	@test -d dist/mcp || { echo "нет dist/mcp — цель dist_mcp не отработала" >&2; exit 1; }
	@test -f dist/mcp/register_mcp.py || { echo "нет dist/mcp/register_mcp.py — без него MCP не пропишется ни у кого" >&2; exit 1; }
# Гейты по составу debian/. Каждый из них закрывает отказ, который иначе
# проявляется не ошибкой сборки, а молчаливой поломкой у пользователя.
	@test ! -f debian/lib-local-rag.service || { echo "debian/lib-local-rag.service подпадает под шаблон dh_installsystemd и даст ВТОРОЙ, системный юнит с запуском от root — оставьте только debian/lib-local-rag.user.service" >&2; exit 1; }
	@test -f debian/lib-local-rag.user.service || { echo "нет debian/lib-local-rag.user.service — dh_installsystemduser не разложит юнит демона, и пакет поставится, не подняв его ни у кого" >&2; exit 1; }
	@ls debian/units/*.service >/dev/null 2>&1 || { echo "нет debian/units/*.service — некому регистрировать MCP-мост" >&2; exit 1; }
	@ls debian/units/*.path >/dev/null 2>&1 || { echo "нет debian/units/*.path — перерегистрация после перезаписи конфига агентом работать не будет" >&2; exit 1; }
	@test -f debian/source/format || { echo "нет debian/source/format" >&2; exit 1; }
# Версия живёт в src/version.py; расхождение с changelog означает, что пакет
# назовётся не так, как считает само приложение, и /docs покажет чужой номер.
	@v=$$(sed -n 's/^__version__ = "\(.*\)"$$/\1/p' src/version.py); \
	 c=$$(sed -n '1s/^lib-local-rag (\(.*\)).*/\1/p' debian/changelog); \
	 test "$$v" = "$$c" || { echo "версии разошлись: src/version.py=$$v, debian/changelog=$$c" >&2; exit 1; }
# --ignore-builtin-builddeps: dpkg-checkbuilddeps неявно требует
# build-essential:native, а в образе сборки его нет и он не нужен, компилировать
# нечего. Флаг снимает только это неявное требование, Build-Depends из
# debian/control (debhelper-compat) по-прежнему проверяется.
#
# scripts/build_deb_nosudo.sh подставляет в DEB_BUILD_CMD
# scripts/deb_rules_build.sh: debhelper там распакован в домашний каталог,
# а база /var/lib/dpkg/status, по которой dpkg-buildpackage проверяет
# Build-Depends и пишет .buildinfo, на SberOS пользователю закрыта. Наличие
# инструментов тот скрипт проверяет сам.
	$(DEB_BUILD_CMD)
# Проверка готового пакета, а не только исходников: права внутри .deb, версия,
# ConditionUser в юнитах, вставка debhelper в postinst. 1.0.6 прошёл бы все
# гейты выше и всё равно дал бы у пользователя «Unit ... could not be found».
# Через sh: бит +x теряется при переносе проекта архивом, а sh он не нужен.
	@v=$$(sed -n 's/^__version__ = "\(.*\)"$$/\1/p' src/version.py); \
	 sh scripts/check_deb.sh "../lib-local-rag_$${v}_$$(dpkg --print-architecture).deb"

openvino:
	uv sync --active --native-tls --group torch
	uv run utils/convert_openvino_tokenizer.py
	uv run utils/convert_openvino.py

# Эксперимент: кросс-энкодер для переранжирования гибридной выдачи (выключен по умолчанию,
# reranker_model_cfg.enabled). Скачать с Hugging Face, сконвертировать в OpenVINO, сверить с
# исходной моделью: utils/reranker_model.py → models/reranker/. Пакеты конвертации — из зеркала
# на один запуск (uv run --with), uv.lock не меняется; transformers 5 — ради ModernBERT (arga).
# Другая модель или режим: RERANKER_ARGS="--model arga --mode fp16".
SYS_CA = /etc/ssl/certs/ca-certificates.crt
RERANKER_WITH = --group torch --with nncf --with huggingface-hub --with "transformers>=5.5"
reranker-model:
	$(if $(wildcard $(SYS_CA)),SSL_CERT_FILE=$(SYS_CA) REQUESTS_CA_BUNDLE=$(SYS_CA)) \
	uv run --frozen $(RERANKER_WITH) python utils/reranker_model.py $(RERANKER_ARGS)

export_zip:
	git archive --format=zip --output repo.zip HEAD
	openssl enc -aes-256-ctr -pbkdf2 -in repo.zip -out repo.zip.enc
	rm repo.zip

docker_build:
# Чистим первым делом, до docker build: docker cp в СУЩЕСТВУЮЩИЙ каталог кладёт
# источник внутрь него, и после прошлой сборки вышло бы dist/dist — zip собрался
# бы молча и оказался нерабочим. Заодно прошлый dist не уедет в docker-контекст.
	rm -rf ./dist
	docker build \
		--build-arg OSC_TOKEN=$(OSC_TOKEN) \
		--network host \
		-t rag-sberos:v1 .
# Имя контейнера фиксированное: если прошлая сборка упала между create и rm, он
# остался висеть, и следующий create падает на «name already in use». Снимаем
# заранее; ведущий дефис — отсутствие контейнера здесь не ошибка.
	-docker rm -f tc
	docker create --name tc rag-sberos:v1 /bin/sh
	docker cp tc:/app/dist ./dist && docker rm tc
# Мост в образ не копируется и собран быть не может — он не часть
# PyInstaller-бандла. Докладываем его на хосте, поверх извлечённого dist.
	$(MAKE) dist_mcp dist_extras

tests:
	uv run --frozen pytest -m "not integration"

ruff:
	uv run ruff format
	uv run ruff check src --fix
	uv run ruff check tests --fix
