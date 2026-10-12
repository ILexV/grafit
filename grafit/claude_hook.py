"""Хук Claude Code (PostToolUse на Write/Edit/MultiEdit): «похоже на существующее» для НОВЫХ
объявлений функций/классов в правке — антидубли в момент написания, а не на приёмке.

Молчит, если новых объявлений нет или вероятных дублей (dist≤0.10, как у similar) не нашлось.
Fail-open: любой сбой (нет графа, сервиса эмбеддингов, FalkorDB, вышел бюджет ~3 с) — exit 0
без вывода, правку никогда не блокирует. Эмбеддер только общий сервис grafit-embed: грузить
модель локально в хуке слишком долго. Установка opt-in по проекту: `grafit hook install --claude`.
"""
from __future__ import annotations
import json, os, re, shutil, signal, subprocess, sys, time
from pathlib import Path

TOOLS = {"Write", "Edit", "MultiEdit"}
BUDGET_S = float(os.environ.get("GRAFIT_HOOK_BUDGET", "3"))
MAX_DECLS = 3                 # проверяем не больше стольких новых объявлений за правку
EMBED_EST_S = 1.0             # оценка эмбеддинга одного объявления (e5-large на CPU ≈0.9 с)
MARGIN_S = 0.3                # запас до бюджета на запрос в граф и вывод
MAX_CANDS = 3                 # и показываем не больше стольких кандидатов
THRESHOLD = 0.10              # тот же порог «вероятный дубль», что у grafit_similar
MARK = "claude-hook"          # по нему находим свою запись в settings.local.json


class _Timeout(Exception):
    pass


def new_declarations(event: dict) -> tuple[str, list[str]]:
    """(путь файла, имена объявлений, которых до правки не было). Edit/MultiEdit — объявления
    new_string минус old_string; Write — содержимое минус прежняя версия файла (originalFile
    из ответа инструмента, иначе HEAD)."""
    from . import decls
    tool = event.get("tool_name")
    ti = event.get("tool_input") or {}
    path = ti.get("file_path") or ""
    if tool not in TOOLS or not path or not decls.supported(path):
        return path, []
    if tool == "Edit":
        names = decls.new_names(path, ti.get("old_string") or "", ti.get("new_string") or "")
    elif tool == "MultiEdit":
        names = set()
        for e in ti.get("edits") or []:
            names |= decls.new_names(path, e.get("old_string") or "", e.get("new_string") or "")
    else:
        old = (event.get("tool_response") or {}).get("originalFile")
        if not isinstance(old, str):
            old = _head_version(path)
        names = decls.new_names(path, old, ti.get("content") or "")
    return path, sorted(names)


def _head_version(path: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(Path(path).parent), "show", f"HEAD:./{Path(path).name}"],
                           capture_output=True, text=True, timeout=1)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _remote_embedder(cfg: dict, timeout: float):
    """Только общий сервис эмбеддингов с той же моделью/версией fastembed, что у индекса;
    иначе None (локальная модель грузится десятки секунд — не для хука)."""
    from . import common
    url = common.embed_url()
    if not url:
        return None
    health = common._probe_embed(url, timeout=min(1.0, timeout))
    if not health or health.get("model") != cfg.get("model"):
        return None
    if cfg.get("fastembed") and health.get("fastembed") and health["fastembed"] != cfg["fastembed"]:
        return None
    return common.RemoteEmbedder(url, cfg["model"], timeout=timeout)


def _line(loc: str | None) -> str:
    m = re.search(r"\d+", loc or "")
    return m.group(0) if m else "?"


def find_candidates(path: str, names: list[str], deadline: float) -> tuple[str | None, list]:
    """(граф, [(новое имя, rel:строка, label, sf, loc, dist)]) — вероятные дубли новых
    объявлений, лучшие MAX_CANDS по дистанции. Граф None — проект не проиндексирован."""
    from . import common, decls, dupes
    root = common.project_root(Path(path).parent)
    explicit = os.environ.get("GRAFIT_GRAPH")      # копия/worktree проекта → граф оригинала
    name = common.graph_name(explicit or None, root=root)
    if not (common.load_meta().get(name) or common.load_registry().get(name)):
        return None, []
    reg = common.load_registry().get(name)
    if not explicit and reg and Path(reg).resolve() != root.resolve():
        return None, []                   # одноимённый граф другого проекта
    cfg = common.load_config()
    if not cfg:
        return None, []
    rel = os.path.relpath(os.path.abspath(path), root)
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    want = set(names)
    found = [(n, ln) for n, ln in decls.declarations(rel, text) if n in want][:MAX_DECLS]
    cache = {}
    anchors = [a for n, ln in found if (a := dupes.worktree_text(root, rel, n, cache=cache, line=ln))]
    if not anchors:
        return name, []
    model = _remote_embedder(cfg, timeout=max(0.2, deadline - time.monotonic()))
    if model is None:
        return name, []
    pfx = "passage: " if cfg.get("is_e5") else ""
    g = common.connect(os.environ.get("GRAFIT_HOST", "localhost"),
                       int(os.environ.get("GRAFIT_PORT", "6399"))).select_graph(name)
    out, seen, est = [], set(), EMBED_EST_S
    for a in anchors:                     # по одному: эмбеддинг ≈1 с — что успели, то и покажем
        t = time.monotonic()
        if t + est > deadline - MARGIN_S:
            break
        v = next(iter(model.embed([pfx + a["text"]]))).tolist()
        est = time.monotonic() - t
        res = dupes.similar_to_text(g, v, a, k=MAX_CANDS, threshold=THRESHOLD)
        for _id, label, _ft, sf, loc, _text, dist, _j in res["candidates"]:
            if (sf, loc) not in seen:
                seen.add((sf, loc))
                out.append((a["label"], f"{rel}:{_line(a['loc'])}", label, sf, loc, dist))
    out.sort(key=lambda c: c[5])
    return name, out[:MAX_CANDS]


