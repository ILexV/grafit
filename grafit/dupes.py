"""Поиск дубликатов кода по векторам узлов: near-duplicate символы для рефакторинга.

Идея: эмбеддинг узла = имя + сниппет тела + doc-комментарий, поэтому БЛИЗКИЕ вектора =
семантически похожий код (кандидаты на extract-method / общий хелпер). Используем тот же
векторный индекс, что и поиск (db.idx.vector.queryNodes), но подаём ему вектор СУЩЕСТВУЮЩЕГО
узла, а не текстового запроса. score индекса — косинусная ДИСТАНЦИЯ (0 = идентично).

Сырой «ближайший сосед» шумен: ~2/3 пар — это co-located символы из ОДНОГО файла
(Command/Handler/Validator делят сниппет) и графово-связанные узлы. Поэтому пары фильтруем:
  • self / generic-узлы (common.is_generic);
  • один и тот же source_file (co-location, не клон);
  • прямое ребро LINK между узлами (Command→Handler — связь, не дубль);
  • framework-шаблонные имена методов (.Handle()/.Dispose()/… одинаковы по контракту, не копипаст).
Опционально кросс-энкодер (reranker) добивает структурно-похожие, но разные по смыслу пары.

Два режима:
  similar(symbol)  — дубли ОДНОГО символа (node-to-node), для точечного рефакторинга;
  find_duplicates() — глобальный скан проекта → кластеры дублей (union-find по парам).
"""
from __future__ import annotations
from . import common, nav

# Столбцы узла-кандидата (порядок фиксирован — на него опираются форматтеры/фильтры).
_COLS = "node.id, node.label, node.file_type, node.source_file, node.source_location, node.text"
_NEIGHBORS_Q = (f"CALL db.idx.vector.queryNodes('Entity','embedding',$k, vecf32($q)) "
                f"YIELD node, score RETURN {_COLS}, score")

# Имена методов, навязанных контрактом фреймворка/рантайма: тела разных реализаций
# структурно идентичны НЕ из-за копипаста, а потому что так требует интерфейс. В глобальном
# скане они слипаются в один огромный псевдо-кластер и топят реальные находки. Сравнение по
# нормализованному «голому» имени (nav._norm: '.Handle()' == 'Handle'). Доменно-значимые методы
# (.HandleRequirementAsync(), .BuildExportDto()) намеренно НЕ в списке — их дубли реальны.
_FRAMEWORK_METHODS = {
    "handle", "handleasync", "invoke", "invokeasync", "dispose", "disposeasync",
    "tostring", "equals", "gethashcode", "configure", "configureservices",
    "onmodelcreating", "main", "buildtargetmodel",
}


def is_noise_label(label: str | None) -> bool:
    """Шумовое для дубль-поиска имя: generic-узел или framework-шаблонный метод.

    Эти узлы дают ложные «дубликаты» (одинаковы по форме, не по копипасту) — исключаются
    из глобального скана по умолчанию (флаг include_framework возвращает их)."""
    if common.is_generic(label):
        return True
    return nav._norm(label) in _FRAMEWORK_METHODS


# Не-исходные файлы: PackageReference из .csproj/.props, конфиги, скрипты, доки. Их узлы
# (имя пакета, ключ конфига) кучно «дублируются» между проектами — это не код для рефакторинга.
_NONSOURCE_EXT = (".csproj", ".props", ".sln", ".json", ".xml", ".yml", ".yaml",
                  ".md", ".txt", ".sh", ".env", ".config", ".lock")
# Расширения исходников: узел, чьё ИМЯ оканчивается на них, — это файл-узел, а не символ.
_SOURCE_EXT = tuple("." + e for e in (common._FRONT_EXT | common._BACK_EXT))


def is_symbol_node(label: str | None, sf: str | None) -> bool:
    """Узел — реальный СИМВОЛ (функция/класс/компонент), а не файл-узел и не запись конфига?

    Дубль-поиск осмыслен только над символами: файл-узлы (label 'Foo.cs') дублируют свой же
    символ, а узлы из .csproj/.json — это пакеты/ключи, не код. Оба режима фильтруют их."""
    if not label:
        return False
    s = (sf or "").lower()
    if s.endswith(_NONSOURCE_EXT):
        return False
    if label.lower().endswith(_SOURCE_EXT):          # 'BuildExportDto.cs' — файл-узел
        return False
    return True


