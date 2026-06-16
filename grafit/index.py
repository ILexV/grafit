"""Индексация проекта: graphify graph.json → FalkorDB (вектор + full-text), эмбеддинги.

Идемпотентно, инкрементально (кэш эмбеддингов): перезаливка считает только
новые/изменённые узлы; при полном кэше модель в RAM не грузится.
"""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
from shutil import which

from . import common


def _have_graphify() -> bool:
    return which("graphify") is not None


def ensure_graph_json(project: Path, build: bool) -> Path:
    gj = project / "graphify-out" / "graph.json"
    if gj.exists():
        return gj
    if build and _have_graphify():
        print("graph.json нет — строю через graphify ...")
        subprocess.run(["graphify", "."], cwd=str(project), check=True)
        if gj.exists():
            return gj
    sys.exit(f"нет {gj}.\nПострой граф: `graphify .` в корне проекта "
             f"(или `grafit load --build`, если graphify установлен: uv tool install graphifyy).")


def index_project(project=None, graph=None, host="localhost", port=6399, model=None,
                  no_snippets=False, no_cache=False, threads=4, batch=16, build=False):
    root = common.project_root(Path(project) if project else None)
    name = common.graph_name(graph, root)
    gj = ensure_graph_json(root, build)
    g = json.loads(gj.read_text(encoding="utf-8"))
    nodes = g["nodes"]
    links = g.get("links") or g.get("edges") or []
    labels_p = root / "graphify-out" / ".graphify_labels.json"
    comm_labels = json.loads(labels_p.read_text(encoding="utf-8")) if labels_p.exists() else {}
    print(f"проект: {root}\nграф FalkorDB: '{name}'  ({len(nodes)} узлов, {len(links)} рёбер)")

    common.register(name, root)

    cfg = common.load_config()
    if model:
        model_name = model
        switching = bool(cfg and cfg.get("model") != model_name)
    elif cfg:
        model_name, switching = cfg["model"], False
    else:
        model_name, switching = common.pick_model(), False
    is_e5 = "e5" in model_name.lower()
    print(f"модель эмбеддингов: {model_name}{' (e5)' if is_e5 else ''}")
    if switching:
        print(f"⚠ смена модели (было {cfg['model']}) — ОСТАЛЬНЫЕ проекты надо перезалить!")

    fcache: dict = {}
    clean_texts = [common.node_text(n, comm_labels, root=root, cache=fcache,
                                    snippets=not no_snippets) for n in nodes]
    embed_texts = ["passage: " + t for t in clean_texts] if is_e5 else clean_texts

    emb_cache = {} if (switching or no_cache) else common.load_emb_cache(model_name)
    hashes = [common.text_hash(model_name, t) for t in embed_texts]
    missing = {}
    for h, t in zip(hashes, embed_texts):
        if h not in emb_cache and h not in missing:
            missing[h] = t
    reused = sum(1 for h in hashes if h in emb_cache)
    print(f"эмбеддинги: из кэша {reused}, считаю заново {len(missing)} из {len(embed_texts)}")
    fe_used = None
    if missing:
        # эмбеддер ТОЛЬКО при промахах: общий сервис (модель в RAM один раз) или локальный
        # fastembed; threads/parallel ограничивают RAM onnxruntime в локальном режиме
        os.environ.setdefault("OMP_NUM_THREADS", str(threads))
        try:
            emb = common.make_embedder(model_name, threads=threads)
        except Exception as ex:
            sys.exit(f"модель '{model_name}' недоступна: {ex}")
        for h, v in zip(missing.keys(),
                        emb.embed(list(missing.values()), batch_size=batch, parallel=None)):
            emb_cache[h] = v
        # версия fastembed, которой реально посчитаны новые вектора (эталон для make_embedder)
        if isinstance(emb, common.RemoteEmbedder):
            fe_used = (common._probe_embed(common.embed_url() or "") or {}).get("fastembed")
        else:
            fe_used = common.fastembed_version()
    common.save_emb_cache(model_name, emb_cache)
    if fe_used:
        common.update_config(fastembed=fe_used)
    embs = [list(emb_cache[h]) for h in hashes]
    dim = len(embs[0])
    if not cfg or switching:
        # merge: не затереть embed_url, если общий сервис уже сконфигурирован
        common.update_config(model=model_name, dim=dim, is_e5=is_e5)
    elif cfg.get("dim") != dim:
        sys.exit(f"размерность {dim} != config {cfg.get('dim')}; перезапусти с --model.")

    db = common.connect(host, port)
    g_db = db.select_graph(name)
    try:
        g_db.delete()
    except Exception:
        pass
    g_db = db.select_graph(name)

    # strip_control на границе записи: FalkorDB-парсер параметров отвергает строки с
    # сырыми управляющими байтами (см. common.strip_control). Чистим все строковые поля,
    # чтобы краш не зависел от того, откуда пришёл текст (исходник, label, метка сообщества).
    sc = common.strip_control
    rows = [{
        "id": sc(n["id"]), "label": sc(n.get("label", "")), "ft": sc(n.get("file_type", "")),
        "sf": sc(n.get("source_file", "")), "loc": sc(n.get("source_location") or ""),
        "comm": n.get("community"), "clabel": sc(comm_labels.get(str(n.get("community")), "")),
        "text": sc(txt), "emb": e,
    } for n, e, txt in zip(nodes, embs, clean_texts)]
    for i in range(0, len(rows), 200):
        g_db.query(
            "UNWIND $rows AS r CREATE (n:Entity {id:r.id, label:r.label, file_type:r.ft, "
            "source_file:r.sf, source_location:r.loc, community:r.comm, "
            "community_label:r.clabel, text:r.text, embedding: vecf32(r.emb)})",
            params={"rows": rows[i:i + 200]})

    try:
        g_db.query(f"CREATE VECTOR INDEX FOR (n:Entity) ON (n.embedding) "
                   f"OPTIONS {{dimension:{dim}, similarityFunction:'cosine'}}")
    except Exception as ex:
        print(f"vector index: {ex}")
    try:
        g_db.query("CREATE INDEX FOR (n:Entity) ON (n.id)")
    except Exception:
        pass
    try:
        g_db.query("CALL db.idx.fulltext.createNodeIndex('Entity', 'label', 'text')")
    except Exception as ex:
        print(f"fulltext index: {ex}")

    erows = [{"s": sc(l["source"]), "t": sc(l["target"]), "rel": sc(l.get("relation", "link")),
              "w": l.get("weight", 1.0)} for l in links]
    for i in range(0, len(erows), 500):
        g_db.query(
            "UNWIND $rows AS r MATCH (a:Entity {id:r.s}), (b:Entity {id:r.t}) "
            "CREATE (a)-[:LINK {relation:r.rel, weight:r.w}]->(b)",
            params={"rows": erows[i:i + 500]})

    cnt = g_db.query("MATCH (n:Entity) RETURN count(n)").result_set[0][0]
    rel = g_db.query("MATCH ()-[r:LINK]->() RETURN count(r)").result_set[0][0]

    # метка свежести: на каком git-коммите построен граф (для grafit status / шапки ответов)
    gi = common.git_info(root) or {}
    common.save_meta(name, commit=gi.get("commit"), short=gi.get("short"),
                     committed_at=gi.get("committed_at"), branch=gi.get("branch"),
                     dirty_at_build=gi.get("dirty"), built_at=common.now_iso(),
                     root=str(root), nodes=cnt, edges=rel, model=model_name)
    if gi.get("dirty"):
        print("⚠ рабочее дерево грязное — граф отражает закоммиченное + несохранённые правки на диске")

    print(f"✓ граф '{name}': {cnt} узлов, {rel} рёбер. Готово.")
    return {"graph": name, "nodes": cnt, "edges": rel, "dim": dim, "model": model_name}