def render(cands: list, rel: str) -> str:
    """Блок для контекста модели: коротко, по новому символу — существующие путь:строка и score."""
    lines = ["grafit: похоже на существующее — проверь, переиспользуй вместо нового кода:"]
    last = None
    for new, where, label, sf, loc, dist in sorted(cands, key=lambda c: (c[1], c[5])):
        if (new, where) != last:
            lines.append(f"{new} ({where}):")
            last = (new, where)
        lines.append(f"  - {sf}:{_line(loc)} {label} (score {1 - dist:.2f})")
    lines.append(f"Подробнее: grafit_similar(file=\"{rel}\")")
    return "\n".join(lines)


def run(event: dict) -> tuple[str | None, dict]:
    """→ (текст для контекста модели или None, запись журнала или {} если нечего проверять)."""
    t0 = time.monotonic()
    path, names = new_declarations(event)
    if not names:
        return None, {}
    rec = {"tool": "claude_hook", "decls": len(names), "fired": False, "n": 0}
    try:
        gname, cands = find_candidates(path, names, t0 + BUDGET_S)
        rec.update(project=gname, n=len(cands), fired=bool(cands))
        if not cands:
            return None, rec
        rel = cands[0][1].rsplit(":", 1)[0]
        return render(cands, rel), rec
    except _Timeout:
        rec.update(ok=False, why="timeout")
        return None, rec
    except Exception as ex:               # нет FalkorDB/сервиса/файла — молчим, но в журнал
        rec.update(ok=False, why=type(ex).__name__)
        return None, rec
    finally:
        rec["ms"] = int((time.monotonic() - t0) * 1000)


def _log(rec: dict) -> None:
    if not rec or os.environ.get("GRAFIT_USAGE_LOG", "1") == "0":
        return
    try:
        from . import common
        rec = {"ts": common.now_iso(), **rec}
        p = common.HOME / "usage.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main(stdin=None, stdout=None) -> int:
    """Точка входа `grafit claude-hook`. Всегда 0; вывод — только JSON с additionalContext."""
    stdout = stdout or sys.stdout
    rec = {}
    use_alarm = hasattr(signal, "SIGALRM")
    try:
        if use_alarm:
            def on_alarm(*_):
                raise _Timeout()
            signal.signal(signal.SIGALRM, on_alarm)
            signal.setitimer(signal.ITIMER_REAL, BUDGET_S)
        event = json.loads((stdin or sys.stdin).read() or "{}")
        text, rec = run(event)
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if text:
            stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": text}}, ensure_ascii=False))
            stdout.flush()
    except BaseException:                 # fail-open: хук никогда не мешает правке
        rec = rec or {}
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
    _log(rec)
    return 0


# --- установка в .claude/settings.local.json проекта ------------------------------

def settings_path(root: Path) -> Path:
    return root / ".claude" / "settings.local.json"


def _is_ours(h) -> bool:
    return isinstance(h, dict) and MARK in str(h.get("command", "")) and "grafit" in str(h.get("command", ""))


def _strip_ours(entries: list) -> tuple[list, int]:
    """Убрать наши команды из записей PostToolUse; записи, где ничего не осталось, — тоже."""
    out, n = [], 0
    for e in entries:
        hs = e.get("hooks") if isinstance(e, dict) else None
        if not isinstance(hs, list):
            out.append(e)
            continue
        keep = [h for h in hs if not _is_ours(h)]
        n += len(hs) - len(keep)
        if keep:
            out.append({**e, "hooks": keep})
        elif len(hs) == len(keep):
            out.append(e)
    return out, n


def install(root: Path | None, uninstall: bool = False) -> list[str]:
    """Поставить/снять PostToolUse-хук в .claude/settings.local.json проекта (локально, не в
    git — как post-commit хук). Идемпотентно; чужие хуки и прочие настройки не трогает."""
    from . import common
    root = common.project_root(root)
    p = settings_path(root)
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    try:
        data = json.loads(old) if old.strip() else {}
    except json.JSONDecodeError as ex:
        raise ValueError(f"{p}: не JSON ({ex}) — поправь руками, файл не трогаю")
    if not isinstance(data, dict):
        raise ValueError(f"{p}: ожидался JSON-объект")
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    entries, n = _strip_ours(list(hooks.get("PostToolUse") or []))
    if not uninstall:
        gbin = shutil.which("grafit") or str(Path(sys.argv[0]).resolve())
        entries.append({"matcher": "Write|Edit|MultiEdit",
                        "hooks": [{"type": "command", "command": f"{gbin} {MARK}", "timeout": 5}]})
    if entries:
        hooks["PostToolUse"] = entries
    else:
        hooks.pop("PostToolUse", None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    new = json.dumps(data, indent=2, ensure_ascii=False) + "\n" if (data or old) else ""
    same = bool(old) and json.loads(old) == data
    if uninstall:
        msg = [f"{p}: снято grafit-хуков: {n}"]
    elif same:
        msg = [f"{p}: хук Claude Code уже установлен — без изменений"]
    else:
        msg = [f"{p}: хук Claude Code {'обновлён' if n else 'установлен'} "
               f"(PostToolUse Write|Edit|MultiEdit → grafit {MARK})"]
    if not same and (new or old):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new, encoding="utf-8")
    if not uninstall:
        r = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", str(p)],
                           capture_output=True)
        if r.returncode == 1:
            msg.append("  ⚠ .claude/settings.local.json не в .gitignore — добавь, чтобы хук "
                       "оставался локальным")
        msg.append("  действует в новых сессиях Claude Code; снять: grafit hook uninstall --claude")
    return msg
