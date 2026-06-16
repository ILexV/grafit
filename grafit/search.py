"""Ядро поиска grafit: вектор + лексика (full-text) → взвешенный RRF → фильтр/штраф → реранкер.

Строка результата: 0=id 1=label 2=file_type 3=source_file 4=source_location
5=community_label 6=text 7=score.
"""
from __future__ import annotations
import re
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


def search(graph, qvec, question, k=8, cand=60, test_penalty=0.5,
           drop_generic=True, reranker=None, hybrid=False, rrf_c=60, lex_weight=0.4):
    vec_rows = graph.query(_VEC_Q, params={"cand": cand, "q": qvec}).result_set
    lex_rows = _lexical(graph, question, cand) if hybrid else []
    if not vec_rows and not lex_rows:
        return []

    by_id = {}
    for r in vec_rows:
        by_id.setdefault(r[0], r)
    for r in lex_rows:
        by_id.setdefault(r[0], r)

    ids = [nid for nid in by_id if not (drop_generic and common.is_generic(by_id[nid][1]))]
    if not ids:
        ids = list(by_id)
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

    if reranker is not None:
        pool = order[:max(k * 4, 30)]
        scores = list(reranker.rerank(question, [(by_id[nid][6] or by_id[nid][1]) for nid in pool]))
        lo, hi = min(scores), max(scores)
        rng = (hi - lo) or 1.0
        order = [nid for nid, _ in sorted(
            zip(pool, scores),
            key=lambda ps: -((ps[1] - lo) / rng
                             - (test_penalty if common.is_test_path(by_id[ps[0]][3]) else 0.0)))]

    return [by_id[nid] for nid in order[:k]]


def neighbors(graph, node_id, limit=6):
    """Соседи узла по графу (для обогащения ответа: тест → реализация и т.п.)."""
    rs = graph.query(
        "MATCH (n:Entity {id:$id})-[r:LINK]-(m:Entity) "
        "RETURN DISTINCT r.relation, m.label, m.source_file LIMIT $lim",
        params={"id": node_id, "lim": limit},
    ).result_set
    return [(rel, lbl, sf) for rel, lbl, sf in rs]
