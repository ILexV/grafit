"""Ядро поиска grafit: вектор + лексика (full-text) → взвешенный RRF → фильтр/штраф → реранкер.

Строка результата: 0=id 1=label 2=file_type 3=source_file 4=source_location
5=community_label 6=text 7=score (косинусная дистанция вектора; None — только лексика).
"""
from __future__ import annotations
import os, re
from . import common

_RERANK_PREF = [
    "jinaai/jina-reranker-v2-base-multilingual",
    "BAAI/bge-reranker-v2-m3",
    "Xenova/ms-marco-MiniLM-L-6-v2",
]

_COLS = ("node.id, node.label, node.file_type, node.source_file, "
         "node.source_location, node.community_label, node.text, score")
_VEC_Q = (f"CALL db.idx.vector.queryNodes('Entity','embedding',$cand, vecf32($q)) "
          f"YIELD node, score RETURN {_COLS}")
_LEX_Q = (f"CALL db.idx.fulltext.queryNodes('Entity', $q) "
          f"YIELD node, score RETURN {_COLS}")


def get_embedder(cfg, threads=4):
    # Общий сервис эмбеддингов (один контейнер на всех), иначе локальный fastembed.
    return common.make_embedder(cfg["model"], threads=threads)


def embed_query(model, cfg, question):
    t = ("query: " + question) if cfg["is_e5"] else question
    return next(iter(model.embed([t]))).tolist()


def get_reranker(threads=4):
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        sup = {m["model"] for m in TextCrossEncoder.list_supported_models()}
    except Exception:
        return None, None
    name = next((n for n in _RERANK_PREF if n in sup), None) or next(iter(sup), None)
    if not name:
        return None, None
    return TextCrossEncoder(model_name=name, threads=threads), name


def _lexical(graph, question, cand):
    """Full-text (RediSearch) по label+text; OR по токенам. [] если индекса нет."""
    terms = [t for t in re.findall(r"\w+", question.lower(), re.UNICODE) if len(t) >= 3]
    if not terms:
        return []
    q = " | ".join(dict.fromkeys(terms))
    try:
        return graph.query(_LEX_Q, params={"q": q}).result_set[:cand]
    except Exception:
        return []  # индекс не создан / синтаксис — деградируем до чистого вектора


# Пороги косинусной ДИСТАНЦИИ (FalkorDB отдаёт distance = 1 - cos, меньше — ближе) для
# multilingual-e5-large: релевантное ~0.13–0.17, запросы «ни о чём» (рецепт борща в HR-коде,
# погода, набор букв) ~0.21–0.25. e5 сжимает шкалу, поэтому пороги узкие. Переопределяются env.
STRONG_DIST = float(os.environ.get("GRAFIT_STRONG_DIST", "0.175"))
NONE_DIST = float(os.environ.get("GRAFIT_NONE_DIST", "0.20"))
# «Сильное» требует ещё и ОТРЫВА лучшего от фона (медиана дистанций ближайших BG_POOL узлов
# того же kind): e5 сжимает шкалу, и случайный узел (Props, VALID_TABS) легко набирает 0.83.
# Калибровка на bi/hr/bpm (2026-10-12, 34 запроса): верный топ — отрыв 0.014–0.052, случайный
# топ (Props, VALID_TABS, From) — 0.008–0.0128; «ни о чём» отсекает абсолютный порог.
STRONG_GAP = float(os.environ.get("GRAFIT_STRONG_GAP", "0.013"))
BG_POOL = 40


# Минимальный пул векторного индекса: HNSW при малом k теряет верных соседей (см. dupes.POOL).
VEC_POOL = 240


def match_quality(dist, bg=None) -> str:
    """strong | weak | none — насколько лучший результат похож на запрос (по дистанции).
    bg — фон (медиана дистанций соседей): без отрыва от него совпадение не «сильное»."""
    if dist is None:
        return "weak"
    if dist <= STRONG_DIST and (bg is None or bg - dist >= STRONG_GAP):
        return "strong"
    return "weak" if dist < NONE_DIST else "none"


def background(dists) -> float | None:
    """Фон выдачи: медиана дистанций ближайших BG_POOL кандидатов (None — мало данных)."""
    ds = sorted(d for d in dists if d is not None)[:BG_POOL]
    if len(ds) < 10:
        return None
    m = len(ds) // 2
    return ds[m] if len(ds) % 2 else (ds[m - 1] + ds[m]) / 2


def search(graph, qvec, question, **kw):
    """Строки результата (см. search_ex) — совместимый вход для CLI/eval."""
    return search_ex(graph, qvec, question, **kw)["rows"]


