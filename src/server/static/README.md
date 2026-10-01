# Статика Swagger UI (офлайн)

Каталог намеренно пустой в репозитории — здесь лежат только инструкции.

## Зачем

`fastapi.openapi.docs.get_swagger_ui_html()` по умолчанию подставляет в страницу
`/docs` ссылки на `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/...`. В
сегменте Sigma интернета нет: страница отдаётся с кодом 200, скрипт молча не
грузится, и пользователь видит пустое белое поле. Ошибки при этом нет нигде —
ни в логе сервера, ни в ответе.

`src/server/app.py` устроен так, что **наличие файлов здесь не обязательно**:
если их нет, работает штатный вариант с CDN, а в лог уходит предупреждение.
Положите файлы, если нужен рабочий Swagger в закрытом контуре.

## Что положить

    swagger-ui-bundle.js
    swagger-ui.css
    favicon.png        (необязательно)

## Откуда взять

Вариант 1 — с внутреннего зеркала PyPI:

    pip download swagger-ui-bundle -d /tmp/sui \
        --index-url https://sberosc.sigma.sbrf.ru/repo/pypi/simple
    # распаковать колесо и забрать из него swagger_ui_bundle/vendor/

Вариант 2 — с машины, у которой есть интернет (версию фиксировать явно):

    curl -LO https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js
    curl -LO https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css

## После добавления

Файлы должны попасть в бандл PyInstaller — за это отвечает строка с
`src/server/static` в `datas` внутри `main.spec`. Проверить после сборки:

    ls dist/main/_internal/src/server/static/

И на живом демоне — страница обязана открываться при выдернутой сети:

    xdg-open "http://127.0.0.1:$(cat ~/.files-search/port)/docs"
