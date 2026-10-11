"""MCP-сервер grafit: семантический поиск по коду из любого MCP-клиента (stdio).

Инструменты: grafit_search, grafit_impact, grafit_tests, grafit_explain, grafit_trace,
grafit_status, grafit_find_existing, grafit_similar, grafit_dupes
(+ редкие: grafit_find_path, grafit_list_projects).
Эмбеддер грузится лениво (на первый запрос) и кэшируется — старт сервера лёгкий.
Проект определяется по cwd клиента (или явным параметром project = имя графа).

Конфиг: GRAFIT_HOST / GRAFIT_PORT (по умолч. localhost:6399), GRAFIT_THREADS (4),
GRAFIT_TOOLS = all (по умолч.) | core (без редких) | список имён через запятую
(search,impact,…), GRAFIT_USAGE_LOG=0 — выключить журнал ~/.grafit/usage.jsonl,
GRAFIT_MCP_REFRESH=0 — не запускать фоновый `grafit refresh` своего проекта при старте.
"""
from __future__ import annotations
import contextvars, functools, json, os, re, time
from . import common, dupes, nav, search

HOST = os.environ.get("GRAFIT_HOST", "localhost")
PORT = int(os.environ.get("GRAFIT_PORT", "6399"))
THREADS = int(os.environ.get("GRAFIT_THREADS", "4"))
USAGE_LOG = common.HOME / "usage.jsonl"

_embedder = None  # ленивый кэш модели


def _embed(question: str):
    global _embedder
    cfg = common.load_config()
    if not cfg:
        raise RuntimeError("grafit не сконфигурирован — выполни `grafit load` хотя бы в одном проекте")
    if _embedder is None:
        _embedder = search.get_embedder(cfg, threads=THREADS)
    return search.embed_query(_embedder, cfg, question), cfg


def _graph(project: str | None):
    name = common.graph_name(project or None)
    _stat_set(project=name)
    return common.connect(HOST, PORT).select_graph(name), name


class _Fresh:
    """Свежесть графа на один вызов: строка шапки + множество файлов, изменённых после сборки
    (узлы из них помечаются, цитаты из них сверяются с label)."""

    def __init__(self, name: str, project: str | None):
        live = None if project else common.project_root()
        f = common.graph_freshness(name, live)
        self.line = common.freshness_line(name, live, f=f)
        self.changed = set(f.get("changed") or [])

    def mark(self, sf) -> str:
        return "  ⚠ изменён после сборки" if sf and sf in self.changed else ""


def _fresh(name: str, project: str | None) -> str:
    """Шапка свежести. Без явного project сравниваем с git-деревом cwd клиента."""
    return _Fresh(name, project).line


_LEGEND = "  легенда: →исходящее · ←входящее · ─структурное (AST) · ⋯производное (by-naming)"


def _alt_lines(r: dict) -> list[str]:
    """Строки с альтернативными кандидатами, когда символ неоднозначен (#1 disambiguation)."""
    alts = r.get("alternatives") or []
    if not r.get("ambiguous") or not alts:
        return []
    items = "; ".join(f"{a['label']} ({a['ft']}, {a['sf']})" for a in alts)
    return [f"  ещё кандидаты: {items}  — уточни project/точное имя/путь"]


def _nb(direction, rel, mlabel, msf) -> str:
    """Соседняя связь с направлением и уверенностью: out '─rel→', in '←rel─';
    структурное (AST) ─/→ vs производное (by-naming/семантика) ⋯ + '(inferred)'."""
    struct = common.relation_kind(rel) == "structural"
    body = "─" if struct else "⋯"
    edge = f"{body}{rel}→" if direction == "out" else f"←{rel}{body}"
    tail = "" if struct else " (inferred)"
    return f"  {edge} {mlabel} ({msf}){tail}"


def _root(name: str) -> str:
    """Корень проекта на диске для графа (из meta.json; иначе cwd)."""
    m = common.load_meta().get(name) or {}
    return m.get("root") or str(common.project_root())


_IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")


