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


# Рёбра «только потребление»: узел, у которого ВСЕ рёбра такие, — reference-фрагмент
# (per-file дубликат символа), а не его определение.
_REF_ONLY_RELS = {"references", "imports", "imports_from"}


def _rel_map(graph, ids: list[str]) -> dict:
    """id → множество типов инцидентных рёбер (обе стороны). Один запрос на пачку."""
    if not ids:
        return {}
    rs = graph.query(
        "MATCH (n:Entity)-[r:LINK]-(m:Entity) WHERE n.id IN $ids "
        "RETURN n.id, collect(DISTINCT r.relation)", params={"ids": ids}).result_set
    return {row[0]: set(row[1]) for row in rs}


def _is_fragment(c, relmap: dict) -> bool:
    """reference-фрагмент или стаб одноимённого символа (НЕ его определение).

    Стаб — placeholder graphify без source_file и без loc. Фрагмент — узел, чьи рёбра
    исключительно references/imports* (символ упомянут в чужом файле, но определён не тут).
    """
    _id, _label, _ft, sf, loc = c
    if not sf and not loc:
        return True
    rels = relmap.get(_id, set())
    return bool(rels) and rels <= _REF_ONLY_RELS


def resolve_node(graph, symbol: str, limit: int = 40) -> dict | None:
    """Найти узел по имени, предпочитая ОПРЕДЕЛЕНИЕ: точное → не-фрагмент → не-тест → код.

    Сравнение нормализованное ('.Foo()' == 'Foo()' == 'Foo'). Одноимённые reference-узлы
    (per-file дубликаты, что graphify плодит на каждый файл-потребитель) и стабы НЕ считаются
    равноправными кандидатами с определением: они штрафуются в ранжировании и не раздувают
    n_candidates/ambiguous. Возвращает dict (match, n_candidates, fragments, ambiguous).
    """
    rs = _candidates(graph, symbol, limit)
    if not rs:
        return None
    sn = _norm(symbol)
    relmap = _rel_map(graph, [c[0] for c in rs])

    def key(c):
        _id, label, ft, sf, _loc = c
        return (0 if _norm(label) == sn else 1,
                1 if _is_fragment(c, relmap) else 0,
                1 if common.is_test_path(sf) else 0,
                _FT_RANK.get(ft, 4), 0 if _loc else 1, len(sf or ""))

    nid, label, ft, sf, loc = sorted(rs, key=key)[0]
    exact = _norm(label) == sn
    exact_all = [c for c in rs if _norm(c[1]) == sn]
    if exact_all:
        real = [c for c in exact_all if not _is_fragment(c, relmap)]
        frags = len(exact_all) - len(real)
        defs = [c for c in real if not common.is_test_path(c[3])]
        alternatives = [{"label": c[1], "ft": c[2], "sf": c[3]}
                        for c in defs if c[0] != nid and c[3]][:3]
        n_candidates = max(1, len(real))
        ambiguous = len(defs) > 1
    else:  # резолв по подстроке — сохраняем прежнее поведение
        frags = 0
        alternatives = [{"label": c[1], "ft": c[2], "sf": c[3]}
                        for c in rs if c[0] != nid and c[3]][:3]
        n_candidates = len(rs)
        ambiguous = len(rs) > 1
    return {"id": nid, "label": label, "ft": ft, "sf": sf, "loc": loc,
            "match": "exact" if exact else "substring",
            "n_candidates": n_candidates, "fragments": frags,
            "ambiguous": ambiguous, "alternatives": alternatives}


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
                            "label": label, "file_type": ft, "source_file": sf,
                            "direction": d})
            if truncated:
                break
        if truncated:
            break
        frontier = nextf
    return out, truncated


def shortest_path(graph, a_id: str, b_id: str, max_hops: int = 6):
    """Кратчайший направленный путь a→b или a←b. Возвращает (labels, rels) или None.

    rels[i] = {"rel": <тип ребра>, "bridge": False} связывает labels[i]→labels[i+1].
    FalkorDB: shortestPath только в WITH/RETURN и только направленный — пробуем оба
    направления и берём короче.
    """
    h = int(max_hops)
    best = None
    for arrow in (f"-[:LINK*..{h}]->", f"<-[:LINK*..{h}]-"):
        sp = f"shortestPath((a){arrow}(b))"
        rs = graph.query(
            f"MATCH (a:Entity {{id:$a}}), (b:Entity {{id:$b}}) "
            f"RETURN [n IN nodes({sp}) | n.label], [r IN relationships({sp}) | r.relation]",
            params={"a": a_id, "b": b_id}).result_set
        if rs and rs[0][0]:
            labels = rs[0][0]
            rels = [{"rel": r, "bridge": False} for r in (rs[0][1] or [])]
            if best is None or len(labels) < len(best[0]):
                best = (labels, rels)
    return best


