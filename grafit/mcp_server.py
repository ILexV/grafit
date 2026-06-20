"""MCP-сервер grafit: семантический поиск по коду из любого MCP-клиента (stdio).

Инструменты: grafit_search, grafit_list_projects, grafit_explain, grafit_find_path,
grafit_status, grafit_tests, grafit_impact, grafit_trace.
Эмбеддер грузится лениво (на первый запрос) и кэшируется — старт сервера лёгкий.
Проект определяется по cwd клиента (или явным параметром project = имя графа).

Конфиг: GRAFIT_HOST / GRAFIT_PORT (по умолч. localhost:6399), GRAFIT_THREADS (4).
"""
from __future__ import annotations
import os
from . import common, dupes, nav, search

HOST = os.environ.get("GRAFIT_HOST", "localhost")
PORT = int(os.environ.get("GRAFIT_PORT", "6399"))
THREADS = int(os.environ.get("GRAFIT_THREADS", "4"))

_embedder = None  # ленивый кэш модели


def _embed(question: str):
    global _embedder
    cfg = common.load_config()
    if not cfg:
        raise RuntimeError("grafit не сконфигурирован — выполни `grafit load` хотя бы в одном проекте")
    if _embedder is None:
        _embedder = search.get_embedder(cfg, threads=THREADS)
    return search.embed_query(_embedder, cfg, question), cfg


def _graph(project: str | None):
    name = common.graph_name(project or None)
    return common.connect(HOST, PORT).select_graph(name), name


def _fresh(name: str, project: str | None) -> str:
    """Шапка свежести. Без явного project сравниваем с git-деревом cwd клиента."""
    live = None if project else common.project_root()
    return common.freshness_line(name, live_root=live)


_LEGEND = "  легенда: →исходящее · ←входящее · ─структурное (AST) · ⋯производное (by-naming)"


def _alt_lines(r: dict) -> list[str]:
    """Строки с альтернативными кандидатами, когда символ неоднозначен (#1 disambiguation)."""
    alts = r.get("alternatives") or []
    if not r.get("ambiguous") or not alts:
        return []
    items = "; ".join(f"{a['label']} ({a['ft']}, {a['sf']})" for a in alts)
    return [f"  ещё кандидаты: {items}  — уточни project/точное имя/путь"]


def _nb(direction, rel, mlabel, msf) -> str:
    """Соседняя связь с направлением и уверенностью: out '─rel→', in '←rel─';
    структурное (AST) ─/→ vs производное (by-naming/семантика) ⋯ + '(inferred)'."""
    struct = common.relation_kind(rel) == "structural"
    body = "─" if struct else "⋯"
    edge = f"{body}{rel}→" if direction == "out" else f"←{rel}{body}"
    tail = "" if struct else " (inferred)"
    return f"  {edge} {mlabel} ({msf}){tail}"


def _root(name: str) -> str:
    """Корень проекта на диске для графа (из meta.json; иначе cwd)."""
    m = common.load_meta().get(name) or {}
    return m.get("root") or str(common.project_root())


def _quote(root, sf, loc, cache) -> list[str]:
    """Подтверждённая цитата: реальные строки файла у source_location (свежесть — в шапке)."""
    snip = common.read_snippet(root, sf, loc, window=3, max_chars=240, cache=cache)
    return [f"    │ {ln}" for ln in snip.splitlines()] if snip else []


try:
    from mcp.server.fastmcp import FastMCP
except Exception as ex:  # pragma: no cover
    raise SystemExit("Не установлен MCP SDK. Поставь: pip install 'grafit-mcp[mcp]' или mcp") from ex

