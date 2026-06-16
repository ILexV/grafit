"""Навигация по графу: резолв символа, направленный обход, кратчайший путь.

Общее ядро для grafit_tests / grafit_impact / grafit_trace (и резолвера explain/find_path).
Чистый граф — без эмбеддингов. Направление рёбер (проверено на реальном графе):
  contains/method/defines : контейнер → член (файл → символ, класс → метод)
  calls                   : caller → callee   (A вызывает B ⇒ A зависит от B)
  references/imports*     : потребитель → цель (A использует B)
  implements/inherits     : потомок → база
Поэтому «кто сломается при изменении X» = ВХОДЯЩИЕ в X (dependents);
«куда течёт поток из X» = ИСХОДЯЩИЕ из X.
"""
from __future__ import annotations
import re
from . import common

# Зависимостные рёбра: A -rel-> B означает «A зависит от B».
DEPENDENCY_RELS = {"calls", "references", "imports", "imports_from", "implements", "inherits"}
# Контейнер → член (определение/вложенность).
CONTAINMENT_RELS = {"contains", "method", "defines"}
# Следование потоку вперёд (references шумные — opt-in отдельно).
FLOW_RELS = {"calls", "method", "contains"}
# Выводные связи (по смыслу/LLM) — по умолчанию вне impact/trace.
SOFT_RELS = {"conceptually_related_to", "semantically_similar_to", "shares_data_with", "rationale_for"}

_FT_RANK = {"code": 0, "concept": 1, "rationale": 2, "document": 3}
_CONTRACT_RE = re.compile(r"(Dto|Contract|Request|Response|Command|Query|Payload)", re.I)


def _norm(s: str) -> str:
    """Нормализовать имя для сравнения: метод '.Foo()' и функция 'Foo()' == 'Foo'."""
    return (s or "").strip().lstrip(".").rstrip("()").lower()


def _candidates(graph, symbol: str, limit: int = 40):
    return graph.query(
        "MATCH (n:Entity) WHERE toLower(n.label) = toLower($s) OR n.label CONTAINS $s "
        "RETURN n.id, n.label, n.file_type, n.source_file, n.source_location LIMIT $lim",
        params={"s": symbol, "lim": limit}).result_set


def resolve_node(graph, symbol: str, limit: int = 40) -> dict | None:
    """Найти узел по имени, предпочитая ОПРЕДЕЛЕНИЕ: точное совпадение → не-тест → код.

    Сравнение нормализованное ('.Foo()' == 'Foo()' == 'Foo'), чтобы функция/метод не
    проигрывали одноимённой concept-доке. Возвращает dict (match, n_candidates, ambiguous).
    """
    rs = _candidates(graph, symbol, limit)
    if not rs:
        return None
    sn = _norm(symbol)

    def key(c):
        _id, label, ft, sf, _loc = c
        exact = 0 if _norm(label) == sn else 1
        test = 1 if common.is_test_path(sf) else 0
        return (exact, test, _FT_RANK.get(ft, 4), 0 if _loc else 1, len(sf or ""))

    nid, label, ft, sf, loc = sorted(rs, key=key)[0]
    exact = _norm(label) == sn
    n_exact_nontest = sum(1 for c in rs
                          if _norm(c[1]) == sn and not common.is_test_path(c[3]))
    return {"id": nid, "label": label, "ft": ft, "sf": sf, "loc": loc,
            "match": "exact" if exact else "substring",
            "n_candidates": len(rs), "ambiguous": n_exact_nontest > 1}


def definition_ids(graph, symbol: str) -> list[str]:
    """ID ВСЕХ узлов с точным (нормализованным) именем символа — не-тест первыми.

    Для impact/tests объединяем зависимости по всем одноимённым узлам: один символ часто
    фрагментирован на несколько узлов (тип/энум/функция в разных файлах, в т.ч. тест-скоуп),
    и полнота важнее «канонического» узла. Fallback на лучший резолв, если точных нет.
    """
    rs = _candidates(graph, symbol)
    sn = _norm(symbol)
    exact = [c for c in rs if _norm(c[1]) == sn]
    if exact:
        # не-тестовые первыми (для стабильности), но включаем все
        exact.sort(key=lambda c: 1 if common.is_test_path(c[3]) else 0)
        return [c[0] for c in exact]
    best = resolve_node(graph, symbol)
    return [best["id"]] if best else []