# --- convention-деривация (Tier 4): рёбра по конвенциям имён, которых нет в графе.
#     Делается на query-time (стор не засоряем); помечается '(by naming)' / inferred.
#     Узлы существуют, но graphify не связал их (DI/MediatR/тест-naming/интерфейс↔impl).

def _bare(label: str) -> str:
    """Имя без ведущих точек и хвостовых скобок, регистр сохранён ('.Foo()' -> 'Foo')."""
    return (label or "").strip().lstrip(".").rstrip("()")


def _conv_names(label: str) -> list[tuple[str, str]]:
    """Кандидаты (relation, target_label) по конвенциям имён для символа label."""
    base = _bare(label)
    if not base:
        return []
    out = [("tested_by", base + "Tests"), ("tested_by", base + "Test")]
    if base.endswith(("Command", "Query")):
        out.append(("handled_by", base + "Handler"))      # LoginCommand -> LoginCommandHandler
    if base.endswith("Handler"):
        out.append(("handles", base[:-len("Handler")]))   # LoginCommandHandler -> LoginCommand
    if len(base) > 1 and base[0] == "I" and base[1].isupper():
        out.append(("impl_of", base[1:]))                  # IJwtTokenService -> JwtTokenService
    else:
        out.append(("impl_of", "I" + base))                # JwtTokenService -> IJwtTokenService
    return out


def convention_links(graph, label: str) -> list[dict]:
    """Существующие узлы, связанные с label по конвенции имён (точное имя, без LLM/graphify)."""
    cands = _conv_names(label)
    if not cands:
        return []
    names = list({t for _, t in cands})
    rs = graph.query(
        "MATCH (n:Entity) WHERE n.label IN $names "
        "RETURN n.id, n.label, n.file_type, n.source_file",
        params={"names": names}).result_set
    bylabel: dict = {}
    for nid, lbl, ft, sf in rs:
        bylabel.setdefault(lbl, []).append({"id": nid, "label": lbl, "file_type": ft, "source_file": sf})
    out, seen = [], set()
    self_bare = _bare(label)
    for rel, tgt in cands:
        for n in bylabel.get(tgt, []):
            if _bare(n["label"]) == self_bare or n["id"] in seen:
                continue
            seen.add(n["id"])
            out.append({"rel": rel, **n})
    return out


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
        amb = f" · {r['n_candidates']} опред." + (" ⚠ неоднозначно" if r["ambiguous"] else "")
    elif r.get("fragments"):
        amb = f" · +{r['fragments']} reference-узлов"
    hdr = f"[{name}] {verb}: {r['label']} ({r['ft']}, {r['sf']}:{r['loc']}){amb}"
    alts = r.get("alternatives") or []
    if r.get("ambiguous") and alts:
        items = "; ".join(f"{a['label']} ({a['ft']}, {a['sf']})" for a in alts)
        hdr += f"\n  ещё кандидаты: {items}  — уточни project/точное имя/путь"
    return hdr


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
    byfile: dict = {}
    for t in sorted(tests, key=lambda x: x["hop"]):
        byfile.setdefault(t["source_file"], t)
    for sf, t in byfile.items():
        out.append(f"  ─ {t['rel']} ({t['hop']} hop)  {t['label']}  ({sf})")
    # convention: класс XTests, даже если ребра в графе нет
    conv = [c for c in convention_links(graph, r["label"])
            if c["rel"] == "tested_by" and c["source_file"] not in byfile]
    for c in conv:
        out.append(f"  ⋯ tested_by (by naming)  {c['label']}  ({c['source_file']})")
    if not tests and not conv:
        out.append(f"прямых тестовых связей ≤{max_hops} hops нет")
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
    if deps:
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
    # convention: ломаются по конвенции (handler у Command, impl у интерфейса, XTests),
    # даже если ребра в графе нет
    seen_ids = {d["id"] for d in deps}
    conv = [c for c in convention_links(graph, r["label"]) if c["id"] not in seen_ids]
    if conv:
        out.append("по конвенции (by naming):")
        for c in conv:
            out.append(f"  ⋯ {c['rel']}  {c['label']}  ({c['source_file']})")
    if not deps and not conv:
        out.append(f"зависимостей ≤{max_hops} hops не найдено")
    return out


