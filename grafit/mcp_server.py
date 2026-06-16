"""MCP-сервер grafit: семантический поиск по коду из любого MCP-клиента (stdio).

Инструменты: grafit_search, grafit_list_projects, grafit_explain, grafit_find_path.
Эмбеддер грузится лениво (на первый запрос) и кэшируется — старт сервера лёгкий.
Проект определяется по cwd клиента (или явным параметром project = имя графа).

Конфиг: GRAFIT_HOST / GRAFIT_PORT (по умолч. localhost:6399), GRAFIT_THREADS (4).
"""
from __future__ import annotations
import os
from . import common, search

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


try:
    from mcp.server.fastmcp import FastMCP
except Exception as ex:  # pragma: no cover
    raise SystemExit("Не установлен MCP SDK. Поставь: pip install 'grafit-mcp[mcp]' или mcp") from ex

mcp = FastMCP("grafit")


@mcp.tool()
def grafit_search(question: str, k: int = 8, project: str = "", neighbors: int = 4,
                  hybrid: bool = False, rerank: bool = False) -> str:
    """Семантический поиск по кодовой базе проекта (граф знаний). Возвращает наиболее
    релевантные узлы с цитатами path:line и соседями по графу.

    question: вопрос на естественном языке (RU/EN).
    project:  имя графа проекта (по умолчанию определяется из текущей папки).
    hybrid/rerank: опц. лексика+RRF / кросс-энкодер (по бенчмаркам прироста не дают)."""
    qvec, _ = _embed(question)
    g, name = _graph(project or None)
    reranker = search.get_reranker(threads=THREADS)[0] if rerank else None
    rows = search.search(g, qvec, question, k=k, hybrid=hybrid, reranker=reranker)
    if not rows:
        return f"[{name}] ничего не найдено (залит ли проект? `grafit load`)"
    out = [f"[{name}] {question}"]
    for nid, label, ft, sf, loc, clabel, text, score in rows:
        tag = " [test]" if common.is_test_path(sf) else ""
        out.append(f"\n● {label} ({ft}){tag}\n  {sf}:{loc}  | community: {clabel}")
        for rel, mlabel, msf in search.neighbors(g, nid, neighbors):
            out.append(f"    ─ {rel} → {mlabel} ({msf})")
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
def grafit_explain(symbol: str, project: str = "", neighbors: int = 10) -> str:
    """Объяснить узел (класс/функция/концепт) по имени и его связи в графе."""
    g, name = _graph(project or None)
    rs = g.query(
        "MATCH (n:Entity) WHERE toLower(n.label) = toLower($s) OR n.label CONTAINS $s "
        "RETURN n.id, n.label, n.file_type, n.source_file, n.source_location LIMIT 1",
        params={"s": symbol}).result_set
    if not rs:
        return f"[{name}] узел '{symbol}' не найден"
    nid, label, ft, sf, loc = rs[0]
    out = [f"[{name}] {label} ({ft})\n  {sf}:{loc}"]
    for rel, mlabel, msf in search.neighbors(g, nid, neighbors):
        out.append(f"  ─ {rel} → {mlabel} ({msf})")
    return "\n".join(out)


@mcp.tool()
def grafit_find_path(source: str, target: str, project: str = "", max_hops: int = 6) -> str:
    """Кратчайший путь в графе между двумя сущностями (по именам)."""
    g, name = _graph(project or None)

    def resolve(s):
        rs = g.query("MATCH (n:Entity) WHERE toLower(n.label)=toLower($s) OR n.label CONTAINS $s "
                     "RETURN n.id LIMIT 1", params={"s": s}).result_set
        return rs[0][0] if rs else None

    a, b = resolve(source), resolve(target)
    if not a or not b:
        return f"[{name}] не найдено: {source if not a else target}"
    rs = g.query(
        f"MATCH (a:Entity {{id:$a}}), (b:Entity {{id:$b}}) "
        f"MATCH p = shortestPath((a)-[:LINK*..{int(max_hops)}]-(b)) "
        f"RETURN [n IN nodes(p) | n.label]",
        params={"a": a, "b": b}).result_set
    if not rs or not rs[0][0]:
        return f"[{name}] путь {source} → {target} не найден (≤{max_hops} шагов)"
    return f"[{name}] " + "  →  ".join(rs[0][0])


def main():
    mcp.run()


if __name__ == "__main__":
    main()
