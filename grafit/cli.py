"""grafit CLI: up | down | load | query | eval | list | mcp."""
from __future__ import annotations
import argparse, shutil, subprocess, sys
from pathlib import Path

from . import common

CONTAINER = "grafit-falkordb"
IMAGE = "falkordb/falkordb:latest"
DEFAULT_PORT = 6399   # уникальный хост-порт (6379 занят прочими FalkorDB)
UI_PORT = 6400

# Общий сервис эмбеддингов: модель в RAM один раз, отвечает всем агентам по HTTP.
EMBED_CONTAINER = "grafit-embed"
EMBED_IMAGE = "grafit-embed:local"
EMBED_PORT = 6401     # уникальный хост-порт (HTTP /embed, /health), только localhost
DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-large"
# Версия fastembed фиксируется ЖЁСТКО: вектора зависят не от имени модели, а от
# реализации (пример — смена CLS→mean pooling для e5 между версиями). Должна совпадать
# с пином в pyproject.toml и Dockerfile.embed. Сменил — перезалей проекты `--no-cache`.
PINNED_FASTEMBED = "0.8.0"

_DOCKERFILE_EMBED = f"""\
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastembed=={PINNED_FASTEMBED}" fastapi "uvicorn[standard]"
ARG EMBED_MODEL=intfloat/multilingual-e5-large
ENV GRAFIT_EMBED_MODEL=${{EMBED_MODEL}}
# Запекаем модель в образ ДО COPY сервера: правки server.py не триггерят пере-скачивание.
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(model_name=os.environ['GRAFIT_EMBED_MODEL'])"
COPY server.py /app/server.py
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
"""


def _container_exists(name: str) -> bool:
    return bool(subprocess.run(["docker", "ps", "-aq", "-f", f"name=^{name}$"],
                               capture_output=True, text=True).stdout.strip())


def _image_exists(ref: str) -> bool:
    return bool(subprocess.run(["docker", "images", "-q", ref],
                               capture_output=True, text=True).stdout.strip())


def _build_embed_image(model: str):
    """Собрать образ grafit-embed из standalone embed_server.py (контекст в ~/.grafit)."""
    # Путь к standalone-серверу без import (он тянет fastapi, которого нет в окружении тула).
    src = Path(__file__).resolve().parent / "embed_server.py"
    bd = common.HOME / "embed-build"
    bd.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, bd / "server.py")
    (bd / "Dockerfile").write_text(_DOCKERFILE_EMBED, encoding="utf-8")
    print(f"собираю образ {EMBED_IMAGE} (модель {model} запекается — это разово, ~несколько минут)…")
    subprocess.run(["docker", "build", "-t", EMBED_IMAGE,
                    "--build-arg", f"EMBED_MODEL={model}", str(bd)], check=True)


def _up_embed(model: str, rebuild: bool):
    if rebuild and _container_exists(EMBED_CONTAINER):
        subprocess.run(["docker", "rm", "-f", EMBED_CONTAINER])
    if _container_exists(EMBED_CONTAINER) and not rebuild:
        subprocess.run(["docker", "start", EMBED_CONTAINER])
    else:
        if rebuild or not _image_exists(EMBED_IMAGE):
            _build_embed_image(model)
        subprocess.run([
            "docker", "run", "-d", "--name", EMBED_CONTAINER, "--restart", "unless-stopped",
            "-p", f"127.0.0.1:{EMBED_PORT}:8080",
            "-e", f"GRAFIT_EMBED_MODEL={model}",
            "-v", "grafit-embed-cache:/root/.cache/fastembed",
            EMBED_IMAGE])
    url = f"http://127.0.0.1:{EMBED_PORT}"
    common.update_config(embed_url=url)
    print(f"grafit-embed '{EMBED_CONTAINER}' на {url}  (модель {model}, embed_url прописан в config)")