# Конвенции, по которым можно ДОСТРОИТЬ маршрут, когда прямого ребра нет:
# interface↔impl (направление меняется на интерфейсе) и Command↔Handler (MediatR — реального
# ребра mediator.Send(LoginCommand)→LoginCommandHandler в графе нет).
_ROUTING_RELS = {"impl_of", "handles", "handled_by"}
# как назвать мост в выводе для направления, в котором его проходим
_BRIDGE_LABEL = {"impl_of": "impl_of", "handles": "handled_by", "handled_by": "handled_by"}


def _route_alts(graph, node: dict) -> list[tuple]:
    """Сам узел + его routing-алиасы по конвенции (id, label, bridge_rel|None)."""
    alts = [(node["id"], node["label"], None)]
    for c in convention_links(graph, node["label"]):
        if c["rel"] in _ROUTING_RELS:
            alts.append((c["id"], c["label"], _BRIDGE_LABEL[c["rel"]]))
    return alts


def _file_node(graph, sf: str):
    """Узел-файл для символа (его source_file). Import-рёбра живут на файле, не на символе."""
    if not sf:
        return None
    base = sf.rsplit("/", 1)[-1]
    rs = graph.query(
        "MATCH (f:Entity {source_file:$sf}) WHERE f.label = $base RETURN f.id, f.label LIMIT 1",
        params={"sf": sf, "base": base}).result_set
    return (rs[0][0], rs[0][1]) if rs else None


def _endpoint_alts(graph, node: dict) -> list[tuple]:
    """routing-алиасы + узел-файл символа (для файл-уровневых рёбер вроде imports)."""
    alts = _route_alts(graph, node)
    fn = _file_node(graph, node.get("sf"))
    if fn and fn[0] != node["id"] and all(fn[0] != a[0] for a in alts):
        alts.append((fn[0], fn[1], "in_file"))
    return alts


def _instances(graph, label: str) -> list[str]:
    """ID всех узлов того же логического символа (нормализованное имя), включая
    file-scoped reference-фрагменты. _bare убирает скобки/точки для CONTAINS-поиска."""
    ln = _norm(label)
    return [c[0] for c in _candidates(graph, _bare(label)) if _norm(c[1]) == ln]


def _instance_path(graph, a: dict, b: dict, max_hops: int = 6):
    """Кратчайший РЕАЛЬНЫЙ направленный путь между любыми инстансами символов a и b.

    Резолвер целит в каноническое определение, но реальное ребро (references/imports/…)
    часто идёт к/от file-scoped фрагмента того же имени. Кросс-произведение инстансов
    считается на сервере (shortestPath по спискам id). Рёбра реальные → bridge=False,
    путь чистый directed (не составной). Возвращает (labels, rels) или None.
    """
    aids, bids = _instances(graph, a["label"]), _instances(graph, b["label"])
    if not aids or not bids:
        return None
    h = int(max_hops)
    best = None
    for arrow in (f"-[:LINK*..{h}]->", f"<-[:LINK*..{h}]-"):
        sp = f"shortestPath((a){arrow}(b))"
        rs = graph.query(
            f"MATCH (a:Entity), (b:Entity) WHERE a.id IN $aids AND b.id IN $bids AND a.id <> b.id "
            f"WITH {sp} AS p WHERE p IS NOT NULL "
            f"RETURN [n IN nodes(p) | n.label], [r IN relationships(p) | r.relation] "
            f"ORDER BY length(p) LIMIT 1",
            params={"aids": aids, "bids": bids}).result_set
        if rs and rs[0][0]:
            labels = rs[0][0]
            rels = [{"rel": r, "bridge": False} for r in (rs[0][1] or [])]
            if best is None or len(labels) < len(best[0]):
                best = (labels, rels)
    return best