# Суффиксы именной СЕМЬИ одного домена: XCommand/XCommandHandler/XCommandValidator/XQuery…
# связаны конвенцией (CQRS/MediatR/FluentValidation), а не копипастом — их близкие вектора
# не повод к рефакторингу. Прямого LINK-ребра Command→Handler в графе нет (MediatR), а файлы
# разные, поэтому co-location/linked-фильтры их не ловят — нужен отдельный предикат по имени.
_FAMILY_SUFFIXES = ("Handler", "Validator", "Command", "Query", "Endpoint",
                    "Response", "Request", "Result")


def is_family_pair(a_label: str | None, b_label: str | None) -> bool:
    """Пара — члены одной именной семьи (XCommand ↔ XCommandHandler/Validator/…)?

    ИДЕНТИЧНЫЕ имена в разных файлах НЕ считаем семьёй — это и есть искомый клон
    (.BuildExportDto ↔ .BuildExportDto). Отличие по ПРЕФИКСУ (UserDto ↔ OrgUserDto) тоже не
    семья — там разные стемы, возможен реальный дубль. Семья = длинное имя == короткое + суффикс."""
    a, b = nav._bare(a_label), nav._bare(b_label)
    if not a or not b or a == b:
        return False
    long, short = (a, b) if len(a) >= len(b) else (b, a)
    return any(long == short + suf for suf in _FAMILY_SUFFIXES)


def pair_key(a_id: str, b_id: str) -> tuple[str, str]:
    """Ненаправленный ключ пары (для дедупа A↔B == B↔A)."""
    return (a_id, b_id) if a_id <= b_id else (b_id, a_id)