def _quote(root, sf, loc, cache, label: str = "", changed: bool = False) -> list[str]:
    """Цитата: реальные строки файла у source_location, СВЕРЕННЫЕ с узлом. Граф хранит номер
    строки на момент сборки; если файл с тех пор правили, по старому номеру лежит чужой код.
    Имя символа не нашлось в цитате → «строка сместилась» (+ где оно сейчас), а не чужой код."""
    snip = common.read_snippet(root, sf, loc, window=3, max_chars=240, cache=cache)
    bare = nav._bare(label or "")
    if not _IDENT.match(bare or ""):
        bare = ""   # файл-узел (Foo.cs)/концепт/неидентификатор — сверять не с чем
    head = "\n".join(snip.splitlines()[:2])   # объявление — на строке loc (+1 на перенос сигнатуры)
    if snip and (not bare or re.search(rf"(?<![\w$]){re.escape(bare)}(?![\w$])", head)):
        tag = ["    (файл изменён после сборки — цитата из текущей версии)"] if changed else []
        return tag + [f"    │ {ln}" for ln in snip.splitlines()]
    if not bare:
        return []
    why = "файл изменён после сборки" if changed else "строка не совпадает с узлом"
    now = _relocate(root, sf, bare, cache)
    where = f"; сейчас «{bare}» похоже на L{now}" if now else f"; «{bare}» в файле не найден"
    return [f"    ⚠ строка сместилась ({why}){where} — открой файл, не верь номеру {loc}"]


def _relocate(root, sf, bare, cache) -> int | None:
    """Номер строки, где символ встречается в текущей версии файла (первое вхождение)."""
    lines = common._file_lines(root, sf, cache)
    if not lines:
        return None
    rx = re.compile(rf"(?<![\w$]){re.escape(bare)}(?![\w$])")
    for i, ln in enumerate(lines, 1):
        if rx.search(ln):
            return i
    return None


# --- журнал вызовов: что реально зовут агенты (без текста запросов — только длина) ---
_STAT: contextvars.ContextVar = contextvars.ContextVar("grafit_stat", default=None)


def _stat_set(**kw) -> None:
    st = _STAT.get()
    if st is not None:
        st.update(kw)


def _log_usage(rec: dict) -> None:
    if os.environ.get("GRAFIT_USAGE_LOG", "1") == "0":
        return
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass   # журнал — best effort, инструмент не должен падать из-за него


def _logged(fn):
    """Обёртка инструмента: пишет в usage.jsonl инструмент, проект, время, мс, число
    результатов, длину запроса. Сигнатура сохраняется (FastMCP строит схему по ней)."""
    @functools.wraps(fn)
    def wrapper(*args, **kw):
        st = {}
        tok = _STAT.set(st)
        t0 = time.monotonic()
        q = next((kw[k] for k in ("question", "description", "symbol", "source") if isinstance(kw.get(k), str)), None)
        ok = True
        try:
            out = fn(*args, **kw)
            return out
        except Exception:
            ok = False
            raise
        finally:
            _STAT.reset(tok)
            _log_usage({"ts": common.now_iso(), "tool": fn.__name__,
                        "project": st.get("project") or kw.get("project") or None,
                        "ms": int((time.monotonic() - t0) * 1000), "n": st.get("n"),
                        "q_len": len(q) if q is not None else None, "ok": ok})
    return wrapper


# Редкие инструменты: GRAFIT_TOOLS=core их скрывает (меньше шума в списке инструментов агента).
# similar/dupes НЕ редкие — поиск дублей перед написанием кода главный сценарий grafit.
RARE_TOOLS = {"grafit_list_projects", "grafit_find_path"}


def tool_enabled(name: str, spec: str | None = None) -> bool:
    spec = (os.environ.get("GRAFIT_TOOLS", "all") if spec is None else spec).strip().lower()
    if spec in ("", "all"):
        return True
    if spec == "core":
        return name not in RARE_TOOLS
    wanted = {w.strip() for w in spec.split(",") if w.strip()}
    return name in wanted or name.removeprefix("grafit_") in wanted