def _hop_query(direction: str) -> str:
    # in: предшественники (m -rel-> a); out: преемники (a -rel-> m). parent = узел из фронтира (a).
    if direction == "in":
        pat = "(m:Entity)-[r:LINK]->(a:Entity)"
    else:
        pat = "(a:Entity)-[r:LINK]->(m:Entity)"
    return (f"MATCH {pat} WHERE a.id IN $ids AND ($all OR r.relation IN $rels) "
            "RETURN a.id AS parent, r.relation AS rel, m.id, m.label, m.file_type, m.source_file")


def expand(graph, start_ids, direction: str = "out", rels=None,
           max_hops: int = 2, node_cap: int = 40):
    """Послойный BFS с фильтром relation и направлением. Возвращает (rows, truncated).

    rows: список {hop, parent, rel, id, label, file_type, source_file}; узлы дедуплицированы
    (BFS-дерево). truncated=True, если упёрлись в node_cap (не молчим — вызывающий печатает +N).
    direction: 'out' | 'in' | 'both'.
    """
    rels = list(rels or [])
    all_rels = not rels
    seen = set(start_ids)
    frontier = list(start_ids)
    out = []
    truncated = False
    dirs = ["in", "out"] if direction == "both" else [direction]
    for hop in range(1, int(max_hops) + 1):
        if not frontier:
            break
        nextf = []
        for d in dirs:
            rsq = graph.query(_hop_query(d),
                              params={"ids": frontier, "rels": rels, "all": all_rels}).result_set
            for parent, rel, nid, label, ft, sf in rsq:
                if nid in seen:
                    continue
                if len(out) >= node_cap:
                    truncated = True
                    break
                seen.add(nid)
                nextf.append(nid)
                out.append({"hop": hop, "parent": parent, "rel": rel, "id": nid,
                            "label": label, "file_type": ft, "source_file": sf})
            if truncated:
                break
        if truncated:
            break
        frontier = nextf
    return out, truncated


def shortest_path(graph, a_id: str, b_id: str, max_hops: int = 6):
    """Кратчайший направленный путь a→b или a←b (список label) или None.

    FalkorDB: shortestPath только в WITH/RETURN и только направленный — пробуем оба
    направления и берём короче.
    """
    h = int(max_hops)
    found = []
    for arrow in (f"-[:LINK*..{h}]->", f"<-[:LINK*..{h}]-"):
        rs = graph.query(
            f"MATCH (a:Entity {{id:$a}}), (b:Entity {{id:$b}}) "
            f"RETURN [n IN nodes(shortestPath((a){arrow}(b))) | n.label]",
            params={"a": a_id, "b": b_id}).result_set
        if rs and rs[0][0]:
            found.append(rs[0][0])
    return min(found, key=len) if found else None


def impact_category(label: str, sf: str, ft: str) -> str:
    """Категория зависимого узла для impact: tests|frontend|docs|contract|backend|other."""
    if common.is_test_path(sf):
        return "tests"
    s = sf or ""
    if s.startswith("frontend/") or s.endswith((".ts", ".tsx", ".vue", ".jsx")):
        return "frontend"
    if (ft or "") in ("document", "concept", "rationale"):
        return "docs"
    if _CONTRACT_RE.search(label or "") or _CONTRACT_RE.search(s):
        return "contract"
    if (ft or "") == "code":
        return "backend"
    return "other"


# --- форматтеры (чистые строко-строители; используют MCP и CLI; шапку свежести
#     добавляет вызывающий) ---

def resolved_hdr(name: str, verb: str, r: dict) -> str:
    amb = ""
    if r["n_candidates"] > 1:
        amb = f" · {r['n_candidates']} кандидат(ов)" + (" ⚠ неоднозначно" if r["ambiguous"] else "")
    return f"[{name}] {verb}: {r['label']} ({r['ft']}, {r['sf']}:{r['loc']}){amb}"


def _mark(rel: str):
    struct = common.relation_kind(rel) == "structural"
    return ("─" if struct else "⋯", "" if struct else " (inferred)")


