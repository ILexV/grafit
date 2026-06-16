"""grafit CLI: up | down | load | query | eval | list | mcp."""
from __future__ import annotations
import argparse, subprocess, sys

from . import common

CONTAINER = "grafit-falkordb"
IMAGE = "falkordb/falkordb:latest"
DEFAULT_PORT = 6399   # уникальный хост-порт (6379 занят прочими FalkorDB)
UI_PORT = 6400


def _up(args):
    # Идемпотентно: если контейнер есть — стартуем, иначе создаём.
    exists = subprocess.run(["docker", "ps", "-aq", "-f", f"name=^{CONTAINER}$"],
                            capture_output=True, text=True).stdout.strip()
    if exists:
        subprocess.run(["docker", "start", CONTAINER])
    else:
        subprocess.run([
            "docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
            "-p", f"127.0.0.1:{args.port}:6379", "-p", f"127.0.0.1:{UI_PORT}:3000",
            "-v", "grafit-falkordb:/data", IMAGE])
    print(f"FalkorDB '{CONTAINER}' на 127.0.0.1:{args.port} (UI :{UI_PORT})")


def _down(args):
    subprocess.run(["docker", "stop", CONTAINER])


def _load(args):
    from . import index
    index.index_project(project=args.path, graph=args.graph, host=args.host, port=args.port,
                        model=args.model, no_snippets=args.no_snippets, no_cache=args.no_cache,
                        threads=args.threads, batch=args.batch, build=args.build)


def _list(args):
    reg = common.load_registry()
    print("Графы проектов в FalkorDB:")
    for g in sorted(common.list_graphs(args.host, args.port)):
        print(f"  • {g:30} {reg.get(g, '(нет в реестре)')}")


def _query(args):
    from . import search
    cfg = common.load_config()
    if not cfg:
        sys.exit("config нет — сначала `grafit load`")
    name = common.graph_name(args.graph)
    model = search.get_embedder(cfg, threads=args.threads)
    qvec = search.embed_query(model, cfg, args.question)
    reranker = rname = None
    if args.rerank:
        reranker, rname = search.get_reranker(threads=args.threads)
    g = common.connect(args.host, args.port).select_graph(name)
    rows = search.search(g, qvec, args.question, k=args.k, cand=args.cand,
                         test_penalty=args.test_penalty, hybrid=args.hybrid,
                         lex_weight=args.lex_weight, drop_generic=not args.no_generic_filter,
                         reranker=reranker)
    if not rows:
        print(f"граф '{name}': ничего не найдено (залит ли проект?)"); return
    mode = ("гибрид" if args.hybrid else "вектор") + (f"+реранк:{rname}" if reranker else "")
    print(f"[{name}] {args.question}   ({mode})\n{'=' * 70}")
    for nid, label, ft, sf, loc, clabel, text, score in rows:
        tag = " ·тест" if common.is_test_path(sf) else ""
        print(f"\n● {label}  ({ft}){tag}")
        if sf:
            print(f"   {sf}:{loc}   сообщество: {clabel}")
        for rel, mlabel, msf in search.neighbors(g, nid, args.neighbors):
            print(f"     ─ {rel} → {mlabel}  ({msf})")


def _eval(args):
    from . import eval as ev
    ev.run_eval(graph=args.graph, golden=args.golden, host=args.host,
                port=args.port, threads=args.threads)


def _mcp(args):
    from . import mcp_server
    mcp_server.main()


def main():
    ap = argparse.ArgumentParser(prog="grafit", description="Code knowledge-graph + семантический поиск")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("up", help="поднять FalkorDB (docker)"); p.set_defaults(fn=_up)
    p = sub.add_parser("down", help="остановить FalkorDB"); p.set_defaults(fn=_down)

    p = sub.add_parser("load", help="проиндексировать проект")
    p.add_argument("path", nargs="?", default=None, help="корень проекта (по умолч. cwd)")
    p.add_argument("--graph", default=None); p.add_argument("--model", default=None)
    p.add_argument("--build", action="store_true", help="сначала построить graph.json через graphify")
    p.add_argument("--no-snippets", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--threads", type=int, default=4); p.add_argument("--batch", type=int, default=16)
    p.set_defaults(fn=_load)

    p = sub.add_parser("list", help="графы проектов"); p.set_defaults(fn=_list)

    p = sub.add_parser("query", help="семантический поиск по коду")
    p.add_argument("question")
    p.add_argument("-k", type=int, default=8); p.add_argument("--neighbors", type=int, default=6)
    p.add_argument("--cand", type=int, default=60)
    p.add_argument("--test-penalty", type=float, default=0.5)
    p.add_argument("--hybrid", action="store_true", help="вектор+лексика (RRF), opt-in")
    p.add_argument("--lex-weight", type=float, default=0.4)
    p.add_argument("--no-generic-filter", action="store_true")
    p.add_argument("--rerank", action="store_true", help="кросс-энкодер реранкер, opt-in")
    p.add_argument("--threads", type=int, default=4); p.add_argument("--graph", default=None)
    p.set_defaults(fn=_query)

    p = sub.add_parser("eval", help="метрики поиска по золотому набору")
    p.add_argument("--graph", default=None); p.add_argument("--golden", default=None)
    p.add_argument("--threads", type=int, default=4)
    p.set_defaults(fn=_eval)

    p = sub.add_parser("mcp", help="запустить MCP-сервер (stdio)"); p.set_defaults(fn=_mcp)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