_INSTRUCTIONS = """\
grafit — граф знаний кодовой базы с СЕМАНТИЧЕСКИМ поиском (эмбеддинги multilingual-e5).
По графу на проект; проект определяется из cwd клиента (или параметром project).

Когда звать grafit вместо grep/glob/текстового поиска:
• Вопросы «где/как реализовано X», «что отвечает за Y», «найди обработчик/валидацию/экран
  для …». grafit_search находит по СМЫСЛУ — даже если твои слова не совпадают дословно с
  именами символов, комментариями или строками в коде. Устойчив к синонимам, перефразировке,
  смешению RU/EN и опечаткам — там, где grep по точному токену даёт ноль. Для концептуальных
  вопросов и когда точные имена заранее НЕ известны это, как правило, НАДЁЖНЕЕ текстового поиска.
• Индексируются и UI-надписи фронтенда (JSX-текст, placeholder/aria-label/title) — можно найти
  компонент по подписи на экране.
• grep по-прежнему лучше для точного совпадения уже известной строки/идентификатора.
  Инструменты дополняют друг друга: начни с grafit_search по смыслу, при нужде добей grep'ом
  по найденным именам.

Перед рефакторингом/удалением символа — grafit_impact (кто его использует) и grafit_tests
(какие тесты заденет): это машинный обход графа, он ловит НЕПРЯМЫХ потребителей, которых grep
по имени пропускает. grafit_trace — поток вперёд (endpoint→handler→service), grafit_find_path —
связь между двумя сущностями, grafit_explain — узел и его связи.

Для рефакторинга дублей: grafit_similar(symbol) — что в проекте дублирует ВОТ ЭТУ функцию/
компонент (код→код по вектору, перед написанием новой или выносом общего кода); grafit_dupes —
глобальный скан копипаста по проекту (кластеры near-duplicate символов).

Свежесть: каждый ответ начинается шапкой свежести. Если видишь «⚠ … перезалей `grafit load`» —
граф отстал от кода: делай поправку на это и предложи перезалить.
"""

mcp = FastMCP("grafit", instructions=_INSTRUCTIONS)


@mcp.tool()
def grafit_search(question: str, k: int = 8, project: str = "", neighbors: int = 4,
                  hybrid: bool = False, rerank: bool = False, kind: str = "all",
                  snippet: bool = False) -> str:
    """СЕМАНТИЧЕСКИЙ поиск по коду — находит релевантное по СМЫСЛУ (эмбеддинги multilingual-e5),
    а не по точному совпадению текста. Предпочитай его grep/текстовому поиску для вопросов
    «где/как реализовано…», «что отвечает за…», когда точные имена заранее не известны: устойчив
    к синонимам, перефразировке, смешению RU/EN и опечаткам (там, где grep по токену даёт ноль —
    обычно надёжнее). Индексирует и UI-надписи фронтенда (JSX-текст, placeholder/aria-label) —
    можно найти экран по подписи. Возвращает узлы с цитатами path:line и соседями по графу.
    Если ищешь уже известную точную строку/идентификатор — быстрее grep; их можно сочетать.

    question: вопрос на естественном языке (RU/EN), формулируй по смыслу, не угадывай имена.
    project:  имя графа проекта (по умолчанию определяется из текущей папки).
    kind:     фильтр узлов — all|code|tests|docs|prod|frontend|backend (prod = код без
              тестов/миграций/генерёнки; frontend/backend = код одного слоя по пути — полезно,
              когда общий UI-запрос поднимает backend-узлы выше нужного компонента).
    snippet:  подмешать реальные строки исходника у каждого узла (source-first, экономит чтение).
    hybrid/rerank: опц. лексика+RRF / кросс-энкодер (по бенчмаркам прироста не дают)."""
    qvec, _ = _embed(question)
    g, name = _graph(project or None)
    reranker = search.get_reranker(threads=THREADS)[0] if rerank else None
    rows = search.search(g, qvec, question, k=k, hybrid=hybrid, reranker=reranker, kind=kind)
    if not rows:
        return f"{_fresh(name, project or None)}\n[{name}] ничего не найдено (залит ли проект? `grafit load`)"
    root = _root(name) if snippet else None
    fcache: dict = {}
    out = [_fresh(name, project or None), f"[{name}] {question}"]
    any_nb = False
    for nid, label, ft, sf, loc, clabel, text, score in rows:
        tag = " [test]" if common.is_test_path(sf) else ""
        out.append(f"\n● {label} ({ft}){tag}\n  {sf}:{loc}  | community: {clabel}")
        if snippet:
            out.extend(_quote(root, sf, loc, fcache))
        for d, rel, mlabel, msf in search.neighbors(g, nid, neighbors):
            any_nb = True
            out.append("  " + _nb(d, rel, mlabel, msf))
    if any_nb:
        out.append(_LEGEND)
    return "\n".join(out)


