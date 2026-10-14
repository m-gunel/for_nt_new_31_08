.PHONY: build deb dist_mcp dist_extras docker_build openvino spec tests reranker-model
# Дефис: в чистом клоне .secret нет, а без него make не выполнит ни одной цели.
-include .secret
# Без общего export: OSC_TOKEN и прочее из .secret в окружение рецептов не уходят.
# uv получает индекс через UV_INDEX_URL ниже, docker_build — токен своим export.

# dpkg-checkbuilddeps неявно требует build-essential:native, а его нет в образе
# сборки и компилировать нечего. Build-Depends из debian/control проверяются.
DEB_BUILD_FLAGS ?= --ignore-builtin-builddeps
# scripts/build_deb_nosudo.sh подменяет на scripts/deb_rules_build.sh (зачем — там).
DEB_BUILD_CMD ?= dpkg-buildpackage -us -uc -b $(DEB_BUILD_FLAGS)

# uv не читает pip.conf и без этого идёт на pypi.org, недоступный из корпоративной
# сети. Индекс — зеркало SberOSC, токен — OSC_TOKEN из .secret, если он есть.
# Сертификаты системные (в них корпоративный CA): UV_SYSTEM_CERTS для uv >= 0.11,
# UV_NATIVE_TLS — то же для старых uv.
ifdef OSC_TOKEN
export UV_INDEX_URL ?= https://token:$(OSC_TOKEN)@sberosc.sigma.sbrf.ru/repo/pypi/simple
else
export UV_INDEX_URL ?= https://sberosc.sigma.sbrf.ru/repo/pypi/simple
endif
export UV_SYSTEM_CERTS ?= 1
export UV_NATIVE_TLS ?= 1

spec:
	uv run --native-tls pyi-makespec \
		--add-data=./models:./models \
		--collect-all openvino \
		--collect-all openvino_tokenizers \
		--collect-all uvicorn \
		--collect-all langchain_core \
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
		--exclude-module=langchain \
		--exclude-module=onnxruntime \
		main.py
		

# Не зависит от spec: main.spec правится руками, pyi-makespec его затрёт.
# --frozen ставит ровно uv.lock; без него uv, сочтя lock устаревшим, пересчитает
# зависимости через индекс и возьмёт другие версии.
# Без --active: uv run его не учитывает, и sync с run работали бы с разными venv.
build:
	uv sync --frozen --group build
	uv run --frozen pyinstaller main.spec
	$(MAKE) dist_mcp dist_extras

# PyInstaller пакует только main.py: без этой цели MCP-мост не попадёт в
# distro.zip. Мост на чистом stdlib, ему хватает системного python3. Цель должна
# вызываться из обоих путей сборки: build (локально) и docker_build (CI).
dist_mcp:
	install -d dist/mcp
	install -m 0755 mcp_launcher.sh dist/mcp/
	install -m 0755 scripts/register_mcp.py dist/mcp/
	install -m 0644 mcp_server.py docs/mcp/README.md dist/mcp/

# Конфиг и скрипты обслуживания поставки. Именно distro.yml: относительный
# model_path из example.yml в бинаре перебивает путь внутрь sys._MEIPASS.
# setup.sh возвращает +x, который теряет zip в Jenkins; uninstall.sh — БТ §7.
dist_extras:
	install -d dist/cfg
	install -m 0644 cfg/distro.yml dist/cfg/
	install -m 0755 scripts/dist_setup.sh dist/setup.sh
	install -m 0755 scripts/uninstall.sh dist/uninstall.sh

# .deb (БТ §4.1) из готового dist/: сначала make build или make docker_build.
# Собирать только этой целью: при umask 077 ручная сборка (tar+ar, dpkg-deb -b)
# переносит в пакет права 0600/0700, и systemd --user не видит юниты.
# Права выравнивает только dh_fixperms в сборке через debhelper.
deb:
	@test -d dist/main || { echo "нет dist/ — сначала make build или make docker_build" >&2; exit 1; }
	@test -d dist/mcp || { echo "нет dist/mcp — цель dist_mcp не отработала" >&2; exit 1; }
	@test -f dist/mcp/register_mcp.py || { echo "нет dist/mcp/register_mcp.py — без него MCP не пропишется ни у кого" >&2; exit 1; }
