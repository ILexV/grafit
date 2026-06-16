"""Eval-харнесс: recall@5/@10, MRR@10 по золотому набору; сравнение конфигураций.

Совпадение: ожидаемая подстрока входит в source_file ИЛИ label (без регистра).
Золотой набор — JSON [{"q": "...", "expect": ["substr", ...]}, ...].
"""
from __future__ import annotations
import json
from pathlib import Path
from . import common, search

DEFAULT_GOLDEN = Path(__file__).resolve().parent / "data" / "golden.example.json"


def _matches(row, expects):
    sf = (row[3] or "").lower(); lbl = (row[1] or "").lower()
    return any(e in sf or e in lbl for e in expects)


def _evaluate(graph, embed_fn, gold, opts):
    rec5 = rec10 = mrr = 0
    misses = []
    for item in gold:
        q, exp = item["q"], [e.lower() for e in item["expect"]]
        rows = search.search(graph, embed_fn(q), q, k=10, **opts)
        ranks = [i + 1 for i, r in enumerate(rows) if _matches(r, exp)]
        if ranks:
            mrr += 1 / ranks[0]; rec10 += 1
            if ranks[0] <= 5:
                rec5 += 1
        else:
            misses.append(q)
    n = len(gold) or 1
    return rec5 / n, rec10 / n, mrr / n, misses


def run_eval(graph=None, golden=None, host="localhost", port=6399, threads=4):
    cfg = common.load_config()
    if not cfg:
        raise SystemExit("config нет — сначала `grafit load` хотя бы для одного проекта")
    gold = json.loads(Path(golden or DEFAULT_GOLDEN).read_text(encoding="utf-8"))
    name = common.graph_name(graph)
    g = common.connect(host, port).select_graph(name)

    embedder = search.get_embedder(cfg, threads=threads)
    qcache: dict = {}

    def embed_fn(q):
        if q not in qcache:
            qcache[q] = search.embed_query(embedder, cfg, q)
        return qcache[q]

    reranker, rname = search.get_reranker(threads=threads)
    configs = [
        ("чистый вектор", dict(hybrid=False, drop_generic=False, test_penalty=0.0, reranker=None)),
        ("+фильтр+штраф", dict(hybrid=False, drop_generic=True, test_penalty=0.5, reranker=None)),
        ("+гибрид RRF", dict(hybrid=True, drop_generic=True, test_penalty=0.5, reranker=None)),
        ("+реранк", dict(hybrid=True, drop_generic=True, test_penalty=0.5, reranker=reranker)),
    ]
    print(f"граф '{name}', золотых запросов: {len(gold)}, реранкер: {rname}")
    print(f"{'конфигурация':<18} {'recall@5':>9} {'recall@10':>10} {'MRR@10':>8}")
    last_misses = []
    for cname, opts in configs:
        r5, r10, mrr, misses = _evaluate(g, embed_fn, gold, opts)
        print(f"{cname:<18} {r5:>9.2f} {r10:>10.2f} {mrr:>8.3f}")
        last_misses = misses
    if last_misses:
        print("\nпромахи:")
        for q in last_misses:
            print(f"  ✗ {q}")