def search_ex(graph, qvec, question, k=8, cand=60, test_penalty=0.5,
              drop_generic=True, reranker=None, hybrid=False, rrf_c=60, lex_weight=0.4,
              kind="all", dedupe=True, hide_fragments=True, collapse=True, _vcand=None) -> dict:
    """Поиск с фильтром по виду узлов, дедупом и скрытием reference-фрагментов.

    Фильтр kind отсёк всё → честно пустой rows (+ filtered_out — сколько кандидатов было
    до фильтра), а НЕ подмена неотфильтрованной выдачей. Дедуп: один узел на sf:loc (graphify
    кладёт file/class/reference-узлы на одну строку). В колонке score — косинусная дистанция
    вектора (None у узла, найденного только лексикой).
    collapse: файл-узел уступает место самому конкретному узлу того же файла (класс/метод), а из
    пары интерфейс↔реализация (тип или одноимённый член) остаётся реализация — contracts[id] =
    (label, sf) свёрнутого контракта.
    → {"rows", "filtered_out", "candidates", "best_dist", "bg_dist", "contracts"}"""
    # при фильтре/дедупе берём больше кандидатов — иначе они «съедят» top-k
    if _vcand:
        vcand = _vcand
    elif kind != "all":
        vcand = max(cand * 4, k * 30)   # docs/тесты/.scratch часто занимают большую часть топа
    elif dedupe or hide_fragments:
        vcand = max(cand, k * 8, VEC_POOL)
    else:
        vcand = cand
    vec_rows = graph.query(_VEC_Q, params={"cand": vcand, "q": qvec}).result_set
    lex_rows = _lexical(graph, question, vcand) if hybrid else []
    empty = {"rows": [], "filtered_out": 0, "candidates": 0, "best_dist": None, "bg_dist": None,
             "contracts": {}, "copies": {}}
    if not vec_rows and not lex_rows:
        return empty

    by_id, vdist = {}, {}
    for r in vec_rows:
        by_id.setdefault(r[0], r)
        vdist.setdefault(r[0], r[7])
    for r in lex_rows:
        by_id.setdefault(r[0], r)

    ids = [nid for nid in by_id
           if not (drop_generic and common.is_generic(by_id[nid][1]))
           and common.kind_matches(by_id[nid][3], by_id[nid][2], kind)]
    widen = kind != "all" and not _vcand and len(vec_rows) >= vcand
    if not ids and not widen:
        return {**empty, "filtered_out": len(by_id), "candidates": len(by_id)}
    idset = set(ids)

    # Взвешенный Reciprocal Rank Fusion: вектор — основной (1.0), лексика — вспом. (lex_weight).
    rrf = {}
    for ranking, w in (([r[0] for r in vec_rows if r[0] in idset], 1.0),
                       ([r[0] for r in lex_rows if r[0] in idset], lex_weight)):
        for i, nid in enumerate(ranking):
            rrf[nid] = rrf.get(nid, 0.0) + w / (rrf_c + i)

    def demote(nid, s):
        return s * (1 - test_penalty) if common.is_test_path(by_id[nid][3]) else s

    order = sorted(idset, key=lambda nid: demote(nid, rrf.get(nid, 0.0)), reverse=True)

    if hide_fragments or dedupe:
        order = _prune(graph, order, by_id, max(k * 4, BG_POOL), dedupe, hide_fragments)
    # фон — по очищенному списку: дубли одной строки и фрагменты тянули бы медиану к топу
    bg = background(vdist.get(nid) for nid in order)
    contracts: dict = {}
    copies: dict = {}
    if collapse:
        order = _collapse_files(order, by_id)
        order, contracts = _collapse_contracts(graph, order[:k * 3], by_id)
        order, copies = _collapse_copies(order, by_id)

    if len(order) < k and widen:
        # фильтр съел почти весь пул — один повторный проход с пулом вчетверо шире
        return search_ex(graph, qvec, question, k=k, cand=cand, test_penalty=test_penalty,
                         drop_generic=drop_generic, reranker=reranker, hybrid=hybrid, rrf_c=rrf_c,
                         lex_weight=lex_weight, kind=kind, dedupe=dedupe,
                         hide_fragments=hide_fragments, collapse=collapse, _vcand=vcand * 4)
    if reranker is not None and order:
        pool = order[:max(k * 4, 30)]
        scores = list(reranker.rerank(question, [(by_id[nid][6] or by_id[nid][1]) for nid in pool]))
        lo, hi = min(scores), max(scores)
        rng = (hi - lo) or 1.0
        order = [nid for nid, _ in sorted(
            zip(pool, scores),
            key=lambda ps: -((ps[1] - lo) / rng
                             - (test_penalty if common.is_test_path(by_id[ps[0]][3]) else 0.0)))]

    rows = [tuple(by_id[nid][:7]) + (vdist.get(nid),) for nid in order[:k]]
    dists = [d for d in (vdist.get(nid) for nid in order) if d is not None]
    return {"rows": rows, "filtered_out": len(by_id) - len(idset), "candidates": len(by_id),
            "best_dist": min(dists) if dists else None,
            "bg_dist": bg,
            "contracts": {nid: c for nid, c in contracts.items() if nid in set(order[:k])},
            "copies": {nid: c for nid, c in copies.items() if nid in set(order[:k])}}


