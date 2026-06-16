# grafit

**Code knowledge-graph + семантический поиск по коду, доступный из любого чата через MCP.**

grafit строит граф знаний кодовой базы (через [graphify](https://github.com/safishamsi/graphify)),
заливает его в **FalkorDB** с **локальными эмбеддингами** (fastembed, без API) и даёт
гибридный поиск: вектор + граф-обход + фильтры. Один инстанс FalkorDB, **один именованный
граф на проект**. MCP-сервер делает поиск доступным в Claude Code, Cursor и любом
MCP-клиенте.

## Возможности
- Семантический поиск по коду (мультиязычный: русский запрос → англоязычный код).
- Эмбеддинг = имя символа + **сниппет исходника** → находит по смыслу, а не по имени.
- Граф-обход: от найденного узла к связанным (тест → реализация, вызовы, импорты).
- Инкрементальная переиндексация (кэш эмбеддингов: считаются только изменённые узлы).
- Фильтр генерик-узлов, демоция тестов; опц. гибрид вектор+лексика (RRF) и кросс-энкодер.
- **Общий сервис эмбеддингов** (`grafit-embed`): модель грузится в RAM один раз и отвечает
  всем агентам/процессам по HTTP — нет N×копий модели в памяти.
- Eval-харнесс (recall@k, MRR) для измеримой настройки.
- Локально и бесплатно (FalkorDB SSPL self-host, эмбеддинги на CPU).

## Установка
```bash
uv tool install grafit-mcp           # или: pipx install grafit-mcp
uv tool install graphifyy            # экстрактор графа (нужен для `grafit load`)
```
Требуется Python 3.10–3.13 и Docker.

## Быстрый старт
```bash
grafit up                            # поднять FalkorDB + общий сервис эмбеддингов (docker)
cd /path/to/your/project
graphify .                           # построить graph.json (или `grafit load --build`)
grafit load                          # проиндексировать проект (имя графа = имя репо)
grafit query "как работает аутентификация по JWT"
grafit list                          # графы проектов в инстансе
```
Первый `grafit up` собирает образ `grafit-embed` и **запекает в него модель** (~2 ГБ,
разово, несколько минут). Дальше старт мгновенный.

## Подключение к проекту (runbook'и)
Пошаговые инструкции по настройке в другом проекте на той же машине:
- **Claude Code** (полный цикл: graphify-сборка + grafit MCP + сохранение изменений в граф
  через git-хук) — [`docs/setup-for-claude-code.md`](docs/setup-for-claude-code.md).
- **opencode** (подключить MCP **только для чтения**; граф ведёт Claude Code) —
  [`docs/setup-for-opencode-readonly.md`](docs/setup-for-opencode-readonly.md).
- **Веб-интерфейс графа** (FalkorDB Browser на :6400, что вводить при входе) —
  [`docs/falkordb-browser.md`](docs/falkordb-browser.md).

## Общий сервис эмбеддингов (`grafit-embed`)
Чтобы модель не грузилась в RAM в каждый процесс (по копии на агента/MCP-клиента),
`grafit up` поднимает контейнер **`grafit-embed`** на `127.0.0.1:6401`: модель в памяти
**один раз**, клиенты (MCP, `grafit load`, `grafit query`) считают вектор по HTTP.

- `grafit up` прописывает `embed_url` в `~/.grafit/config.json` — клиенты подхватывают
  автоматически. Переопределяется `GRAFIT_EMBED_URL=http://127.0.0.1:6401`.
- **Векторы идентичны** локальному fastembed (та же модель/реализация) → переиндексация
  не нужна; `make_embedder` использует сервис только если его модель совпадает с нужной.
- Фоллбэк: если сервис недоступен или модель не совпала — процесс честно предупреждает
  и грузит модель локально (рабочий режим ценой RAM в этом процессе).
- Сменить модель: `grafit up --rebuild-embed --embed-model <name>` (затем перезалить
  проекты тем же `--model`, чтобы индекс совпал). Пропустить сервис: `grafit up --no-embed`.

### Фиксация версии (детерминизм векторов)
Вектора зависят не от имени модели, а от **версии fastembed** (пример: e5-large сменил
CLS→mean pooling между версиями). Поэтому fastembed **запинен точно** (`==0.8.0`) в трёх
местах, которые обязаны совпадать: `pyproject.toml`, `Dockerfile.embed`, `PINNED_FASTEMBED`
в `grafit/cli.py`. Дополнительная защита от дрейфа:
- `grafit load` пишет версию fastembed индекса в `config['fastembed']`;
- `/health` сервиса отдаёт свою версию fastembed;
- `make_embedder` использует сервис только если **совпадают и модель, и версия** — иначе
  предупреждает и грузит локально, не подмешивая несовместимые вектора в индекс.

Меняешь пин fastembed — обнови все три места и перезалей проекты `grafit load --no-cache`
(хеш кэша = `sha1(model+text)`, версию fastembed не учитывает).

## MCP — использование во всех чатах
Запуск сервера: `grafit mcp` (stdio). Подключение в Claude Code:
```json
{ "mcpServers": { "grafit": { "command": "grafit", "args": ["mcp"] } } }
```
Инструменты: `grafit_search`, `grafit_list_projects`, `grafit_explain`, `grafit_find_path`,
`grafit_status`, `grafit_tests`, `grafit_impact`, `grafit_trace`. Проект определяется по
текущей папке клиента (или параметром `project`).

Каждый ответ начинается со **строки свежести** графа (на каком коммите построен, отстал ли
от HEAD, грязно ли дерево). У `grafit_search` есть `kind` — фильтр узлов `all|code|tests|docs|prod`
(`prod` = код без тестов/миграций/генерёнки). Связи помечены: `─` структурная (из AST),
`⋯ … (inferred)` — выводная (по смыслу).

### Навигация по графу (без эмбеддингов)
Направленный обход типизированных рёбер — для работы перед изменением:
- **`grafit_tests(symbol)`** — какие тесты связаны с символом (зови ПЕРЕД правкой).
- **`grafit_impact(symbol)`** — что сломается при изменении: входящие зависимости,
  сгруппированы по `tests/frontend/backend/contract/docs`.
- **`grafit_trace(source, target="", with_references=False)`** — поток вперёд (endpoint →
  handler → service → …) деревом; с `target` — кратчайший путь `source→target`.

Резолв имени предпочитает определение (точное имя → не-тест → код; `Foo`, `Foo()` и `.Foo()`
эквивалентны); при неоднозначности сообщает число кандидатов. Обход capнут (`node_cap`),
усечение всегда помечается `(+N ещё)`.

## Разделение данных между проектами
Один FalkorDB, **один именованный граф на проект** (имя = basename git-репо, очищенное).
Реестр `~/.grafit/projects.json` ловит коллизии имён. Модель эмбеддингов общая
(`~/.grafit/config.json`), кэш — `~/.grafit/cache/`. Состояние переопределяется
переменной `GRAFIT_HOME`.

## Ранжирование (конвейер `search.py`)
вектор-KNN (+ опц. лексика full-text → взвешенный RRF) → фильтр генериков → демоция
тестов → опц. кросс-энкодер реранкер → top-k + соседи по графу.

> По бенчмаркам на сильном эмбеддере (`multilingual-e5-large`) гибрид RRF и реранкер
> прироста не дали (модель сама вбирает лексику через сниппеты) — оба **opt-in**.
> Дефолт: чистый вектор + фильтр генериков + демоция тестов. Меряйте `grafit eval`.

## Свежесть графа
Главный риск архитектуры «граф отдельно от кода» — молча устаревший граф. Поэтому
`grafit load` записывает в `~/.grafit/meta.json` git-коммит, на котором построен граф
(+ время, ветку, счётчики). Дальше:

- **`grafit status`** (CLI и MCP-инструмент `grafit_status`) — отчёт: на каком коммите граф,
  насколько HEAD ушёл вперёд, грязно ли рабочее дерево. `--all` — по всем графам.
- **Строка свежести** идёт шапкой к каждому ответу `grafit_search`/`explain`/`find_path`/`query`:
  `граф @ abc123 · свежий` либо `⚠ … HEAD +N · дерево грязное — перезалей`.
- Состояния: `fresh` (совпал, чисто) · `dirty` (несохранённые правки — граф их не видит) ·
  `behind:N` (HEAD ушёл вперёд) · `diverged` (ребейз/другая ветка) · `unknown` (старый граф
  без метки / вне git).

Граф, залитый старой версией grafit, метки не имеет — `status` это честно покажет и
предложит перезалить. Метаданные появляются при первом `grafit load` после обновления.

## Авто-обновление графа (post-commit)
Цепочка обновления: правки кода → graphify обновляет `graphify-out/graph.json` →
**`grafit load`** заливает его в FalkorDB → MCP видит свежие данные (без рестарта,
коннект к БД на каждый запрос). Шаг `grafit load` можно автоматизировать в git-хуке,
дождавшись, пока graphify допишет `graph.json`:

```sh
# .git/hooks/post-commit (после блока graphify hook)
if command -v grafit >/dev/null 2>&1; then
    base=$(stat -c %Y graphify-out/graph.json 2>/dev/null || echo 0)
    nohup env DIR="$(pwd)" BASE="$base" sh -c '
        cd "$DIR"; i=0; stable=0; prev=""
        while [ $i -lt 150 ]; do
            cur=$(stat -c %Y graphify-out/graph.json 2>/dev/null || echo 0)
            [ "$cur" -gt "$BASE" ] && { [ "$cur" = "$prev" ] && stable=$((stable+1)) || stable=0; [ $stable -ge 3 ] && break; }
            prev=$cur; i=$((i+1)); sleep 2
        done
        ulimit -v 8000000 2>/dev/null; grafit load
    ' >>~/.cache/grafit-load.log 2>&1 </dev/null &
fi
```
Документация (LLM-семантика) обновляется отдельно: `graphify --update` → `grafit load`.

## Порты
grafit поднимает контейнеры на уникальных хост-портах (127.0.0.1 only):
- **6399** — FalkorDB, данные/запросы (Redis-протокол). `--port` (CLI) / `GRAFIT_PORT` (MCP).
- **6400** — встроенный web-UI графа FalkorDB.
- **6401** — `grafit-embed`, HTTP `/embed` и `/health`. `GRAFIT_EMBED_URL` у клиентов.

Уникальны, чтобы не конфликтовать со стандартным Redis/FalkorDB на 6379 и dev-сервером на 3000.

## Память
Эмбеддинг крупных моделей на CPU ограничивайте `--threads` (по умолч. 4). Прогоны под
`ulimit -v` рекомендуются. Кэш эмбеддингов делает повторную заливку лёгкой (без модели).

## Команды
`grafit up [--no-embed --embed-model M --rebuild-embed]` · `grafit down` ·
`grafit load [path] [--build --model M --graph N --no-snippets]` ·
`grafit query "…" [-k --hybrid --rerank --neighbors --kind code|tests|docs|prod]` ·
`grafit status [--graph N --all]` · `grafit tests <symbol>` · `grafit impact <symbol>` ·
`grafit trace <source> [--to T --with-references --hops N]` · `grafit list` ·
`grafit eval [--golden file]` · `grafit mcp`

## Лицензия
MIT (см. `LICENSE`). FalkorDB — SSPLv1 (бесплатно для self-host).