def _up(args):
    # Идемпотентно: если контейнер есть — стартуем, иначе создаём.
    if _container_exists(CONTAINER):
        subprocess.run(["docker", "start", CONTAINER])
    else:
        subprocess.run([
            "docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
            "-p", f"127.0.0.1:{args.port}:6379", "-p", f"127.0.0.1:{UI_PORT}:3000",
            "-v", "grafit-falkordb:/data", IMAGE])
    print(f"FalkorDB '{CONTAINER}' на 127.0.0.1:{args.port} (UI :{UI_PORT})")
    if args.no_embed:
        print("grafit-embed пропущен (--no-embed) — модель будет грузиться в каждый процесс локально")
        return
    cfg = common.load_config() or {}
    model = args.embed_model or cfg.get("model") or DEFAULT_EMBED_MODEL
    _up_embed(model, rebuild=args.rebuild_embed)


def _down(args):
    subprocess.run(["docker", "stop", EMBED_CONTAINER, CONTAINER])


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


def _status(args):
    if args.all:
        names = sorted(common.load_meta().keys()) or sorted(common.list_graphs(args.host, args.port))
        if not names:
            print("нет графов с метаданными — перезалей: grafit load"); return
        for i, name in enumerate(names):
            # для чужого графа сравниваем с его meta.root (live_root=None), не с cwd
            print(common.freshness_report(name))
            if i < len(names) - 1:
                print()
        return
    # без --all: текущий проект (по cwd), сравнение с рабочим деревом cwd
    name = common.graph_name(args.graph)
    live = None if args.graph else common.project_root()
    print(common.freshness_report(name, live_root=live))


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
                         reranker=reranker, kind=args.kind)
    fresh = common.freshness_line(name, live_root=(None if args.graph else common.project_root()))
    if not rows:
        print(f"{fresh}\nграф '{name}': ничего не найдено (залит ли проект?)"); return
    mode = ("гибрид" if args.hybrid else "вектор") + (f"+реранк:{rname}" if reranker else "")
    mode += f", kind={args.kind}" if args.kind != "all" else ""
    print(f"{fresh}\n[{name}] {args.question}   ({mode})\n{'=' * 70}")
    root = common.project_root() if args.snippet else None
    fcache: dict = {}
    nb_shown = False
    for nid, label, ft, sf, loc, clabel, text, score in rows:
        tag = " ·тест" if common.is_test_path(sf) else ""
        print(f"\n● {label}  ({ft}){tag}")
        if sf:
            print(f"   {sf}:{loc}   сообщество: {clabel}")
        if args.snippet:
            snip = common.read_snippet(root, sf, loc, window=3, max_chars=240, cache=fcache)
            for ln in snip.splitlines():
                print(f"     │ {ln}")
        for d, rel, mlabel, msf in search.neighbors(g, nid, args.neighbors):
            struct = common.relation_kind(rel) == "structural"
            body = "─" if struct else "⋯"
            edge = f"{body}{rel}→" if d == "out" else f"←{rel}{body}"
            inf = "" if struct else "  (inferred)"
            print(f"     {edge} {mlabel}  ({msf}){inf}")
            nb_shown = True
    if nb_shown:
        print("\nлегенда: →исходящее · ←входящее · ─структурное (AST) · ⋯производное (by-naming)")


def _nav_cmd(args, fmt):
    from . import nav
    name = common.graph_name(args.graph)
    g = common.connect(args.host, args.port).select_graph(name)
    fresh = common.freshness_line(name, live_root=(None if args.graph else common.project_root()))
    print("\n".join([fresh] + fmt(nav, g, name)))


def _tests(args):
    _nav_cmd(args, lambda nav, g, name: nav.format_tests(g, name, args.symbol, args.hops))


def _impact(args):
    _nav_cmd(args, lambda nav, g, name: nav.format_impact(g, name, args.symbol, args.hops))