def _is_file_node(r) -> bool:
    sf = r[3] or ""
    return bool(sf) and r[1] == sf.rsplit("/", 1)[-1]


def _collapse_files(order, by_id):
    """Файл-узел (label = имя файла) и узел из того же файла — один результат: на место файла
    встаёт первый (самый релевантный) конкретный узел этого файла. Файл без пары — остаётся."""
    out, used = [], set()
    for i, nid in enumerate(order):
        if nid in used:
            continue
        r = by_id[nid]
        if _is_file_node(r):
            twin = next((x for x in order[i + 1:] if x not in used
                         and by_id[x][3] == r[3] and not _is_file_node(by_id[x])), None)
            if twin:
                used.add(twin)
                nid = twin
        used.add(nid)
        out.append(nid)
    return out


def _collapse_copies(order, by_id):
    """Копии одной функции по разным файлам — одна строка (dupes.collapse_copies)."""
    from . import dupes
    return dupes.collapse_copies([(nid, by_id[nid][1], by_id[nid][3], by_id[nid][4], by_id[nid][6])
                                  for nid in order])


def _collapse_contracts(graph, order, by_id):
    """Интерфейс/база и реализация в одной выдаче — один результат: остаётся реализация (код,
    который делает работу) на месте лучшего из двух; контракт — пометкой. Типы — по рёбрам
    implements/inherits или конвенции IFoo↔Foo, члены — одноимённые с родственными владельцами."""
    from . import dupes
    if len(order) < 2:
        return order, {}
    own = dupes.owners(graph, order)
    typ = {nid: (own[nid] if nid in own else by_id[nid][1]) for nid in order
           if nid in own or (by_id[nid][2] == "code" and not by_id[nid][1].endswith(")")
                             and not _is_file_node(by_id[nid]))}
    if len(typ) < 2:
        return order, {}
    sup = dupes.supertypes(graph, list(typ.values()))

    def contract_of(a, b):          # b — контракт a? (a реализует/наследует b)
        ta, tb = dupes.type_name(typ[a]), dupes.type_name(typ[b])
        return tb in sup.get(ta, ()) or tb == "i" + ta

    def same_member(a, b):
        if (a in own) != (b in own):
            return False
        return a not in own or dupes.member_name(by_id[a][1]) == dupes.member_name(by_id[b][1])

    drop, notes, pos = set(), {}, {nid: i for i, nid in enumerate(order)}
    for a in typ:
        for b in typ:
            if a == b or a in drop or b in drop or not same_member(a, b):
                continue
            if contract_of(a, b):
                drop.add(b)
                notes[a] = (by_id[b][1], by_id[b][3])
                pos[a] = min(pos[a], pos[b])
    kept = sorted((nid for nid in order if nid not in drop), key=lambda n: pos[n])
    return kept, notes


def _prune(graph, order, by_id, need, dedupe, hide_fragments):
    """Оставить по одному узлу на sf:loc и выкинуть reference-фрагменты/стабы (символ упомянут
    в чужом файле, но определён не тут) — их цитата ведёт не туда. Набирает до need узлов."""
    from . import nav
    out, seen = [], {}
    pos = 0
    while pos < len(order) and len(out) < need:
        chunk = order[pos:pos + max(need * 2, 20)]
        pos += len(chunk)
        relmap = nav._rel_map(graph, chunk) if hide_fragments else {}
        for nid in chunk:
            r = by_id[nid]
            if hide_fragments and nav._is_fragment((nid, r[1], r[2], r[3], r[4]), relmap):
                continue
            key = (r[3], r[4])
            if dedupe and r[3] and r[4] and key in seen:
                i = seen[key]          # на одной строке файл-узел уступает конкретному узлу
                if _is_file_node(by_id[out[i]]) and not _is_file_node(r):
                    out[i] = nid
                continue
            seen[key] = len(out)
            out.append(nid)
    return out


def neighbors(graph, node_id, limit=6):
    """Соседи узла С НАПРАВЛЕНИЕМ: (direction 'out'|'in', rel, label, source_file).

    out = node→m (исходящее), in = m→node (входящее) — чтобы не путать «A imports B»
    с «B imports A». Слоты делятся между направлениями (исходящим приоритет).
    """
    def fetch(arrow):
        rs = graph.query(
            f"MATCH (n:Entity {{id:$id}}){arrow}(m:Entity) "
            f"RETURN DISTINCT r.relation, m.label, m.source_file LIMIT $lim",
            params={"id": node_id, "lim": limit}).result_set
        return rs

    out_rows = [("out", rel, lbl, sf) for rel, lbl, sf in fetch("-[r:LINK]->")]
    in_rows = [("in", rel, lbl, sf) for rel, lbl, sf in fetch("<-[r:LINK]-")]
    keep_out = limit if not in_rows else max(1, (limit + 1) // 2)
    res = out_rows[:keep_out]
    res += in_rows[: limit - len(res)]
    return res