# Гейты по составу debian/: без них пакет соберётся, но молча сломается у пользователя.
	@test ! -f debian/lib-local-rag.service || { echo "debian/lib-local-rag.service подпадает под шаблон dh_installsystemd и даст ВТОРОЙ, системный юнит с запуском от root — оставьте только debian/lib-local-rag.user.service" >&2; exit 1; }
	@test -f debian/lib-local-rag.user.service || { echo "нет debian/lib-local-rag.user.service — dh_installsystemduser не разложит юнит демона, и пакет поставится, не подняв его ни у кого" >&2; exit 1; }
	@ls debian/units/*.service >/dev/null 2>&1 || { echo "нет debian/units/*.service — некому регистрировать MCP-мост" >&2; exit 1; }
	@ls debian/units/*.path >/dev/null 2>&1 || { echo "нет debian/units/*.path — перерегистрация после перезаписи конфига агентом работать не будет" >&2; exit 1; }
	@test -f debian/source/format || { echo "нет debian/source/format" >&2; exit 1; }
# src/version.py, debian/changelog и pyproject.toml должны совпадать, иначе пакет,
# /docs и метаданные проекта разойдутся в версии.
	@v=$$(sed -n 's/^__version__ = "\(.*\)"$$/\1/p' src/version.py); \
	 c=$$(sed -n '1s/^lib-local-rag (\(.*\)).*/\1/p' debian/changelog); \
	 p=$$(sed -n 's/^version = "\(.*\)"$$/\1/p' pyproject.toml); \
	 test "$$v" = "$$c" && test "$$v" = "$$p" || { echo "версии разошлись: src/version.py=$$v, debian/changelog=$$c, pyproject.toml=$$p" >&2; exit 1; }
	$(DEB_BUILD_CMD)
# Проверка готового .deb: гейты выше не видят прав внутри пакета.
# Через sh: бит +x теряется при переносе проекта архивом.
	@v=$$(sed -n 's/^__version__ = "\(.*\)"$$/\1/p' src/version.py); \
	 sh scripts/check_deb.sh "../lib-local-rag_$${v}_$$(dpkg --print-architecture).deb"

# Чекпойнта с FP32-весами и набора калибровки в репозитории нет — пути задаются
# явно, результат пишется в MODEL_SRC. Модель конвертируется первой: каталог без
# весов (поставочный models/...) отвергается до перезаписи токенизатора.
openvino:
	@test -n "$(MODEL_SRC)" && test -n "$(CALIB)" || { echo "укажите MODEL_SRC=<каталог чекпойнта с FP32-весами> CALIB=<parquet с текстами калибровки>" >&2; exit 1; }
	uv sync --native-tls --group torch
	uv run utils/convert_openvino.py --model "$(MODEL_SRC)" --calib "$(CALIB)"
	uv run utils/convert_openvino_tokenizer.py --model "$(MODEL_SRC)"

# Реранкер (models/reranker, лежит в репозитории): пересобрать — скачать с Hugging Face,
# сконвертировать в OpenVINO (INT8 весов), сверить с исходной моделью и разрезать веса на
# части < 100 МБ (utils/reranker_model.py). Пакеты конвертации — на один запуск (uv run
# --with), uv.lock не меняется. Другая модель или режим: RERANKER_ARGS="--model mminilm".
SYS_CA = /etc/ssl/certs/ca-certificates.crt
RERANKER_WITH = --group torch --with nncf --with huggingface-hub --with "transformers>=5.5"
reranker-model:
	$(if $(wildcard $(SYS_CA)),SSL_CERT_FILE=$(SYS_CA) REQUESTS_CA_BUNDLE=$(SYS_CA)) \
	uv run --frozen $(RERANKER_WITH) python utils/reranker_model.py $(RERANKER_ARGS)

export_zip:
	git archive --format=zip --output repo.zip HEAD
	openssl enc -aes-256-ctr -pbkdf2 -in repo.zip -out repo.zip.enc
	rm repo.zip

# Токен в выводе make не печатается. Dockerfile из CI берёт его из pip.conf и своей
# строки uv.toml, а BuildKit-секрет получит только RUN --mount=type=secret.
docker_build: export OSC_TOKEN := $(OSC_TOKEN)
docker_build:
# До docker build: docker cp в существующий каталог даст dist/dist и нерабочий
# zip, а старый dist не уедет в docker-контекст.
	rm -rf ./dist
	docker build \
		--secret id=osc_token,env=OSC_TOKEN \
		--network host \
		-t rag-sberos:v1 .
# Контейнер мог остаться от упавшей сборки (create упадёт на «name already in use»);
# дефис — его отсутствие не ошибка.
	-docker rm -f tc
	docker create --name tc rag-sberos:v1 /bin/sh
# Итоговый stage собирает пакет в /build: dist там, в /app — только local-rag.deb.
	docker cp tc:/build/dist ./dist && docker rm tc
# Мост не входит в PyInstaller-бандл — докладываем его на хосте поверх dist.
	$(MAKE) dist_mcp dist_extras

tests:
	uv run --frozen pytest -m "not integration"

ruff:
	uv run ruff format
	uv run ruff check src --fix
	uv run ruff check tests --fix
