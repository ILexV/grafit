"""MCP-сервер grafit: семантический поиск по коду из любого MCP-клиента (stdio).

Инструменты: grafit_search, grafit_list_projects, grafit_explain, grafit_find_path,
grafit_status, grafit_tests, grafit_impact, grafit_trace.
Эмбеддер грузится лениво (на первый запрос) и кэшируется — старт сервера лёгкий.
Проект определяется по cwd клиента (или явным параметром project = имя графа).

Конфиг: GRAFIT_HOST / GRAFIT_PORT (по умолч. localhost:6399), GRAFIT_THREADS (4).
"""
from __future__ import annotations
import os
from . import common, nav, search

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

mcp = FastMCP("grafit")


@mcp.tool()
def grafit_search(question: str, k: int = 8, project: str = "", neighbors: int = 4,
                  hybrid: bool = False, rerank: bool = False, kind: str = "all",
                  snippet: bool = False) -> str:
    """Семантический поиск по кодовой базе проекта (граф знаний). Возвращает наиболее
    релевантные узлы с цитатами path:line и соседями по графу.

    question: вопрос на естественном языке (RU/EN).
    project:  имя графа проекта (по умолчанию определяется из текущей папки).
    kind:     фильтр узлов — all|code|tests|docs|prod (prod = код без тестов/миграций/генерёнки).
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
    """Объяснить узел (класс/функция/концепт) по имени и его связи в графе.

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
    if nbs:
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
    (кто использует X) и группирует по категориям: tests/frontend/backend/contract/docs."""
    g, name = _graph(project or None)
    return "\n".join([_fresh(name, project or None)]
                     + nav.format_impact(g, name, symbol, int(max_hops)))


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
