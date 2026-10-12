"""Бенчмарк антидублей: recall@k для find_existing и similar на эталоне data/dupes_bench.json.

Пункт эталона — существующий символ проекта (expect) и его повторная реализация моделью
(другое имя/форма). find_existing ищет по описанию (q — как пишет модель, q_en — с ключевыми
словами кода), similar — по телу, положенному во временный файл (проекты не трогаем).
Плюс доля ложных «сильное» на запросах «ни о чём» и точность «сильное» (лидер — ожидаемый).
Идёт через те же функции, что MCP (search_ex/similar_to_text), — меряется реальная выдача.
"""
from __future__ import annotations
import json, tempfile
from pathlib import Path
from . import common, dupes, nav, search

DEFAULT_BENCH = Path(__file__).resolve().parent / "data" / "dupes_bench.json"
KS = (1, 3, 8)


def load_bench(path=None) -> dict:
    return json.loads(Path(path or DEFAULT_BENCH).read_text(encoding="utf-8"))


def row_matches(label, sf, expect, members=()) -> bool:
    """Строка выдачи — ожидаемый символ? Совпадение по пути и «голому» имени; для свёрнутой
    строки копипаста (members: [(label, sf), …]) — по любому из её членов."""
    for lbl, s in [(label, sf)] + list(members):
        for e in expect:
            if s == e["sf"] and nav._bare(lbl or "") == e["name"]:
                return True
    return False


def first_rank(rows, expect) -> int | None:
    """1-based ранг первой ожидаемой строки (rows: (label, sf, members)) или None."""
    for i, (label, sf, members) in enumerate(rows, 1):
        if row_matches(label, sf, expect, members):
            return i
    return None


def recall(ranks, ks=KS) -> dict:
    n = len(ranks) or 1
    return {k: sum(1 for r in ranks if r is not None and r <= k) / n for k in ks}


class _Embed:
    def __init__(self, threads=4):
        self.cfg = common.load_config()
        if not self.cfg:
            raise SystemExit("grafit не сконфигурирован — `grafit load` хотя бы в одном проекте")
        self.model = search.get_embedder(self.cfg, threads=threads)

    def query(self, text):
        return search.embed_query(self.model, self.cfg, text)

    def passage(self, text):
        t = ("passage: " + text) if self.cfg["is_e5"] else text
        return next(iter(self.model.embed([t]))).tolist()


def _search_rows(res) -> list:
    copies = res.get("copies") or {}
    return [(r[1], r[3], [c[:2] for c in copies.get(r[0], ())]) for r in res["rows"]]


def run(path=None, graphs=None, host="localhost", port=6399, threads=4, log=print) -> dict:
    """Прогнать эталон → {"items": [...], "none": [...]} с рангами и вердиктами по пунктам."""
    bench = load_bench(path)
    emb = _Embed(threads)
    db = common.connect(host, port)
    have = set(common.list_graphs(host, port))
    want = set(graphs) if graphs else None
    out = {"items": [], "none": []}
    for it in bench["items"]:
        gname = it["graph"]
        if gname not in have or (want and gname not in want):
            continue
        g = db.select_graph(gname)
        missing = [e for e in it["expect"] if not _exists(g, e)]
        if len(missing) == len(it["expect"]):
            log(f"⚠ {it['id']}: ни одного ожидаемого узла нет в графе {gname} — пункт пропущен")
            continue
        rec = {"id": it["id"], "graph": gname}
        for key in ("q", "q_en"):
            res = search.search_ex(g, emb.query(it[key]), it[key], k=max(KS), kind="prod")
            rows = _search_rows(res)
            rec[key] = first_rank(rows, it["expect"])
            rec[key + "_strong"] = search.match_quality(res["best_dist"], res.get("bg_dist")) == "strong"
            rec[key + "_top_ok"] = bool(rows) and row_matches(*rows[0][:2], it["expect"], rows[0][2])
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / it["file"]
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(it["body"], encoding="utf-8")
            anchor = dupes.worktree_text(tmp, it["file"], it["symbol"])
            # similar по файлу: объявления извлекает decls (как grafit_similar(file=) и хук)
            fanchors, _ = dupes.file_anchors(tmp, it["file"], base=None)
        fa = next((a for a in fanchors if a["label"] == it["symbol"]), None)
        rec["similar"] = _similar_rank(g, emb, anchor, it["expect"])
        rec["similar_file"] = _similar_rank(g, emb, fa, it["expect"])
        if anchor is None:
            log(f"⚠ {it['id']}: символ {it['symbol']} не найден в теле")
        if fa is None:
            log(f"⚠ {it['id']}: decls не извлёк объявление {it['symbol']}")
        out["items"].append(rec)
    for gname in bench["none"]["graphs"]:
        if gname not in have or (want and gname not in want):
            continue
        g = db.select_graph(gname)
        for q in bench["none"]["q"]:
            res = search.search_ex(g, emb.query(q), q, k=max(KS), kind="prod")
            out["none"].append({"graph": gname, "q": q,
                                "quality": search.match_quality(res["best_dist"], res.get("bg_dist"))})
    return out