def render_tree(root_id, root_label, rows, max_lines: int = 40) -> list[str]:
    children: dict = {}
    for x in rows:
        children.setdefault(x["parent"], []).append(x)
    lines = [f"  {root_label}"]

    def walk(nid, depth):
        for c in children.get(nid, []):
            if len(lines) >= max_lines + 1:
                return
            mk, inf = _mark(c["rel"])
            lines.append("  " * (depth + 1) + f"{mk} {c['rel']} → {c['label']}{inf}")
            walk(c["id"], depth + 1)

    walk(root_id, 0)
    return lines


def format_tests(graph, name: str, symbol: str, max_hops: int = 2) -> list[str]:
    r = resolve_node(graph, symbol)
    if not r:
        return [f"[{name}] символ '{symbol}' не найден"]
    ids = definition_ids(graph, symbol) or [r["id"]]
    rows, _ = expand(graph, ids, "both",
                     DEPENDENCY_RELS | CONTAINMENT_RELS, max_hops=max_hops)
    tests = [x for x in rows if common.is_test_path(x["source_file"])]
    out = [resolved_hdr(name, "tests", r)]
    if not tests:
        out.append(f"прямых тестовых связей ≤{max_hops} hops нет")
        return out
    byfile: dict = {}
    for t in sorted(tests, key=lambda x: x["hop"]):
        byfile.setdefault(t["source_file"], t)
    for sf, t in byfile.items():
        out.append(f"  ─ {t['rel']} ({t['hop']} hop)  {t['label']}  ({sf})")
    return out


def format_impact(graph, name: str, symbol: str, max_hops: int = 2) -> list[str]:
    r = resolve_node(graph, symbol)
    if not r:
        return [f"[{name}] символ '{symbol}' не найден"]
    ids = definition_ids(graph, symbol) or [r["id"]]
    deps, trunc = expand(graph, ids, "in", DEPENDENCY_RELS, max_hops=max_hops)
    defs, _ = expand(graph, ids, "in", CONTAINMENT_RELS, max_hops=1)
    out = [resolved_hdr(name, "impact", r)]
    if defs:
        out.append("определён в:  " + " · ".join(f"{d['label']} ({d['rel']})" for d in defs[:5]))
    if not deps:
        out.append(f"зависимостей ≤{max_hops} hops не найдено")
        return out
    groups: dict = {}
    for d in deps:
        groups.setdefault(impact_category(d["label"], d["source_file"], d["file_type"]), []).append(d)
    tail = "  (+обход усечён)" if trunc else ""
    out.append(f"зависят (≤{max_hops} hops, {len(deps)}){tail}:")
    for cat in ("tests", "frontend", "backend", "contract", "docs", "other"):
        items = groups.get(cat)
        if not items:
            continue
        shown = items[:10]
        extra = f"  (+{len(items) - len(shown)} ещё)" if len(items) > len(shown) else ""
        out.append(f"  {cat} ({len(items)}):  " + " · ".join(i["label"] for i in shown) + extra)
    return out


def format_trace(graph, name: str, source: str, max_hops: int = 4,
                 target: str = "", with_references: bool = False) -> list[str]:
    rs = resolve_node(graph, source)
    if not rs:
        return [f"[{name}] источник '{source}' не найден"]
    if target:
        rt = resolve_node(graph, target)
        if not rt:
            return [f"[{name}] цель '{target}' не найдена"]
        path = shortest_path(graph, rs["id"], rt["id"], max_hops=max_hops)
        if not path:
            return [f"[{name}] путь {rs['label']} → {rt['label']} не найден (≤{max_hops})"]
        return [f"[{name}] " + "  →  ".join(path)]
    rels = set(FLOW_RELS) | ({"references"} if with_references else set())
    rows, trunc = expand(graph, [rs["id"]], "out", rels, max_hops=max_hops)
    out = [f"[{name}] trace ↓ {rs['label']} ({'/'.join(sorted(rels))}, ≤{max_hops})"]
    if not rows:
        hint = (f"  (у '{source}' {rs['n_candidates']} одноимённых узлов — уточни символ/класс)"
                if rs["n_candidates"] > 1 else "  (исходящих flow-связей нет)")
        out.append(hint)
        return out
    out.extend(render_tree(rs["id"], rs["label"], rows))
    if trunc:
        out.append("  (+обход усечён — сузь символ или уменьши hops)")
    return out