try:
    from mcp.server.fastmcp import FastMCP
except Exception as ex:  # pragma: no cover
    raise SystemExit("Не установлен MCP SDK. Поставь: pip install 'grafit-mcp[mcp]' или mcp") from ex

_INSTRUCTIONS = """\
grafit — граф кода проекта с поиском ПО СМЫСЛУ (эмбеддинги multilingual-e5). Проект — по cwd.

Главное: не писать заново то, что в проекте уже есть. Проекты большие, код писали разные
модели, имена непредсказуемы — grep по тексту не находит, grafit находит по смыслу (синонимы,
RU/EN, опечатки, другое название).
• ПЕРЕД созданием метода/класса/сервиса/хука/компонента — grafit_find_existing("что он делает,
  словами"): покажет похожий существующий код со score. Нашлось сильное — переиспользуй/расширь.
• Разведка «где/как реализовано X» — grafit_search первым, до Read/Grep:
  «где проверяется доступ к филиалу» → классы/методы с path:line. grep — для известной строки.
• Перед правкой/удалением символа — grafit_impact (кто использует, в т.ч. непрямо) и
  grafit_tests (какие тесты заденет).
• Написал новый символ — grafit_similar(symbol, file=путь): не дублирует ли он старый код.

score — близость 0..1: сильное / слабое (проверь) / «ничего похожего» (значит, такого кода нет).
Шапка ответа — свежесть графа: файлы, изменённые после сборки, помечены «изменён после
сборки» (перепроверь их в файле); результаты из неизменённых файлов надёжны.
"""

mcp = FastMCP("grafit", instructions=_INSTRUCTIONS)


def _tool(fn):
    """Регистрация инструмента: журнал вызовов + фильтр GRAFIT_TOOLS."""
    if tool_enabled(fn.__name__):
        mcp.tool()(_logged(fn))
    return fn


def _score(dist) -> str:
    return "score —" if dist is None else f"score {1 - dist:.2f}"


_Q_TEXT = {"strong": "сильное", "weak": "слабое — проверь", "none": "ничего похожего"}


def _verdict(res: dict) -> str:
    q = search.match_quality(res["best_dist"])
    best = "" if res["best_dist"] is None else f", лучший score {1 - res['best_dist']:.2f}"
    return f"совпадение: {_Q_TEXT[q]}{best}"


def _empty_answer(fr, name, question, kind, res) -> str | None:
    """Честный пустой ответ: фильтр kind отсёк всё / ничего похожего / граф пуст."""
    if res["rows"] and search.match_quality(res["best_dist"]) != "none":
        return None
    if not res["candidates"]:
        return f"{fr.line}\n[{name}] граф пуст или не залит (`grafit load`)"
    if not res["rows"]:
        return (f"{fr.line}\n[{name}] {question}\nпо фильтру kind={kind} ничего: все "
                f"{res['filtered_out']} ближайших узлов — другого вида (доки/тесты/генерёнка). "
                f"Повтори с kind=all или переформулируй.")
    top = res["rows"][0]
    return (f"{fr.line}\n[{name}] {question}\nничего похожего (лучший score "
            f"{1 - res['best_dist']:.3f} ниже порога {1 - search.NONE_DIST:.3f}). Ближайшее — "
            f"{top[1]} ({top[3]}:{top[4]}), скорее всего не то. Такого кода, вероятно, нет; "
            f"переформулируй или проверь grep'ом по точному имени.")


def _search_rows(question, project, k, kind):
    qvec, _ = _embed(question)
    g, name = _graph(project or None)
    res = search.search_ex(g, qvec, question, k=k, kind=kind)
    _stat_set(n=len(res["rows"]))
    return g, name, res