def _similar_rank(g, emb, anchor, expect):
    if anchor is None:
        return None
    sres = dupes.similar_to_text(g, emb.passage(anchor["text"]), anchor, k=max(KS))
    copies = sres.get("copies") or {}
    rows = [(c[1], c[3], [x[:2] for x in copies.get(c[0], ())])
            for c in sres["candidates"] + sres["near"]]
    return first_rank(rows, expect)


def _exists(g, e) -> bool:
    rs = g.query("MATCH (n:Entity) WHERE n.source_file = $sf RETURN n.label",
                 params={"sf": e["sf"]}).result_set
    return any(nav._bare(r[0] or "") == e["name"] for r in rs)


def summary(res: dict) -> dict:
    items = res["items"]
    s = {"n": len(items), "none_n": len(res["none"])}
    for key in ("q", "q_en", "similar", "similar_file"):
        s[key] = recall([it.get(key) for it in items])
    strong = [it for it in items if it.get("q_strong")]
    s["strong_n"] = len(strong)
    s["strong_precision"] = (sum(1 for it in strong if it["q_top_ok"]) / len(strong)) if strong else None
    s["strong_top3"] = (sum(1 for it in strong if it["q"] and it["q"] <= 3) / len(strong)) if strong else None
    s["none_strong"] = sum(1 for x in res["none"] if x["quality"] == "strong")
    s["none_said_none"] = sum(1 for x in res["none"] if x["quality"] == "none")
    return s


def format_table(res: dict) -> str:
    s = summary(res)
    pct = lambda v: "—" if v is None else f"{v * 100:.0f}%"
    lines = [f"эталон: {s['n']} пар, {s['none_n']} запросов «ни о чём»", "",
             "| инструмент | R@1 | R@3 | R@8 |", "|---|---|---|---|"]
    for key, title in (("q", "find_existing (q, по-русски)"),
                       ("q_en", "find_existing (q_en, + код по-англ.)"),
                       ("similar", "similar (тело, file=)"),
                       ("similar_file", "similar по файлу (decls)")):
        lines.append(f"| {title} | " + " | ".join(pct(s[key][k]) for k in KS) + " |")
    lines += ["", f"«сильное» на q: {s['strong_n']} из {s['n']}; из них аналог в топ-3 — "
              f"{pct(s['strong_top3'])}, первым — {pct(s['strong_precision'])}",
              f"«ни о чём»: ложных «сильное» {s['none_strong']} из {s['none_n']}, "
              f"«ничего похожего» {s['none_said_none']} из {s['none_n']}", "",
              "| пара | q | q_en | similar | по файлу |", "|---|---|---|---|---|"]
    fmt = lambda r: "—" if r is None else str(r)
    for it in res["items"]:
        lines.append(f"| {it['id']} | {fmt(it['q'])} | {fmt(it['q_en'])} | {fmt(it['similar'])} "
                     f"| {fmt(it.get('similar_file'))} |")
    return "\n".join(lines)
