# grafit MCP в opencode — режим только для чтения

Инструкция для **opencode** на этой же VM: подключить grafit MCP, чтобы агенты искали по
коду **семантически** (вектор по графу), но **только читали** граф. Построение и
актуализация графа — НЕ забота opencode: этим занимается Claude Code (skill `/graphify` +
git-хуки `grafit load`, см. `setup-for-claude-code.md`). opencode — потребитель.

> Почему «только чтение» получается само: все MCP-инструменты grafit (`grafit_search`,
> `grafit_explain`, `grafit_find_path`, `grafit_list_projects`) выполняют только запросы к
> FalkorDB и ничего не мутируют. Запись в граф идёт исключительно через CLI `grafit load`,
> которого в MCP нет. Так что MCP безопасен по определению — отдельный «read-only mode»
> включать не требуется, достаточно не запускать `grafit load`/`grafit up|down` из opencode.

---

## 1. Предусловия (обеспечивает Claude Code / владелец VM)

opencode НЕ поднимает инфраструктуру и НЕ заливает граф. К моменту использования должно
быть уже сделано (кем-то один раз):

- `grafit up` — поднят FalkorDB (`:6399`) и общий сервис эмбеддингов `grafit-embed` (`:6401`).
- `grafit load` в нужных проектах — графы залиты (проверить: `grafit list`).

Если граф проекта не залит — это сигнал владельцу проекта (Claude Code), а не повод
opencode что-то «чинить» заливкой.

---

## 2. Подключить MCP в opencode

MCP-сервер `grafit mcp` — локальный stdio-процесс. opencode конфигурирует его в `mcp`
секции `opencode.jsonc`. Глобально (`~/.config/opencode/opencode.jsonc`) — для всех
проектов, или в `opencode.jsonc` конкретного проекта.

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "grafit": {
      "type": "local",
      "command": ["grafit", "mcp"],
      "enabled": true
    }
  }
}
```

Если в конфиге уже есть `mcp` с другими серверами (context7, playwright) — просто добавь
ключ `grafit` рядом, не перезаписывая остальные.

`grafit mcp` сам берёт хост/порт FalkorDB из env `GRAFIT_HOST`/`GRAFIT_PORT` (по умолчанию
`localhost:6399`) и эмбеддер — из общего `grafit-embed` (через `~/.grafit/config.json`),
поэтому **локально модель в RAM не грузит**: процесс лёгкий, подходит для многих
параллельных агентов.

---

## 3. Инструменты (все только читают)

| Инструмент | Назначение |
|------------|-----------|
| `grafit_search(question, k, project, neighbors)` | Семантический поиск по коду: релевантные узлы + цитаты `path:line` + соседи по графу |
| `grafit_explain(symbol, project)` | Объяснить узел (класс/функция/концепт) и его связи |
| `grafit_find_path(source, target, project)` | Кратчайший путь между двумя сущностями |
| `grafit_similar(symbol, threshold, kind)` | Near-duplicate символы для символа (код→код): кандидаты на рефакторинг |
| `grafit_dupes(kind, threshold, limit)` | Глобальный скан копипаста: кластеры near-duplicate символов |
| `grafit_list_projects()` | Какие проекты залиты в общий FalkorDB |

Проект определяется по **текущей папке** opencode (basename git-репо) или явным параметром
`project` (имя графа из `grafit_list_projects`).

---

## 4. Как агентам это использовать

- Вопрос о кодовой базе («где обрабатывается X», «что вызывает Y», «как устроен Z») —
  сначала `grafit_search`, затем читать процитированные `path:line`. Это дешевле и точнее,
  чем слепой grep по всему репозиторию.
- `grafit_explain` — быстро понять роль символа и его связи (тест → реализация, вызовы).
- `grafit_find_path` — проследить связь между двумя частями системы.

**Чего НЕ делать из opencode:**

- НЕ запускать `grafit load`, `grafit up`, `grafit down`, `grafit query --rebuild` и т.п. —
  это операции владельца графа (Claude Code/VM), они меняют общее состояние.
- НЕ считать пустой результат поиска багом инфраструктуры — возможно, проект ещё не залит
  или граф устарел; это эскалация к владельцу, а не повод заливать самому.

---

## 5. (Опц.) Жёстко ограничить инструменты

MCP grafit и так не имеет пишущих инструментов. Если хочется явно зафиксировать на уровне
агента, какие инструменты доступны, используй ограничение инструментов opencode (per-agent
`tools`/permission) и оставь только `grafit_*`. Для штатного использования это не требуется
— достаточно правил из шага 4.

---

## Чеклист

```
[ ] grafit up + grafit load выполнены владельцем (grafit list показывает проект)
[ ] mcp.grafit добавлен в opencode.jsonc (type local, command ["grafit","mcp"], enabled)
[ ] агенты ищут через grafit_search/explain/find_path, читают path:line
[ ] из opencode НЕ запускаются grafit load/up/down (только чтение)
```