def bridged_path(graph, a: dict, b: dict, max_hops: int = 6):
    """Путь a→b; если прямого нет — мостит концы через convention-алиасы (impl_of/handled_by).

    Закрывает кейсы, где ребро не извлечено или меняет направление:
      Handler→IFoo, но Foo→implements→IFoo  → мост impl_of на конце-impl;
      AuthController→LoginCommand, но Command→Handler ребра нет → мост handled_by на конце-handler.
    Возвращает (path_labels|None, bridge_label|None). bridge_label — типы достроенных переходов.
    """
    direct = shortest_path(graph, a["id"], b["id"], max_hops)
    if direct:
        return direct
    a_alts, b_alts = _endpoint_alts(graph, a), _endpoint_alts(graph, b)
    # чистый convention-хоп: алиас одного конца — это сам другой конец (Command↔Handler рядом)
    for aid, _l, arel in a_alts:
        if arel and aid == b["id"]:
            return [a["label"], b["label"]], [{"rel": arel, "bridge": True}]
    for bid, _l, brel in b_alts:
        if brel and bid == a["id"]:
            return [a["label"], b["label"]], [{"rel": brel, "bridge": True}]
    # односторонний мост: ровно один конец заменяем алиасом (иначе получается зигзаг)
    best = None  # (labels, rels)
    for aid, _l, arel in a_alts:
        for bid, _l2, brel in b_alts:
            if (arel is None) == (brel is None):
                continue  # 0 или 2 алиаса — пропускаем
            p = shortest_path(graph, aid, bid, max_hops)
            if not p:
                continue
            plabels, prels = p
            if arel:
                labels = [a["label"]] + plabels
                rels = [{"rel": arel, "bridge": True}] + prels
            else:
                labels = plabels + [b["label"]]
                rels = prels + [{"rel": brel, "bridge": True}]
            if best is None or len(labels) < len(best[0]):
                best = (labels, rels)
    if best:
        return best
    # instance-aware: реальное направленное ребро к/от file-scoped инстансу того же символа
    # (чище, чем compose: directed, без пометки «составной»). До compose, после конвенций.
    return _instance_path(graph, a, b, max_hops)


def _logical_neighbors(graph, label: str, fanout: int = 300):
    """Соседи ЛОГИЧЕСКОГО символа label: реальные рёбра ОТ ВСЕХ узлов с этой меткой
    (обе стороны — схлопывает фрагменты graphify в один символ) + convention-рёбра.

    Возвращает [(neighbor_label, rel, bridge_bool)]. Generic-хабы и semantic/soft-рёбра
    отфильтрованы, иначе BFS уходит через 'string'/'Task' и conceptually_related_to.
    Запрос по n.label (не по id) — именно это объединяет canonical-определение и
    per-file reference-узлы одного имени.
    """
    rs = graph.query(
        "MATCH (n:Entity) WHERE n.label = $L MATCH (n)-[r:LINK]-(m:Entity) "
        "WHERE m.label <> $L RETURN DISTINCT r.relation, m.label LIMIT $lim",
        params={"L": label, "lim": fanout}).result_set
    out = [(mlabel, rel, False) for rel, mlabel in rs
           if rel not in SOFT_RELS and not common.is_generic(mlabel)]
    for c in convention_links(graph, label):
        if c["rel"] in _ROUTING_RELS:
            out.append((c["label"], _BRIDGE_LABEL[c["rel"]], True))
    return out