def _render_rows(g, name, rows, fr, snippet, neighbors, contracts=None) -> list[str]:
    root = _root(name) if snippet else None
    fcache: dict = {}
    out, any_nb = [], False
    for nid, label, ft, sf, loc, clabel, text, dist in rows:
        tag = " [test]" if common.is_test_path(sf) else ""
        out.append(f"\n● {label} ({ft}){tag}  {_score(dist)}\n  {sf}:{loc}{fr.mark(sf)}")
        if contracts and nid in contracts:
            clabel_, csf = contracts[nid]
            out.append(f"  ↳ контракт: {clabel_} ({csf}) — свёрнут сюда, это не дубль")
        if snippet:
            out.extend(_quote(root, sf, loc, fcache, label, sf in fr.changed))
        for d, rel, mlabel, msf in (search.neighbors(g, nid, neighbors) if neighbors else []):
            any_nb = True
            out.append("  " + _nb(d, rel, mlabel, msf))
    if any_nb:
        out.append(_LEGEND)
    return out


@_tool
def grafit_search(question: str, k: int = 8, project: str = "", kind: str = "prod",
                  snippet: bool = False, neighbors: int = 3) -> str:
    """Где/как в проекте реализовано X — поиск ПО СМЫСЛУ, зови первым на разведке, до Read/Grep.
    Пример: «где проверяется доступ к филиалу» → классы/методы с path:line и score, даже если
    слов запроса нет в именах (синонимы, RU/EN, опечатки). grep — только для известной строки.

    kind: prod (по умолч. — код без тестов/доков/генерёнки) | code | tests | docs | frontend |
    backend | all. snippet: показать строки исходника. «ничего похожего» = такого кода нет."""
    g, name, res = _search_rows(question, project, k, kind)
    fr = _Fresh(name, project or None)
    empty = _empty_answer(fr, name, question, kind, res)
    if empty:
        return empty
    out = [fr.line, f"[{name}] {question}  (kind={kind} · {_verdict(res)})"]
    out.extend(_render_rows(g, name, res["rows"], fr, snippet, neighbors, res["contracts"]))
    return "\n".join(out)


@_tool
def grafit_find_existing(description: str, k: int = 6, project: str = "", kind: str = "prod") -> str:
    """ПЕРЕД тем как писать новую функцию/класс/сервис/хук/компонент — нет ли такого уже в проекте.
    Опиши назначение словами: «пересчитать норму часов сотрудника за месяц» → похожий
    существующий код со score и строками исходника; устойчив к другим названиям и опечаткам.

    Сильное совпадение — переиспользуй/расширь найденное вместо нового кода."""
    g, name, res = _search_rows(description, project, k, kind)
    fr = _Fresh(name, project or None)
    empty = _empty_answer(fr, name, description, kind, res)
    if empty:
        return empty.replace("Такого кода, вероятно, нет;", "Похожего кода, вероятно, нет — можно писать новый;")
    q = search.match_quality(res["best_dist"])
    advice = ("похожее уже есть — сначала посмотри, нельзя ли переиспользовать/расширить"
              if q == "strong" else "явного аналога нет, но проверь кандидатов ниже")
    out = [fr.line, f"[{name}] уже есть? «{description}»  (kind={kind} · {_verdict(res)})",
           f"вывод: {advice}"]
    out.extend(_render_rows(g, name, res["rows"], fr, snippet=True, neighbors=0,
                            contracts=res["contracts"]))
    return "\n".join(out)


@_tool
def grafit_impact(symbol: str, project: str = "", max_hops: int = 2) -> str:
    """Что сломается, если изменить/удалить/перенести символ: кто его использует (в т.ч.
    непрямо), сгруппировано по tests/frontend/backend/contract/docs. Пример: «IUserBranchAccessService»
    → 40 потребителей за ~0,6 с; ловит то, что grep по имени пропускает. Точный обход графа."""
    g, name = _graph(project or None)
    lines = nav.format_impact(g, name, symbol, int(max_hops))
    _stat_set(n=max(0, len(lines) - 1))
    return "\n".join([_fresh(name, project or None)] + lines)


@_tool
def grafit_tests(symbol: str, project: str = "", max_hops: int = 2) -> str:
    """Какие тесты заденет правка символа (функции/класса/endpoint) — зови перед изменением,
    чтобы прогнать нужные тесты точечно. Обход графа + связи по конвенции имён."""
    g, name = _graph(project or None)
    lines = nav.format_tests(g, name, symbol, int(max_hops))
    _stat_set(n=max(0, len(lines) - 1))
    return "\n".join([_fresh(name, project or None)] + lines)