@mcp.tool()
def grafit_list_projects() -> str:
    """Список графов проектов, залитых в общий FalkorDB grafit."""
    reg = common.load_registry()
    gs = sorted(common.list_graphs(HOST, PORT))
    if not gs:
        return "нет загруженных проектов"
    return "\n".join(f"• {g}  {reg.get(g, '')}" for g in gs)


@mcp.tool()
def grafit_explain(symbol: str, project: str = "", neighbors: int = 10, snippet: bool = True) -> str:
    """Объяснить узел (класс/функция/концепт) по ТОЧНОМУ имени и его связи в графе.
    Если имя неизвестно или ищешь по смыслу — сначала grafit_search, потом explain по найденному.

    snippet: показать реальные строки исходника у узла (по умолчанию да)."""
    g, name = _graph(project or None)
    fresh = _fresh(name, project or None)
    r = nav.resolve_node(g, symbol)
    if not r:
        return f"{fresh}\n[{name}] узел '{symbol}' не найден"
    match = "точное совпадение" if r["match"] == "exact" else "по подстроке"
    hdr = f"[{name}] {r['label']} ({r['ft']}) — {match}"
    if r["n_candidates"] > 1:
        hdr += f" · {r['n_candidates']} опред." + (" ⚠ неоднозначно" if r["ambiguous"] else "")
    elif r.get("fragments"):
        hdr += f" · +{r['fragments']} reference-узлов"
    out = [fresh, f"{hdr}\n  {r['sf']}:{r['loc']}"]
    out.extend(_alt_lines(r))
    if snippet:
        out.extend(_quote(_root(name), r["sf"], r["loc"], {}))
    nbs = search.neighbors(g, r["id"], neighbors)
    for d, rel, mlabel, msf in nbs:
        out.append(_nb(d, rel, mlabel, msf))
    # file-level imports: для функции/компонента показать, что импортирует её файл
    # (imports висят на файл-узле, не на символе) — помечаем как производные «via file».
    fimp = nav.file_imports(g, r["sf"], exclude_id=r["id"]) if r["ft"] == "code" else []
    for fname, rel, mlabel, msf in fimp:
        out.append(f"  ⋯{rel}→ {mlabel} ({msf}) (via {fname})")
    if nbs or fimp:
        out.append(_LEGEND)
    return "\n".join(out)


@mcp.tool()
def grafit_find_path(source: str, target: str, project: str = "", max_hops: int = 6) -> str:
    """Кратчайший путь в графе между двумя сущностями (по именам)."""
    g, name = _graph(project or None)
    fresh = _fresh(name, project or None)
    a, b = nav.resolve_node(g, source), nav.resolve_node(g, target)
    if not a or not b:
        return f"{fresh}\n[{name}] не найдено: {source if not a else target}"
    path = nav.find_route(g, a, b, max_hops=int(max_hops))
    if not path:
        out = [fresh, f"[{name}] путь {source} → {target} не найден (≤{max_hops} шагов, по направлению рёбер)"]
        hint = nav.related_hint(g, a["id"], a["label"])
        if hint:
            out.append(f"прямого пути нет; связано с {a['label']}:")
            out.extend(hint)
        return "\n".join(out)
    return f"{fresh}\n{nav.render_path(name, path)}"


@mcp.tool()
def grafit_status(project: str = "") -> str:
    """Свежесть графа: на каком git-коммите построен, насколько отстал от HEAD, грязно ли
    дерево. Зови перед тем, как доверять результатам поиска по большому/давнему проекту."""
    name = common.graph_name(project or None)
    live = None if project else common.project_root()
    return common.freshness_report(name, live_root=live)


@mcp.tool()
def grafit_tests(symbol: str, project: str = "", max_hops: int = 2) -> str:
    """Тесты, связанные с символом (функцией/классом/endpoint). Зови ПЕРЕД изменением —
    показывает, какие тесты затрагивает правка. Обход графа, без эмбеддингов."""
    g, name = _graph(project or None)
    return "\n".join([_fresh(name, project or None)]
                     + nav.format_tests(g, name, symbol, int(max_hops)))


@mcp.tool()
def grafit_impact(symbol: str, project: str = "", max_hops: int = 2) -> str:
    """Impact-анализ: что сломается при изменении символа. Идёт по ВХОДЯЩИМ зависимостям
    (кто использует X) и группирует по категориям: tests/frontend/backend/contract/docs.
    Дешёвый гейт ПЕРЕД удалением/переносом символа — ловит непрямых потребителей, которых
    grep по имени пропускает. Обход графа, без эмбеддингов (точно)."""
    g, name = _graph(project or None)
    return "\n".join([_fresh(name, project or None)]
                     + nav.format_impact(g, name, symbol, int(max_hops)))


