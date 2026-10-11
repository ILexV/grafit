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


def match_quality(dist) -> str:
    """strong | weak | none — насколько лучший результат похож на запрос (по дистанции)."""
    if dist is None:
        return "weak"
    if dist <= STRONG_DIST:
        return "strong"
    return "weak" if dist < NONE_DIST else "none"


def search(graph, qvec, question, **kw):
    """Строки результата (см. search_ex) — совместимый вход для CLI/eval."""
    return search_ex(graph, qvec, question, **kw)["rows"]


def search_ex(graph, qvec, question, k=8, cand=60, test_penalty=0.5,
              drop_generic=True, reranker=None, hybrid=False, rrf_c=60, lex_weight=0.4,
              kind="all", dedupe=True, hide_fragments=True, _vcand=None) -> dict:
    """Поиск с фильтром по виду узлов, дедупом и скрытием reference-фрагментов.

    Фильтр kind отсёк всё → честно пустой rows (+ filtered_out — сколько кандидатов было
    до фильтра), а НЕ подмена неотфильтрованной выдачей. Дедуп: один узел на sf:loc (graphify
    кладёт file/class/reference-узлы на одну строку). В колонке score — косинусная дистанция
    вектора (None у узла, найденного только лексикой).
    → {"rows", "filtered_out", "candidates", "best_dist"}"""
    # при фильтре/дедупе берём больше кандидатов — иначе они «съедят» top-k
    if _vcand:
        vcand = _vcand
    elif kind != "all":
        vcand = max(cand * 4, k * 30)   # docs/тесты/.scratch часто занимают большую часть топа
    elif dedupe or hide_fragments:
        vcand = max(cand, k * 8)
    else:
        vcand = cand
    vec_rows = graph.query(_VEC_Q, params={"cand": vcand, "q": qvec}).result_set
    lex_rows = _lexical(graph, question, vcand) if hybrid else []
    empty = {"rows": [], "filtered_out": 0, "candidates": 0, "best_dist": None}
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
        order = _prune(graph, order, by_id, k * 4, dedupe, hide_fragments)

    if len(order) < k and widen:
        # фильтр съел почти весь пул — один повторный проход с пулом вчетверо шире
        return search_ex(graph, qvec, question, k=k, cand=cand, test_penalty=test_penalty,
                         drop_generic=drop_generic, reranker=reranker, hybrid=hybrid, rrf_c=rrf_c,
                         lex_weight=lex_weight, kind=kind, dedupe=dedupe,
                         hide_fragments=hide_fragments, _vcand=vcand * 4)
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
            "best_dist": min(dists) if dists else None}


def _prune(graph, order, by_id, need, dedupe, hide_fragments):
    """Оставить по одному узлу на sf:loc и выкинуть reference-фрагменты/стабы (символ упомянут
    в чужом файле, но определён не тут) — их цитата ведёт не туда. Набирает до need узлов."""
    from . import nav
    out, seen = [], set()
    pos = 0
    while pos < len(order) and len(out) < need:
        chunk = order[pos:pos + max(need * 2, 20)]
        pos += len(chunk)
        relmap = nav._rel_map(graph, chunk) if hide_fragments else {}
        for nid in chunk:
            r = by_id[nid]
            key = (r[3], r[4])
            if dedupe and r[3] and r[4] and key in seen:
                continue
            if hide_fragments and nav._is_fragment((nid, r[1], r[2], r[3], r[4]), relmap):
                continue
            seen.add(key)
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