def cluster_pairs(pairs: list[tuple]) -> list[set]:
    """Сгруппировать пары (a_id, b_id, dist, …) в кластеры связности (union-find).

    Транзитивно: если A~B и B~C, то {A,B,C} — один кластер дублирующегося кода. Чистая
    функция (без графа) — тестируется напрямую."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:        # path compression
            parent[x], x = root, parent[x]
        return root

    for a, b, *_ in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: dict = {}
    for a, b, *_ in pairs:
        groups.setdefault(find(a), set()).update((a, b))
    return list(groups.values())


# --- графовые примитивы -----------------------------------------------------------

def node_embedding(graph, node_id: str):
    """Вектор узла по id (или None). Косинусная дистанция к нему = близость кода."""
    rs = graph.query("MATCH (n:Entity {id:$id}) RETURN n.embedding",
                     params={"id": node_id}).result_set
    return list(rs[0][0]) if rs and rs[0][0] is not None else None


def nearest(graph, qvec, k: int):
    """top-k узлов по вектору: строки (id, label, ft, sf, loc, text, dist), ближние первыми."""
    return graph.query(_NEIGHBORS_Q, params={"k": int(k), "q": qvec}).result_set


def _linked_ids(graph, node_id: str) -> set:
    """id всех узлов с прямым ребром LINK к/от узла (любое направление) — это связи, не дубли."""
    rs = graph.query("MATCH (n:Entity {id:$id})-[:LINK]-(m:Entity) RETURN DISTINCT m.id",
                     params={"id": node_id}).result_set
    return {r[0] for r in rs}


def fragment_ids(graph, nodes) -> set:
    """id узлов-ФРАГМЕНТОВ среди nodes: per-use упоминание типа/символа (рёбра только
    references/imports) или стаб без sf/loc — это НЕ определение, а usage. Для дубль-поиска
    ложные «клоны»: тип/enum, используемый в N файлах, иначе слипается в фейк-кластер
    (AuditLogStatus ×12) и сшивает реальные находки транзитивно. Делегирует nav._is_fragment.
    nodes — любые кортежи с (id,label,ft,sf,loc) в первых 5 позициях. Один запрос на пачку."""
    relmap = nav._rel_map(graph, [n[0] for n in nodes])
    return {n[0] for n in nodes
            if nav._is_fragment((n[0], n[1], n[2], n[3], n[4]), relmap)}


# --- режим 1: дубли одного символа ------------------------------------------------

def similar(graph, symbol: str, k: int = 6, threshold: float = 0.10,
            kind: str = "all", reranker=None) -> dict | None:
    """Найти near-duplicate узлы для символа (node-to-node). Возвращает dict с резолвом и
    отфильтрованными кандидатами [(id,label,ft,sf,loc,text,dist)] или None, если символ не найден.

    Фильтрация: self, generic/framework-шум, ОДИН source_file (co-location), прямые LINK-связи,
    dist > threshold. reranker (опц.) пересортировывает оставшихся по кросс-энкодеру."""
    r = nav.resolve_node(graph, symbol)
    if not r:
        return None
    qvec = node_embedding(graph, r["id"])
    if qvec is None:
        return {"resolved": r, "candidates": []}
    linked = _linked_ids(graph, r["id"])
    rows = nearest(graph, qvec, max(k * 8, 40))
    frag = fragment_ids(graph, rows)               # usage-узлы типа/символа — не дубли
    out = []
    for nid, label, ft, sf, loc, text, dist in rows:
        if nid == r["id"] or nid in linked or nid in frag:
            continue
        if is_noise_label(label) or not is_symbol_node(label, sf):
            continue
        if sf and sf == r["sf"]:                    # co-location в одном файле — не клон
            continue
        if is_family_pair(r["label"], label):       # XCommand↔XCommandHandler — связь, не дубль
            continue
        if not common.kind_matches(sf, ft, kind):
            continue
        if dist > threshold:
            continue
        out.append((nid, label, ft, sf, loc, text, dist))
        if len(out) >= k * 3:
            break
    out.sort(key=lambda x: x[6])
    if reranker is not None and out:
        out = _rerank_pairs(reranker, r.get("text") or r["label"], out)
    return {"resolved": r, "candidates": out[:k]}


def _rerank_pairs(reranker, anchor_text, rows):
    """Пересортировать кандидатов по кросс-энкодеру (релевантность тела anchor↔кандидат).
    Кросс-энкодер чувствительнее к смыслу, чем косинус: отодвигает структурно-похожие, но
    функционально разные пары. Дистанция остаётся в строке как первичная метрика."""
    docs = [(rw[5] or rw[1]) for rw in rows]
    scores = list(reranker.rerank(anchor_text, docs))
    return [rw for rw, _ in sorted(zip(rows, scores), key=lambda ps: -ps[1])]


# --- режим 2: глобальный скан проекта ---------------------------------------------

def _scan_nodes(graph, kind: str, include_framework: bool):
    """Узлы-кандидаты с эмбеддингами под фильтром kind, без шумовых имён."""
    rs = graph.query(
        "MATCH (n:Entity) WHERE n.embedding IS NOT NULL "
        "RETURN n.id, n.label, n.file_type, n.source_file, n.source_location, n.embedding"
    ).result_set
    keep = []
    for nid, label, ft, sf, loc, emb in rs:
        if not common.kind_matches(sf, ft, kind):
            continue
        if not is_symbol_node(label, sf):
            continue
        if (is_noise_label if not include_framework else common.is_generic)(label):
            continue
        keep.append((nid, label, ft, sf, loc, emb))
    frag = fragment_ids(graph, keep)               # отсев usage-узлов (тип/enum used-everywhere)
    return [n for n in keep if n[0] not in frag]


def _all_linked(graph) -> set:
    """Множество ненаправленных ключей всех прямых LINK-пар графа (для отсева связей)."""
    rs = graph.query("MATCH (a:Entity)-[:LINK]->(b:Entity) RETURN a.id, b.id").result_set
    return {pair_key(a, b) for a, b in rs}


def find_duplicates(graph, kind: str = "prod", threshold: float = 0.06, topk: int = 4,
                    include_framework: bool = False, reranker=None) -> list[dict]:
    """Глобальный скан: кластеры near-duplicate символов по проекту (кандидаты на рефакторинг).

    Для каждого узла берём top-k соседей по вектору, копим МЕЖФАЙЛОВЫЕ не-связанные пары
    с dist ≤ threshold, кластеризуем (union-find). Возвращает список кластеров, отсортированных
    по плотности (минимальная внутренняя дистанция): [{members:[(id,label,sf,loc)], min_dist, ...}].
    """
    nodes = _scan_nodes(graph, kind, include_framework)
    linked = _all_linked(graph)
    meta = {n[0]: n for n in nodes}
    pairs: dict = {}                                # key -> dist (минимальная встреченная)
    for nid, label, ft, sf, loc, emb in nodes:
        for hid, hlabel, hft, hsf, hloc, htext, dist in nearest(graph, list(emb), topk + 1):
            if hid == nid or hid not in meta:
                continue
            if dist > threshold:
                continue
            if sf and hsf and sf == hsf:            # co-location — не клон
                continue
            if is_family_pair(label, hlabel):       # XCommand↔XCommandHandler — связь, не дубль
                continue
            key = pair_key(nid, hid)
            if key in linked:                       # прямая связь — не дубль
                continue
            if key not in pairs or dist < pairs[key]:
                pairs[key] = dist
    if reranker is not None and pairs:
        pairs = _rerank_filter(reranker, pairs, meta)
    pair_list = [(a, b, d) for (a, b), d in pairs.items()]
    clusters = []
    for ids in cluster_pairs(pair_list):
        dists = [d for a, b, d in pair_list if a in ids and b in ids]
        members = [(i, meta[i][1], meta[i][3], meta[i][4]) for i in ids if i in meta]
        members.sort(key=lambda m: (m[2], m[3]))
        clusters.append({"members": members, "min_dist": min(dists),
                         "max_dist": max(dists), "size": len(members)})
    clusters.sort(key=lambda c: (c["min_dist"], -c["size"]))
    return clusters


def _rerank_filter(reranker, pairs: dict, meta: dict, keep_ratio: float = 0.75) -> dict:
    """Отсеять структурно-похожие, но смыслово разные пары кросс-энкодером: держим верхние
    keep_ratio по кросс-score. Грубая, но дешёвая страховка против framework-шаблонного шума,
    проскочившего denylist. Тексты берём из узлов (node.text), фолбэк — label."""
    keys = list(pairs)
    a_texts = [meta[k[0]][1] for k in keys]         # label достаточно как «запрос»
    scores = []
    for k, at in zip(keys, a_texts):
        bt = meta[k[1]][1]
        s = next(iter(reranker.rerank(at, [bt])), 0.0)
        scores.append(s)
    order = sorted(range(len(keys)), key=lambda i: -scores[i])
    keep = set(order[:max(1, int(len(keys) * keep_ratio))])
    return {keys[i]: pairs[keys[i]] for i in keep}


# --- форматтеры (строко-строители; шапку свежести добавляет вызывающий) ------------

def _snip(root, sf, loc, cache):
    return [f"    │ {ln}" for ln in
            common.read_snippet(root, sf, loc, window=2, max_chars=200, cache=cache).splitlines()] \
        if root else []


def format_similar(graph, name: str, symbol: str, k: int = 6, threshold: float = 0.10,
                   kind: str = "all", reranker=None, root=None) -> list[str]:
    res = similar(graph, symbol, k=k, threshold=threshold, kind=kind, reranker=reranker)
    if res is None:
        return [f"[{name}] символ '{symbol}' не найден"]
    r = res["resolved"]
    out = [f"[{name}] дубли ~ {r['label']} ({r['ft']}, {r['sf']}:{r['loc']})  "
           f"порог dist≤{threshold}"]
    cands = res["candidates"]
    if not cands:
        out.append("  похожих узлов в пределах порога нет (это хорошо — дублей не видно)")
        return out
    cache: dict = {}
    for nid, label, ft, sf, loc, text, dist in cands:
        out.append(f"\n  ≈{dist:.4f}  {label} ({ft})\n          {sf}:{loc}")
        out.extend(_snip(root, sf, loc, cache))
    return out


def format_duplicates(graph, name: str, kind: str = "prod", threshold: float = 0.06,
                      topk: int = 4, limit: int = 20, include_framework: bool = False,
                      reranker=None, root=None) -> list[str]:
    clusters = find_duplicates(graph, kind=kind, threshold=threshold, topk=topk,
                               include_framework=include_framework, reranker=reranker)
    out = [f"[{name}] дубликаты (kind={kind}, dist≤{threshold}): "
           f"{len(clusters)} кластеров"]
    if not clusters:
        out.append("  near-duplicate символов в пределах порога не найдено")
        return out
    cache: dict = {}
    for c in clusters[:limit]:
        out.append(f"\n● кластер ×{c['size']}  (dist {c['min_dist']:.4f}–{c['max_dist']:.4f})")
        for nid, label, sf, loc in c["members"]:
            out.append(f"    {label}   {sf}:{loc}")
            out.extend(_snip(root, sf, loc, cache))
    if len(clusters) > limit:
        out.append(f"\n  (+{len(clusters) - limit} кластеров ещё — сузь порог или подними limit)")
    return out