@mcp.tool()
def grafit_similar(symbol: str, project: str = "", k: int = 6, threshold: float = 0.10,
                   kind: str = "all", rerank: bool = False, snippet: bool = True) -> str:
    """Near-duplicate символы для ОДНОГО символа — кандидаты на рефакторинг (общий хелпер/
    extract-method). Сравнивает эмбеддинг узла (имя+тело+doc) с остальными по косинусу. В отличие
    от grafit_search (запрос→код) это код→код: «что в проекте дублирует ВОТ ЭТУ функцию/компонент».
    Отсекает шум: сам узел, generic/framework-методы (.Handle()/.Dispose()), символы из ТОГО ЖЕ
    файла (co-location ≠ дубль) и графово-связанные (Command→Handler). Зови перед тем, как писать
    новую функцию (нет ли уже такой) или вынося общий код.

    threshold: максимальная косинусная дистанция (0=идентично; меньше — строже; дефолт 0.10).
    kind:      фильтр узлов all|code|tests|docs|prod|frontend|backend.
    rerank:    добить кросс-энкодером (отодвигает структурно-похожие, но разные по смыслу)."""
    g, name = _graph(project or None)
    reranker = search.get_reranker(threads=THREADS)[0] if rerank else None
    root = _root(name) if snippet else None
    return "\n".join([_fresh(name, project or None)]
                     + dupes.format_similar(g, name, symbol, k=k, threshold=threshold,
                                            kind=kind, reranker=reranker, root=root))


@mcp.tool()
def grafit_dupes(project: str = "", kind: str = "prod", threshold: float = 0.06,
                 limit: int = 20, pairs: bool = False, include_framework: bool = False,
                 rerank: bool = False, snippet: bool = False) -> str:
    """Глобальный скан дубликатов кода по всему проекту — кандидаты на рефакторинг. Для каждого
    узла берёт ближайших по вектору, копит МЕЖФАЙЛОВЫЕ не-связанные пары (dist≤threshold).
    Дороже grafit_similar (скан всего графа) — зови для аудита/«где у нас копипаст».

    Каждая находка помечена ДВУМЯ сигналами: dist (косинус, семантика) и Jaccard шинглов —
    `копипаст` (буквально тот же текст, сливать почти наверняка) vs `семантика` (похожая логика,
    другой текст — решать). Кластеры с одним именем метода и членом-интерфейсом помечаются
    `реализации контракта → extract base/template method`. Сортировка по «выгоде» (размер×тесность).

    kind:               по умолчанию prod (код без тестов/миграций/генерёнки).
    threshold:          максимальная косинусная дистанция пары (дефолт 0.06; гистограмма в выводе).
    pairs:              плоский список ПАР вместо кластеров (не раздувается транзитивно; «что с чем»).
    include_framework:  включить шаблонные методы (.Handle()/…) — обычно шум, по умолч. выкл.
    rerank:             кросс-энкодером отсеять структурно-похожие, но разные по смыслу пары.
    snippet:            показать строки исходника у каждого члена (только для кластеров)."""
    g, name = _graph(project or None)
    reranker = search.get_reranker(threads=THREADS)[0] if rerank else None
    root = _root(name) if snippet else None
    fmt = dupes.format_pairs if pairs else dupes.format_duplicates
    return "\n".join([_fresh(name, project or None)]
                     + fmt(g, name, kind=kind, threshold=threshold, limit=limit,
                           include_framework=include_framework, reranker=reranker, root=root))


@mcp.tool()
def grafit_trace(source: str, project: str = "", max_hops: int = 4, target: str = "",
                 with_references: bool = False) -> str:
    """Проследить поток ВПЕРЁД от символа (endpoint → handler → service → …). Без target —
    дерево достижимости по calls/method/contains; с target — кратчайший путь source→target.
    with_references подмешивает шумные references-рёбра."""
    g, name = _graph(project or None)
    return "\n".join([_fresh(name, project or None)]
                     + nav.format_trace(g, name, source, int(max_hops), target, with_references))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
