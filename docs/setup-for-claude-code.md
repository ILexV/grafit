# Настройка graphify + grafit (knowledge-graph + семантический MCP) — runbook для Claude Code

Инструкция для Claude Code, работающего на **этой же VM** в другом проекте с похожим
agent-workflow. Два слоя:

- **graphify** — строит граф знаний по кодовой базе (`graphify-out/graph.json`). Часть I.
- **grafit** — заливает граф в общий **FalkorDB** + локальные эмбеддинги и даёт
  **семантический поиск по MCP** из любого чата. Часть II.

> Граф **per-project**: `graphify-out/` внутри репозитория + свой именованный граф в
> FalkorDB. Инструменты (graphify, grafit) и skill — глобальные, ставить заново НЕ нужно.

---

# Часть I — graphify (построение графа)

## 0. Предусловия (уже выполнено на VM — только проверить)

`uv`, `graphify`/`graphify-mcp` (пакет PyPI называется **`graphifyy`**, с двойной «y») и
глобальный skill `~/.claude/skills/graphify/` уже установлены. Проверка:

```bash
export PATH="$HOME/.local/bin:$PATH"
graphify --version          # ожидается graphify 0.8.x
ls ~/.claude/skills/graphify/SKILL.md
```

Если `graphify` не найден — только тогда переустанавливай:
`uv tool install graphifyy` (подтверждение установки спросят — это ожидаемо, имя пакета
`graphifyy` намеренно отличается от репозитория `graphify`).

⚠️ Системный `python3` **не** видит graphify (он в изолированном venv uv). Правильный
интерпретатор — из shebang бинаря: `head -1 $(which graphify) | sed 's/^#!//'`
(обычно `/home/lex/.local/share/uv/tools/graphifyy/bin/python`). Skill сам сохраняет его
в `graphify-out/.graphify_python` при первом прогоне.

---

## 1. `.graphifyignore` — САМЫЙ ВАЖНЫЙ ШАГ (сделать ДО сборки)

**graphify НЕ уважает вложенные `.gitignore`.** Без фильтра он затянет build-артефакты и
сгенерированные отчёты — корпус раздувается на порядки (в одном проекте дошло до 14M слов
и лога на 150 МБ), прогон становится дорогим и «зашумлённым».

Создай `.graphifyignore` в корне (синтаксис как у `.gitignore`). Базовый шаблон —
**адаптируй под стек проекта**:

```gitignore
# Артефакты graphify
graphify-out/

# Build-артефакты и зависимости (graphify не уважает вложенные .gitignore!)
**/bin/
**/obj/
**/node_modules/
**/dist/
**/build/
**/target/
**/.venv/
**/__pycache__/

# Сгенерированные отчёты (покрытие, мутации, тест-вывод)
**/TestResults/
**/coverage/
**/StrykerOutput/
**/playwright-report/
**/test-results/

# Процессный/оркестрационный шум — НЕ знания о продукте
.opencode/
.claude/
.qodana/
.git/
```

Как проверить, что фильтр сработал (до сборки):

```bash
cd <project-root>
PY=$(head -1 $(which graphify) | sed 's/^#!//')
mkdir -p graphify-out
$PY -c "
import json; from graphify.detect import detect; from pathlib import Path
d=detect(Path('.'))
print('files', d['total_files'], '| words', d['total_words'])
for k,v in d['files'].items():
    if v: print(' ', k, len(v))
"
```

Признаки незакрытого шума: `total_words` в миллионах, сотни файлов в `document`/`image`
из каталогов отчётов/задач. Если так — найди крупнейшие файлы и добавь их каталоги в
`.graphifyignore`:

```bash
find . -type f \( -name '*.md' -o -name '*.html' \) | xargs du -h 2>/dev/null | sort -rh | head
```

Ориентир здорового корпуса: десятки-сотни файлов кода + только **реальная** документация
(`docs/`, `README`), без архивов задач и генератов.

---

## 2. `.gitignore`

Чтобы артефакты графа не коммитились:

```bash
echo 'graphify-out/' >> .gitignore
```

---

## 3. Первичная сборка графа

В сессии Claude Code просто:

```
/graphify .
```

Skill сам пройдёт пайплайн. Что он делает и на что смотреть:

