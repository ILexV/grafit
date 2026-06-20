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
import re
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


def is_interface_member(label: str | None, sf: str | None) -> bool:
    """Узел — объявление в ИНТЕРФЕЙСЕ/контракте (а не реализация)? Для аннотации #4: кластер
    одинаковых методов, где есть член-интерфейс, — это реализации одного контракта, и подсказка
    «вынеси базовый/template-метод» точнее, чем «дубль». Эвристика по имени файла/символа:
    C#-конвенция `IFoo` (PascalCase с I-префиксом) или путь `/Interfaces/`."""
    base = (sf or "").rsplit("/", 1)[-1]
    if len(base) >= 2 and base[0] == "I" and base[1].isupper():
        return True
    if "/interfaces/" in (sf or "").lower():
        return True
    bl = nav._bare(label)
    return len(bl) >= 2 and bl[0] == "I" and bl[1].isupper()


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


# --- токенные шинглы: второй сигнал, ортогональный вектору -------------------------
# Косинус эмбеддинга ловит «делает похожее» (семантика), Jaccard k-грамм токенов — «буквально
# тот же текст» (копипаст). Их сочетание делит находки: высокий Jaccard = literal-copy (сливать
# в общий код почти наверняка), низкий Jaccard при близком векторе = семантический дубль
# (похожая логика, но другой текст — решать человеку). Источник текста — сниппет тела узла.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[^\sA-Za-z0-9_]")
LITERAL_JACCARD = 0.45        # порог «это буквальный копипаст», откалиброван на bpm/core_ledger