@_tool
def grafit_explain(symbol: str, project: str = "", neighbors: int = 10, snippet: bool = True) -> str:
    """Узел по ТОЧНОМУ имени (класс/функция) и его связи: где определён, что вызывает, кто
    использует, строки исходника. Имя неизвестно — сначала grafit_search."""
    g, name = _graph(project or None)
    fr = _Fresh(name, project or None)
    r = nav.resolve_node(g, symbol)
    if not r:
        _stat_set(n=0)
        return f"{fr.line}\n[{name}] узел '{symbol}' не найден"
    _stat_set(n=1)
    match = "точное совпадение" if r["match"] == "exact" else "по подстроке"
    hdr = f"[{name}] {r['label']} ({r['ft']}) — {match}"
    if r["n_candidates"] > 1:
        hdr += f" · {r['n_candidates']} опред." + (" ⚠ неоднозначно" if r["ambiguous"] else "")
    elif r.get("fragments"):
        hdr += f" · +{r['fragments']} reference-узлов"
    out = [fr.line, f"{hdr}\n  {r['sf']}:{r['loc']}{fr.mark(r['sf'])}"]
    out.extend(_alt_lines(r))
    if snippet:
        out.extend(_quote(_root(name), r["sf"], r["loc"], {}, r["label"], r["sf"] in fr.changed))
    nbs = search.neighbors(g, r["id"], neighbors)
    for d, rel, mlabel, msf in nbs:
        out.append(_nb(d, rel, mlabel, msf))
    # file-level imports: для функции/компонента показать, что импортирует её файл
    # (imports висят на файл-узле, не на символе) — помечаем как производные «via file».
    fimp = nav.file_imports(g, r["sf"], exclude_id=r["id"]) if r["ft"] == "code" else []
    for fname, rel, mlabel, msf in fimp:
        out.append(f"  ⋯{rel}→ {mlabel} ({msf}) (via {fname})")
    if nbs or fimp:
        out.append(_LEGEND)
    return "\n".join(out)


@_tool
def grafit_trace(source: str, project: str = "", max_hops: int = 4, target: str = "",
                 with_references: bool = False) -> str:
    """Куда течёт поток от символа: endpoint → handler → service → … (дерево вызовов вперёд).
    С target — кратчайший путь source→target. with_references подмешивает шумные references."""
    g, name = _graph(project or None)
    lines = nav.format_trace(g, name, source, int(max_hops), target, with_references)
    _stat_set(n=max(0, len(lines) - 1))
    return "\n".join([_fresh(name, project or None)] + lines)


@_tool
def grafit_similar(symbol: str, project: str = "", file: str = "", k: int = 6,
                   threshold: float = 0.10, kind: str = "all", rerank: bool = False,
                   snippet: bool = True) -> str:
    """Не дублирует ли символ уже существующий код — near-duplicate по смыслу тела (код→код).
    Только что написал функцию — передай file=путь: символ читается из РАБОЧЕГО ДЕРЕВА, даже
    если его ещё нет в графе (проверка на приёмке). Без file — символ из графа.

    threshold: макс. косинусная дистанция (0=идентично, дефолт 0.10). [копипаст]/[семантика] —
    буквальная копия vs похожая логика. rerank: кросс-энкодер вторым проходом."""
    g, name = _graph(project or None)
    reranker = search.get_reranker(threads=THREADS)[0] if rerank else None
    root = _root(name)
    if file:
        rel = os.path.relpath(os.path.abspath(file), root) if os.path.isabs(file) else file
        anchor = dupes.worktree_text(root, rel, symbol)
        if not anchor:
            _stat_set(n=0)
            return f"{_fresh(name, project or None)}\n[{name}] '{symbol}' не найден в {rel}"
        qvec, cfg = _embed_passage(anchor["text"])
        lines = dupes.format_similar_text(g, name, anchor, qvec, k=k, threshold=threshold,
                                          kind=kind, reranker=reranker,
                                          root=root if snippet else None)
    else:
        lines = dupes.format_similar(g, name, symbol, k=k, threshold=threshold, kind=kind,
                                     reranker=reranker, root=root if snippet else None)
    _stat_set(n=sum(1 for ln in lines if ln.lstrip().startswith("≈")))
    return "\n".join([_fresh(name, project or None)] + lines)