- **AST-экстракция кода** — детерминированная, бесплатная (без LLM). Покрывает все
  поддерживаемые языки.
- **Семантическая экстракция доков** — через параллельные general-purpose субагенты
  (по ~20 файлов на чанк). Это единственная дорогая по токенам часть.
- Если корпус **> 500 файлов или > 2M слов** — skill предупредит и предложит сузить.
  Это сигнал, что `.graphifyignore` недонастроен (вернись к шагу 1), а не повод дробить.

Выход в `graphify-out/`: `graph.html` (интерактив), `graph.json` (данные), `GRAPH_REPORT.md`.

> Хочешь дёшево, без LLM по докам? Можно построить только код:
> `graphify update .` (AST-only). Доки тогда в граф не попадут.

---

## 4. Автоактуализация графа (graphify-слой)

Делёж ответственности: **код** обновляется автоматически и бесплатно (AST),
**документация** — отдельно (нужен LLM).

### 4a. git-хук на код (рекомендуется — «поставил и забыл»)

```bash
export PATH="$HOME/.local/bin:$PATH"
graphify hook install     # post-commit + post-checkout, пересборка кода детачем
```

⚠️ **Если проект использует кастомный `core.hooksPath`** (например `.githooks`, `.husky`)
— хук ляжет туда, а эта папка обычно коммитится и навяжется всей команде. Чтобы оставить
хук **локальным**, добавь его в локальный игнор (он не коммитится):

```bash
git config core.hooksPath    # узнать папку; пусто = стандартный .git/hooks (тогда и так локально)
# если папка кастомная (напр. .githooks):
printf '\n.githooks/post-commit\n.githooks/post-checkout\n' >> .git/info/exclude
```

Проверка: после `git commit` в логе мелькнёт `[graphify hook] launching background rebuild`;
детали — `~/.cache/graphify-rebuild.log`. Хук skip-friendly: нет graphify → `exit 0`,
коммит не ломается.

### 4b. watcher (опц., для активной разработки без коммитов)

```bash
graphify watch .          # фоновый процесс (НЕ демон — перезапускать после reboot)
```

### 4c. документация (LLM) — через workflow, см. шаг 5

CLI `graphify update` обновляет **только код**. Доки требуют skill-флоу:
`/graphify --update` (инкрементально, только изменённые файлы — дёшево).

---

## 5. Встраивание в agent-workflow (если воркфлоу похож на этот)

Если в проекте есть агент, обновляющий документацию (аналог `docs-updater`), и
оркестратор задач — свяжи их с графом так же, как сделано здесь. Паттерн:

**docs-агент «пинает» сервис** (ему не нужны Bash/LLM — только Write):
после обновления доков, если есть каталог `graphify-out/`, пишет флаг-файл
`graphify-out/needs_update` с содержимым `1`. В список разрешённых файлов агента добавь
`graphify-out/needs_update`.

**Оркестратор применяет флаг на финализации** (у него есть skill и Bash):
если `graphify-out/needs_update` существует — вызвать `/graphify --update`, затем
**снять флаг явно**: `rm -f graphify-out/needs_update`.

⚠️ Имя флага — `needs_update` (без точки), а штатный cleanup в SKILL.md удаляет
`.needs_update` (с точкой) — рассинхрон в самом graphify. Поэтому снимай сам, иначе флаг
залипнет и будет триггерить лишние прогоны.

---

# Часть II — grafit (FalkorDB + эмбеддинги + семантический MCP)

graphify даёт `graph.json`; **grafit** превращает его в семантический поиск по коду,
доступный из любого MCP-клиента. Один общий FalkorDB на все проекты VM, **один именованный
граф на проект** (имя = basename git-репо). Поиск — по смыслу (вектор), а не по подстроке.

## 6. Предусловия grafit (один раз на VM)

`grafit` установлен как uv-tool (`~/.local/bin/grafit`, пакет PyPI `grafit-mcp`). Проверка:

```bash
grafit --help          # есть подкоманды up|down|load|query|list|mcp
```

Поднять стек (идемпотентно): **FalkorDB** + общий **сервис эмбеддингов**:

```bash
grafit up              # FalkorDB :6399 (UI :6400) + grafit-embed :6401
```