def shingles(text: str | None, k: int = 4) -> frozenset:
    """Множество k-грамм токенов исходника (для Jaccard-оценки буквального совпадения).
    Токен = идентификатор или одиночный знак пунктуации; k-грамма устойчивее к переименованию
    отдельных переменных, чем сравнение по словам. Чистая функция — тестируется напрямую."""
    toks = _TOKEN_RE.findall(text or "")
    if len(toks) < k:
        return frozenset([" ".join(toks)]) if toks else frozenset()
    return frozenset(" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    """|A∩B| / |A∪B| — доля общих шинглов (0..1). 0 при пустом любом из множеств."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


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


def node_text(graph, node_id: str) -> str:
    """Текст узла (имя+сниппет+doc) по id — для Jaccard-шинглов anchor-символа."""
    rs = graph.query("MATCH (n:Entity {id:$id}) RETURN n.text",
                     params={"id": node_id}).result_set
    return (rs[0][0] if rs and rs[0][0] else "") or ""


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
        out = _rerank_pairs(reranker, node_text(graph, r["id"]) or r["label"], out)
    out = out[:k]
    # Второй сигнал: Jaccard шинглов anchor↔кандидат — копипаст (высокий) vs семантика (низкий)
    ash = shingles(node_text(graph, r["id"]))
    out = [(*row, jaccard(ash, shingles(row[5]))) for row in out]
    return {"resolved": r, "candidates": out}


def _rerank_pairs(reranker, anchor_text, rows):
    """Пересортировать кандидатов по кросс-энкодеру (релевантность тела anchor↔кандидат).
    Кросс-энкодер чувствительнее к смыслу, чем косинус: отодвигает структурно-похожие, но
    функционально разные пары. Дистанция остаётся в строке как первичная метрика."""
    docs = [(rw[5] or rw[1]) for rw in rows]
    scores = list(reranker.rerank(anchor_text, docs))
    return [rw for rw, _ in sorted(zip(rows, scores), key=lambda ps: -ps[1])]


# --- режим 2: глобальный скан проекта ---------------------------------------------

def _scan_nodes(graph, kind: str, include_framework: bool):
    """Узлы-кандидаты под фильтром kind, без шумовых имён. Кортеж (id,label,ft,sf,loc,text,emb):
    text для Jaccard-шинглов, emb для векторного поиска."""
    rs = graph.query(
        "MATCH (n:Entity) WHERE n.embedding IS NOT NULL "
        "RETURN n.id, n.label, n.file_type, n.source_file, n.source_location, n.text, n.embedding"
    ).result_set
    keep = []
    for nid, label, ft, sf, loc, text, emb in rs:
        if not common.kind_matches(sf, ft, kind):
            continue
        if not is_symbol_node(label, sf):
            continue
        if (is_noise_label if not include_framework else common.is_generic)(label):
            continue
        keep.append((nid, label, ft, sf, loc, text, emb))
    frag = fragment_ids(graph, keep)               # отсев usage-узлов (тип/enum used-everywhere)
    return [n for n in keep if n[0] not in frag]


def _all_linked(graph) -> set:
    """Множество ненаправленных ключей всех прямых LINK-пар графа (для отсева связей)."""
    rs = graph.query("MATCH (a:Entity)-[:LINK]->(b:Entity) RETURN a.id, b.id").result_set
    return {pair_key(a, b) for a, b in rs}


def _collect_pairs(graph, kind: str, threshold: float, topk: int,
                   include_framework: bool, reranker):
    """Скан проекта → (meta, pair_info). pair_info[key] = (dist, jaccard): косинус-дистанция
    (семантика) и Jaccard шинглов (буквальность). Это общее ядро для кластеров и режима пар."""
    nodes = _scan_nodes(graph, kind, include_framework)
    linked = _all_linked(graph)
    meta = {n[0]: n for n in nodes}
    dist_of: dict = {}
    for nid, label, ft, sf, loc, text, emb in nodes:
        for hid, hlabel, hft, hsf, hloc, htext, dist in nearest(graph, list(emb), topk + 1):
            if hid == nid or hid not in meta or dist > threshold:
                continue
            if sf and hsf and sf == hsf:            # co-location — не клон
                continue
            if is_family_pair(label, hlabel):       # XCommand↔XCommandHandler — связь, не дубль
                continue
            key = pair_key(nid, hid)
            if key in linked:                       # прямая связь — не дубль
                continue
            if key not in dist_of or dist < dist_of[key]:
                dist_of[key] = dist
    if reranker is not None and dist_of:
        dist_of = _rerank_filter(reranker, dist_of, meta)
    shcache: dict = {}

    def sh(i):                                       # шинглы тела узла, кэш по id
        return shcache.setdefault(i, shingles(meta[i][5]))
    pair_info = {key: (d, jaccard(sh(key[0]), sh(key[1]))) for key, d in dist_of.items()}
    return meta, pair_info


def _cluster_score(size: int, min_dist: float, max_jaccard: float) -> float:
    """Эвристика «выгоды рефакторинга» для сортировки: больше членов × ближе × буквальнее —
    выше. Кластер из 22 идентичных методов важнее тесной пары из двух. Только для порядка."""
    return size * (1.0 + max_jaccard) / (min_dist + 0.005)


def find_duplicates(graph, kind: str = "prod", threshold: float = 0.06, topk: int = 4,
                    include_framework: bool = False, reranker=None) -> list[dict]:
    """Глобальный скан: кластеры near-duplicate символов по проекту (кандидаты на рефакторинг).

    Межфайловые не-связанные пары (dist ≤ threshold) кластеризуются (union-find). Кластер несёт:
    min/max_dist, max_jaccard (буквальность), literal (копипаст vs семантика), shared_name (один
    метод в N местах), contract (реализации интерфейса → extract base), score (порядок по выгоде).
    Сортировка — по score (крупные тесные буквальные первыми)."""
    meta, pair_info = _collect_pairs(graph, kind, threshold, topk, include_framework, reranker)
    pair_list = [(a, b, d) for (a, b), (d, j) in pair_info.items()]
    clusters = []
    for ids in cluster_pairs(pair_list):
        intra = [pair_info[k] for k in pair_info if k[0] in ids and k[1] in ids]
        dists = [d for d, j in intra]
        jaccs = [j for d, j in intra]
        members = [(i, meta[i][1], meta[i][3], meta[i][4]) for i in ids if i in meta]
        members.sort(key=lambda m: (m[2], m[3]))
        names = {nav._norm(m[1]) for m in members}
        max_j = max(jaccs) if jaccs else 0.0
        contract = (len(names) == 1
                    and any(is_interface_member(m[1], m[2]) for m in members))
        clusters.append({
            "members": members, "min_dist": min(dists), "max_dist": max(dists),
            "size": len(members), "max_jaccard": max_j,
            "literal": max_j >= LITERAL_JACCARD,
            "shared_name": next(iter(names)) if len(names) == 1 else None,
            "contract": contract,
            "score": _cluster_score(len(members), min(dists), max_j),
        })
    clusters.sort(key=lambda c: -c["score"])
    return clusters


def duplicate_pairs(graph, kind: str = "prod", threshold: float = 0.06, topk: int = 4,
                    include_framework: bool = False, reranker=None) -> list[dict]:
    """То же ядро, но плоский список ПАР (без транзитивной кластеризации — не раздувается).
    Пара: {a,b:(label,sf,loc), dist, jaccard, literal}. Сортировка — буквальные и тесные первыми."""
    meta, pair_info = _collect_pairs(graph, kind, threshold, topk, include_framework, reranker)
    out = []
    for (a, b), (d, j) in pair_info.items():
        out.append({"a": (meta[a][1], meta[a][3], meta[a][4]),
                    "b": (meta[b][1], meta[b][3], meta[b][4]),
                    "dist": d, "jaccard": j, "literal": j >= LITERAL_JACCARD,
                    "shared_name": nav._norm(meta[a][1]) == nav._norm(meta[b][1])})
    out.sort(key=lambda p: (-(p["jaccard"]), p["dist"]))
    return out


def dist_histogram(pairs_or_clusters, bucket: float = 0.02) -> list[tuple]:
    """Распределение дистанций по корзинам ширины bucket — для подсказки порога (#7).
    Принимает список пар (dict с 'dist') или кластеров (берёт min_dist). [(верх_корзины, кол-во)]."""
    hist: dict = {}
    for x in pairs_or_clusters:
        d = x.get("dist", x.get("min_dist", 0.0))
        b = round((int(d / bucket) + 1) * bucket, 4)
        hist[b] = hist.get(b, 0) + 1
    return sorted(hist.items())


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


def _tag(literal: bool, jacc: float) -> str:
    """Метка природы дубля по Jaccard: буквальный копипаст vs семантически похожий код."""
    return f"копипаст J={jacc:.2f}" if literal else f"семантика J={jacc:.2f}"


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
    for nid, label, ft, sf, loc, text, dist, jacc in cands:
        out.append(f"\n  ≈{dist:.4f}  [{_tag(jacc >= LITERAL_JACCARD, jacc)}]  {label} ({ft})"
                   f"\n          {sf}:{loc}")
        out.extend(_snip(root, sf, loc, cache))
    return out


def _hist_lines(items) -> list[str]:
    """Строки распределения дистанций (подсказка порога #7)."""
    hist = dist_histogram(items)
    if not hist:
        return []
    lines = ["  распределение dist (для подбора порога):"]
    for top, cnt in hist:
        lines.append(f"    ≤{top:.2f}: {cnt:4d}  {'█' * min(40, cnt)}")
    return lines


def format_duplicates(graph, name: str, kind: str = "prod", threshold: float = 0.06,
                      topk: int = 4, limit: int = 20, include_framework: bool = False,
                      reranker=None, root=None) -> list[str]:
    clusters = find_duplicates(graph, kind=kind, threshold=threshold, topk=topk,
                               include_framework=include_framework, reranker=reranker)
    lit = sum(1 for c in clusters if c["literal"])
    out = [f"[{name}] дубликаты (kind={kind}, dist≤{threshold}): {len(clusters)} кластеров "
           f"({lit} копипаст / {len(clusters) - lit} семантика), сортировка по выгоде"]
    if not clusters:
        out.append("  near-duplicate символов в пределах порога не найдено")
        return out
    out.extend(_hist_lines(clusters))
    cache: dict = {}
    for c in clusters[:limit]:
        tags = [_tag(c["literal"], c["max_jaccard"])]
        if c["shared_name"]:
            tags.append(f"один метод ×{c['size']}")
        if c["contract"]:
            tags.append("реализации контракта → extract base/template method")
        out.append(f"\n● ×{c['size']}  dist {c['min_dist']:.4f}–{c['max_dist']:.4f}  "
                   f"[{' · '.join(tags)}]")
        for nid, label, sf, loc in c["members"]:
            out.append(f"    {label}   {sf}:{loc}")
            out.extend(_snip(root, sf, loc, cache))
    if len(clusters) > limit:
        out.append(f"\n  (+{len(clusters) - limit} кластеров ещё — сузь порог или подними limit)")
    return out


def format_pairs(graph, name: str, kind: str = "prod", threshold: float = 0.06, topk: int = 4,
                 limit: int = 30, include_framework: bool = False, reranker=None,
                 root=None) -> list[str]:
    """Плоский список ПАР (не кластеры) — не раздувается транзитивно, точнее для «что с чем
    сливать». Буквальные (копипаст) первыми."""
    pairs = duplicate_pairs(graph, kind=kind, threshold=threshold, topk=topk,
                            include_framework=include_framework, reranker=reranker)
    lit = sum(1 for p in pairs if p["literal"])
    out = [f"[{name}] дубль-пары (kind={kind}, dist≤{threshold}): {len(pairs)} пар "
           f"({lit} копипаст / {len(pairs) - lit} семантика)"]
    if not pairs:
        out.append("  near-duplicate пар в пределах порога не найдено")
        return out
    out.extend(_hist_lines(pairs))
    for p in pairs[:limit]:
        (la, sfa, loca), (lb, sfb, locb) = p["a"], p["b"]
        out.append(f"\n  ≈{p['dist']:.4f}  [{_tag(p['literal'], p['jaccard'])}]  {la} ↔ {lb}")
        out.append(f"          {sfa}:{loca}")
        out.append(f"          {sfb}:{locb}")
    if len(pairs) > limit:
        out.append(f"\n  (+{len(pairs) - limit} пар ещё)")
    return out