def _embed_passage(text: str):
    """Вектор кода как у узла при индексации (e5: префикс 'passage: ')."""
    global _embedder
    cfg = common.load_config()
    if not cfg:
        raise RuntimeError("grafit не сконфигурирован — выполни `grafit load` хотя бы в одном проекте")
    if _embedder is None:
        _embedder = search.get_embedder(cfg, threads=THREADS)
    t = ("passage: " + text) if cfg["is_e5"] else text
    return next(iter(_embedder.embed([t]))).tolist(), cfg


@_tool
def grafit_dupes(project: str = "", kind: str = "prod", threshold: float = 0.06,
                 limit: int = 20, pairs: bool = False, include_framework: bool = False,
                 rerank: bool = False, snippet: bool = False) -> str:
    """Где в проекте копипаст: глобальный скан near-duplicate кода (кластеры или pairs=True —
    пары) — кандидаты на вынос общего кода. Дороже grafit_similar, для аудита.

    Метки: [копипаст] (тот же текст — сливать) / [семантика] (похожая логика — решать);
    сортировка по выгоде. threshold — макс. дистанция пары (дефолт 0.06)."""
    g, name = _graph(project or None)
    reranker = search.get_reranker(threads=THREADS)[0] if rerank else None
    root = _root(name) if snippet else None
    fmt = dupes.format_pairs if pairs else dupes.format_duplicates
    lines = fmt(g, name, kind=kind, threshold=threshold, limit=limit,
                include_framework=include_framework, reranker=reranker, root=root)
    _stat_set(n=max(0, len(lines) - 1))
    return "\n".join([_fresh(name, project or None)] + lines)


@_tool
def grafit_status(project: str = "") -> str:
    """Свежесть графа: на каком коммите построен и какие файлы кода изменены после сборки
    (их результаты перепроверь; остальное актуально)."""
    name = common.graph_name(project or None)
    _stat_set(project=name)
    live = None if project else common.project_root()
    return common.freshness_report(name, live_root=live)


@_tool
def grafit_find_path(source: str, target: str, project: str = "", max_hops: int = 6) -> str:
    """Как связаны две сущности: кратчайший путь в графе между ними (по именам)."""
    g, name = _graph(project or None)
    fresh = _fresh(name, project or None)
    a, b = nav.resolve_node(g, source), nav.resolve_node(g, target)
    if not a or not b:
        return f"{fresh}\n[{name}] не найдено: {source if not a else target}"
    path = nav.find_route(g, a, b, max_hops=int(max_hops))
    if not path:
        out = [fresh, f"[{name}] путь {source} → {target} не найден (≤{max_hops} шагов, по направлению рёбер)"]
        hint = nav.related_hint(g, a["id"], a["label"])
        if hint:
            out.append(f"прямого пути нет; связано с {a['label']}:")
            out.extend(hint)
        return "\n".join(out)
    return f"{fresh}\n{nav.render_path(name, path)}"


@_tool
def grafit_list_projects() -> str:
    """Какие проекты залиты в grafit (имя графа → путь)."""
    reg = common.load_registry()
    gs = sorted(common.list_graphs(HOST, PORT))
    _stat_set(n=len(gs))
    if not gs:
        return "нет загруженных проектов"
    return "\n".join(f"• {g}  {reg.get(g, '')}" for g in gs)


def main():
    # Догнать отставший граф своего проекта в фоне: не блокирует старт и ответы (граф
    # подменяется атомарно), очередь — через flock проекта и семафор заливок.
    from . import refresh
    refresh.spawn_background()
    mcp.run()


if __name__ == "__main__":
    main()