Первый `grafit up` собирает образ `grafit-embed` и **запекает в него модель** (e5-large,
~2 ГБ, разово, несколько минут). `grafit-embed` держит модель в RAM **один раз** и отвечает
всем агентам/чатам по HTTP — нет N×копий модели в памяти. `grafit up` сам прописывает
`embed_url` в `~/.grafit/config.json`, клиенты подхватывают автоматически.

## 7. Подключить проект к графу

```bash
cd <project-root>
grafit load            # имя графа = basename git-репо; читает graphify-out/graph.json
grafit list            # убедиться, что проект появился
grafit query "как работает аутентификация по JWT"   # smoke-проверка
```

Нет `graph.json`? Сначала собери граф (часть I, `/graphify .`) или `grafit load --build`
(grafit сам вызовет graphify). Заливка **инкрементальная**: повторный `load` пересчитывает
эмбеддинги только изменённых узлов (кэш в `~/.grafit/cache/`).

## 8. Подключить MCP в Claude Code

MCP-сервер `grafit mcp` — локальный stdio-процесс, который клиент поднимает сам.
Добавь в `.mcp.json` проекта (или `claude mcp add`):

```json
{ "mcpServers": { "grafit": { "command": "grafit", "args": ["mcp"] } } }
```

Инструменты MCP (**все только читают** граф, ничего не пишут):

- `grafit_search(question, k, project, neighbors, kind, snippet)` — семантический поиск по коду,
  цитаты `path:line` + соседи. `kind` = `all|code|tests|docs|prod`; `snippet=True` подмешивает
  реальные строки исходника (source-first, экономит чтение файла).
- `grafit_explain(symbol, project)` — объяснить узел (класс/функция) и его связи.
- `grafit_find_path(source, target, project)` — кратчайший путь между сущностями.
- `grafit_status(project)` — **свежесть графа**: на каком коммите построен, отстал ли от HEAD,
  грязно ли дерево. Зови перед доверием к результатам на большом/давнем проекте.
- `grafit_tests(symbol)` — связанные тесты символа (зови ПЕРЕД изменением).
- `grafit_impact(symbol)` — что сломается при изменении (входящие зависимости по
  категориям tests/frontend/backend/contract/docs).
- `grafit_trace(source, target, with_references)` — поток вперёд деревом (endpoint → handler →
  service); с `target` — кратчайший путь. Эти три — обход графа, без эмбеддингов (быстро).
  Дополняются convention-деривацией (`tested_by`/`handled_by`/`impl_of`, помечено `by naming`):
  связи DI/MediatR/тест-naming, которые graphify не извлёк; при пустом пути — fallback на
  связанные символы вместо «не найдено».
- `grafit_similar(symbol, threshold, kind, rerank)` — near-duplicate символы ВОТ ЭТОГО символа
  (код→код по вектору): кандидаты на общий хелпер/extract-method. Зови перед написанием новой
  функции или выносом общего кода. Отсекает co-location, именные семьи (`XCommand↔XCommandHandler`)
  и graph-связи. `threshold` — макс. косинусная дистанция (0=идентично, дефолт 0.10).
- `grafit_dupes(kind, threshold, limit, pairs, rerank)` — глобальный скан копипаста по проекту:
  кластеры near-duplicate символов (`kind=prod`; `pairs=True` — плоский список пар). Каждая
  находка помечена `копипаст` (Jaccard шинглов высок) vs `семантика`, кластеры контрактов —
  `extract base/template method`; сортировка по выгоде. Для аудита «где у нас дубли».
- `grafit_list_projects()` — какие проекты залиты в общий FalkorDB.

Каждый ответ начинается со **строки свежести** (`граф @ abc123 · свежий` либо `⚠ … HEAD +N —
перезалей`). Связи помечены: `─` структурная (AST), `⋯ … (inferred)` — выводная по смыслу.
Метка свежести берётся из `~/.grafit/meta.json`, который пишет `grafit load` (см. шаг 9 — хук
обновляет её автоматически на каждом коммите).

Проект определяется по **cwd клиента** (basename git-репо) или явным параметром `project`.
Несколько чатов/агентов работают одновременно: каждый поднимает свой лёгкий `grafit mcp`,
но модель эмбеддингов общая (в контейнере `grafit-embed`), FalkorDB обслуживает
конкурентные чтения штатно.