def compose_path(graph, a: dict, b: dict, max_hops: int = 6, label_cap: int = 2000):
    """Tier-3 fallback: собрать путь a→b, трактуя одноимённые узлы как ОДИН логический
    символ (схлопывание фрагментов). Двунаправленный BFS по нормализованным меткам через
    реальные рёбра (обе стороны, по всем инстансам метки) + convention-рёбра.

    Включается только когда прямой и одномостовой пути не нашлись: длинные backend-flows
    (Controller→Command→Handler→Service) рвутся на стыке, где символ фрагментирован на
    canonical-определение и per-file reference-узлы. Двунаправленный обход (встреча
    посередине) держит латентность в узде на плотном графе. Слабее по направлению —
    переходы помечаются composed (направление приблизительное). Возвращает (labels, rels)|None.
    """
    start_n, goal_n = _norm(a["label"]), _norm(b["label"])
    if start_n == goal_n:
        return None
    # parent_f[x]=(prev,label,rel,bridge): ребро prev─rel→x; parent_b[x]: ребро x─rel→prev
    # (prev ближе к цели). Оба в forward-ориентации (start→goal) для единой сборки.
    parent_f = {start_n: (None, a["label"], None, False)}
    parent_b = {goal_n: (None, b["label"], None, False)}
    front_f = [(start_n, a["label"])]
    front_b = [(goal_n, b["label"])]
    visited = 0
    for _ in range((int(max_hops) + 1) // 2):
        for front, parent, other in ((front_f, parent_f, parent_b),
                                     (front_b, parent_b, parent_f)):
            nextf = []
            for cur_n, cur_label in front:
                visited += 1
                if visited > label_cap:
                    return None
                for nlabel, rel, bridge in _logical_neighbors(graph, cur_label):
                    nn = _norm(nlabel)
                    if nn in parent:
                        continue
                    parent[nn] = (cur_n, nlabel, rel, bridge)
                    if nn in other:
                        return _compose_join(parent_f, parent_b, nn)
                    nextf.append((nn, nlabel))
            front[:] = nextf
        if not front_f and not front_b:
            break
    return None


def _compose_join(parent_f, parent_b, meet):
    """Сшить forward-половину (start→meet) и backward-половину (meet→goal) в (labels, rels)."""
    chain = []
    cur = meet
    while cur is not None:                       # meet → start
        prev_n, label, rel, bridge = parent_f[cur]
        chain.append((label, rel, bridge))
        cur = prev_n
    chain.reverse()                              # start → meet
    labels = [c[0] for c in chain]
    rels = [{"rel": c[1], "bridge": c[2], "composed": True} for c in chain[1:]]
    cur = meet
    while parent_b[cur][0] is not None:          # meet → goal
        prev_n, _lbl, rel, bridge = parent_b[cur]
        rels.append({"rel": rel, "bridge": bridge, "composed": True})
        labels.append(parent_b[prev_n][1])
        cur = prev_n
    return labels, rels


def find_route(graph, a: dict, b: dict, max_hops: int = 6):
    """Лучший путь a→b: прямой → одномостовой → составной (canonical fallback)."""
    return bridged_path(graph, a, b, max_hops) or compose_path(graph, a, b, max_hops)


def render_path(name: str, path) -> str:
    """Однострочный путь с типом каждого перехода: ─rel→ структурное ребро,
    ⋯rel→ производный переход (by-naming мост, in_file или составной canonical-хоп)."""
    labels, rels = path
    s = labels[0]
    for i, r in enumerate(rels):
        mark = "⋯" if r.get("bridge") else "─"
        s += f"  {mark}{r['rel']}→  {labels[i + 1]}"
    derived = list(dict.fromkeys(r["rel"] for r in rels if r.get("bridge")))
    suffix = f"   (⋯ производные: {', '.join(derived)})" if derived else ""
    if any(r.get("composed") for r in rels):
        suffix += "   (составной путь — направление приблизительное)"
    return f"[{name}] {s}{suffix}"


def related_hint(graph, node_id: str, label: str, limit: int = 8) -> list[str]:
    """Fallback (#6): связанные символы, когда прямого пути/потока нет — конвенции +
    ближайшие references/imports в обе стороны. Чтобы ответ не был просто «не найдено»."""
    lines = []
    for c in convention_links(graph, label)[:limit]:
        lines.append(f"  ⋯{c['rel']}→ {c['label']}  ({c['source_file']})  (by naming)")
    rows, _ = expand(graph, [node_id], "both",
                     {"references", "imports", "imports_from", "calls"}, max_hops=1, node_cap=limit)
    for x in rows[:limit]:
        edge = f"─{x['rel']}→" if x["direction"] == "out" else f"←{x['rel']}─"
        lines.append(f"  {edge} {x['label']}  ({x['source_file']})")
    return lines


def format_trace(graph, name: str, source: str, max_hops: int = 4,
                 target: str = "", with_references: bool = False) -> list[str]:
    rs = resolve_node(graph, source)
    if not rs:
        return [f"[{name}] источник '{source}' не найден"]
    if target:
        rt = resolve_node(graph, target)
        if not rt:
            return [f"[{name}] цель '{target}' не найдена"]
        path = find_route(graph, rs, rt, max_hops=max_hops)
        if not path:
            out = [f"[{name}] путь {rs['label']} → {rt['label']} не найден (≤{max_hops})"]
            hint = related_hint(graph, rs["id"], rs["label"])
            if hint:
                out.append("прямого пути нет; связано с источником:")
                out.extend(hint)
            return out
        return [render_path(name, path)]
    rels = set(FLOW_RELS) | ({"references"} if with_references else set())
    rows, trunc = expand(graph, [rs["id"]], "out", rels, max_hops=max_hops)
    out = [f"[{name}] trace ↓ {rs['label']} ({'/'.join(sorted(rels))}, ≤{max_hops})"]
    if not rows:
        if rs["n_candidates"] > 1:
            out.append(f"  (у '{source}' {rs['n_candidates']} одноимённых узлов — уточни символ/класс)")
        else:
            out.append("  (исходящих flow-связей нет)")
        hint = related_hint(graph, rs["id"], rs["label"])
        if hint:
            out.append("связано (не прямой flow):")
            out.extend(hint)
        return out
    out.extend(render_tree(rs["id"], rs["label"], rows))
    if trunc:
        out.append("  (+обход усечён — сузь символ или уменьши hops)")
    return out