def _trace(args):
    _nav_cmd(args, lambda nav, g, name: nav.format_trace(
        g, name, args.source, args.hops, args.to or "", args.with_references))


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

    p = sub.add_parser("up", help="поднять FalkorDB + общий сервис эмбеддингов (docker)")
    p.add_argument("--no-embed", action="store_true", help="не поднимать grafit-embed (модель локально в каждом процессе)")
    p.add_argument("--embed-model", default=None, help=f"модель для grafit-embed (по умолч. из config или {DEFAULT_EMBED_MODEL})")
    p.add_argument("--rebuild-embed", action="store_true", help="пересобрать образ grafit-embed (смена модели)")
    p.set_defaults(fn=_up)
    p = sub.add_parser("down", help="остановить FalkorDB + grafit-embed"); p.set_defaults(fn=_down)

    p = sub.add_parser("load", help="проиндексировать проект")
    p.add_argument("path", nargs="?", default=None, help="корень проекта (по умолч. cwd)")
    p.add_argument("--graph", default=None); p.add_argument("--model", default=None)
    p.add_argument("--build", action="store_true", help="сначала построить graph.json через graphify")
    p.add_argument("--no-snippets", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--threads", type=int, default=4); p.add_argument("--batch", type=int, default=16)
    p.set_defaults(fn=_load)

    p = sub.add_parser("list", help="графы проектов"); p.set_defaults(fn=_list)

    p = sub.add_parser("status", help="свежесть графа (на каком коммите построен, отстал ли от HEAD)")
    p.add_argument("--graph", default=None, help="имя графа (по умолч. из cwd)")
    p.add_argument("--all", action="store_true", help="отчёт по всем графам с метаданными")
    p.set_defaults(fn=_status)

    p = sub.add_parser("query", help="семантический поиск по коду")
    p.add_argument("question")
    p.add_argument("-k", type=int, default=8); p.add_argument("--neighbors", type=int, default=6)
    p.add_argument("--kind", choices=["all", "code", "tests", "docs", "prod"], default="all",
                   help="фильтр узлов: code|tests|docs|prod (prod=код без тестов/миграций/генерёнки)")
    p.add_argument("--snippet", action="store_true", help="показать реальные строки исходника у узлов")
    p.add_argument("--cand", type=int, default=60)
    p.add_argument("--test-penalty", type=float, default=0.5)
    p.add_argument("--hybrid", action="store_true", help="вектор+лексика (RRF), opt-in")
    p.add_argument("--lex-weight", type=float, default=0.4)
    p.add_argument("--no-generic-filter", action="store_true")
    p.add_argument("--rerank", action="store_true", help="кросс-энкодер реранкер, opt-in")
    p.add_argument("--threads", type=int, default=4); p.add_argument("--graph", default=None)
    p.set_defaults(fn=_query)

    p = sub.add_parser("tests", help="тесты, связанные с символом (перед изменением)")
    p.add_argument("symbol"); p.add_argument("--graph", default=None)
    p.add_argument("--hops", type=int, default=2)
    p.set_defaults(fn=_tests)

    p = sub.add_parser("impact", help="что сломается при изменении символа (входящие зависимости)")
    p.add_argument("symbol"); p.add_argument("--graph", default=None)
    p.add_argument("--hops", type=int, default=2)
    p.set_defaults(fn=_impact)

    p = sub.add_parser("trace", help="проследить поток вперёд от символа (или путь до --to)")
    p.add_argument("source"); p.add_argument("--to", default=None, help="цель: кратчайший путь source→target")
    p.add_argument("--graph", default=None); p.add_argument("--hops", type=int, default=4)
    p.add_argument("--with-references", action="store_true", help="подмешать шумные references-рёбра")
    p.set_defaults(fn=_trace)

    p = sub.add_parser("eval", help="метрики поиска по золотому набору")
    p.add_argument("--graph", default=None); p.add_argument("--golden", default=None)
    p.add_argument("--threads", type=int, default=4)
    p.set_defaults(fn=_eval)

    p = sub.add_parser("mcp", help="запустить MCP-сервер (stdio)"); p.set_defaults(fn=_mcp)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