## 9. Сохранение изменений в граф (автообновление) — ГЛАВНОЕ

Полная цепочка на коммит:

```
git commit
  → graphify пересобирает graphify-out/graph.json   (часть I, graphify-хук)
  → grafit load заливает graph.json в FalkorDB        (grafit-хук, ниже)
  → MCP сразу видит свежие данные (коннект к БД на каждый запрос, рестарт не нужен)
```

graphify-хук (часть I, шаг 4a) обновляет только `graph.json`. Чтобы изменения дошли **до
FalkorDB/MCP**, добавь grafit-блок в `post-commit` **ПОСЛЕ** graphify-блока. Он ждёт в
фоне, пока graphify допишет `graph.json` (mtime > baseline + стабилизация), затем
`grafit load`. Не блокирует коммит, skip-friendly:

```sh
# --- grafit-hook-start (после graphify-блока) ---
[ "${GRAFIT_SKIP_HOOK:-0}" = "1" ] && exit 0
if command -v grafit >/dev/null 2>&1; then
    _GRAFIT_LOG="${HOME}/.cache/grafit-load.log"
    mkdir -p "$(dirname "$_GRAFIT_LOG")"
    _GJ_BASE=$(stat -c %Y graphify-out/graph.json 2>/dev/null || echo 0)
    echo "[grafit hook] grafit load запланирован после пересборки графа (log: $_GRAFIT_LOG)"
    nohup env GRAFIT_DIR="$(pwd)" GJ_BASE="$_GJ_BASE" sh -c '
        cd "$GRAFIT_DIR" || exit 0
        GJ="graphify-out/graph.json"; prev=""; stable=0; i=0
        while [ $i -lt 150 ]; do
            cur=$(stat -c %Y "$GJ" 2>/dev/null || echo 0)
            if [ "$cur" != "0" ] && [ "$cur" -gt "$GJ_BASE" ]; then
                if [ "$cur" = "$prev" ]; then stable=$((stable + 1)); else stable=0; fi
                [ $stable -ge 3 ] && break
            fi
            prev="$cur"; i=$((i + 1)); sleep 2
        done
        ulimit -v 8000000 2>/dev/null
        grafit load
    ' >>"$_GRAFIT_LOG" 2>&1 </dev/null &
fi
# --- grafit-hook-end ---
```

Как у graphify-хука: при кастомном `core.hooksPath` оставь хук **локальным** через
`.git/info/exclude` (см. шаг 4a). Проверка: после коммита в `~/.cache/grafit-load.log`
появится прогон `grafit load`; `grafit query` отдаёт свежие узлы.

> **Документация (LLM-семантика)** в FalkorDB не обновляется хуком автоматически — как и в
> части I: `graphify update .` (или `/graphify --update`) → затем `grafit load`.

## 10. Фиксация версии модели (детерминизм векторов)

Вектора зависят не от имени модели, а от **версии fastembed** (пример: e5-large сменил
CLS→mean pooling между версиями). В grafit `fastembed` запинен точно (`==0.8.0`), `/health`
сервиса отдаёт версию, а клиент сверяет её с версией индекса — и не подмешивает
несовместимые вектора. Менять модель/версию — осознанно: `grafit up --rebuild-embed
--embed-model <name>`, затем перезалить проекты `grafit load --no-cache`.

---

## Чеклист быстрого старта

```
graphify (часть I):
[ ] graphify --version  (если нет — uv tool install graphifyy)
[ ] .graphifyignore настроен под стек  ← без этого корпус раздувается
[ ] echo 'graphify-out/' >> .gitignore
[ ] /graphify .  (первичная сборка; total_words не в миллионах)
[ ] graphify hook install  (+ .git/info/exclude при кастомном core.hooksPath)

grafit (часть II):
[ ] grafit up  (FalkorDB :6399 + grafit-embed :6401; первый раз собирает образ)
[ ] grafit load  (из корня проекта; grafit list — проект виден)
[ ] MCP в .mcp.json: { "grafit": { "command": "grafit", "args": ["mcp"] } }
[ ] grafit-блок в post-commit ПОСЛЕ graphify-блока (+ .git/info/exclude)
[ ] (опц.) docs-флоу: graphify update . → grafit load для семантики доков
```
