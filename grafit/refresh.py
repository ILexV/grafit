"""Автообновление графов: `grafit refresh` (graphify update без LLM → load) и
`grafit hook install` (фоновый refresh в post-commit после graphify-блока).

Конкуренцию держат существующие замки: per-project flock + один отложенный rerun
(«ровно один догоняющий проход на проект») и глобальный семафор заливок (GRAFIT_MAX_LOADS).
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path

from . import common

log = common.log


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --- заливка с coalescing (общая для `grafit load` и refresh) ---

def load_coalesced(name: str, already_active: bool = False, **index_kw) -> bool:
    """`grafit load` с «ровно одним отложенным проходом на проект»: если заливка этого графа
    уже идёт — не плодим второй процесс и не убиваем активный, лишь помечаем rerun (False).
    Активный загрузчик после своего прохода видит пометку и делает ОДИН догоняющий проход."""
    from . import index
    if not already_active and not common.become_active_loader(name):
        common.request_rerun(name)
        log(f"[grafit] заливка '{name}' уже идёт — отложен один повторный проход (rerun)")
        return False
    while True:
        common.take_rerun(name)          # снимаем pending: грузим текущее состояние графа
        index.index_project(graph=name, **index_kw)
        common.release_load_lock()       # отпускаем глобальный слот — даём ход другим проектам
        if not common.rerun_pending(name):
            return True
        log(f"[grafit] во время заливки '{name}' пришли изменения — ещё один проход")


# --- что нужно обновить ---

def ignored_in_graph(root: Path, gj: Path) -> list[str]:
    """Файлы КОДА, чьи узлы есть в graph.json, хотя файл уже под .graphifyignore: инкрементальная
    пересборка graphify их не убирает — нужна полная (graphify update --force). Доки не в счёт:
    их узлы из LLM-извлечения, `graphify update` их не пересобирает (вечный --force впустую)."""
    pats = common.load_ignore_patterns(root)
    if not pats or not gj.exists():
        return []
    try:
        nodes = json.loads(gj.read_text(encoding="utf-8")).get("nodes") or []
    except Exception:
        return []
    files = {n.get("source_file") for n in nodes if n.get("source_file")}
    out = []
    for sf in files:
        rel = os.path.relpath(sf, root) if os.path.isabs(sf) else sf
        if (not rel.startswith("..") and Path(rel).suffix in common.CODE_EXTENSIONS
                and common.is_ignored(rel, pats)):
            out.append(rel)
    return sorted(out)


def graph_json_outdated(root: Path, gj: Path, code_changed: list[str] | None) -> bool:
    """graph.json старше изменённых файлов кода → graphify его ещё не пересобрал."""
    if not gj.exists():
        return True
    if code_changed is None:
        return True
    gm = gj.stat().st_mtime
    for rel in code_changed:
        p = root / rel
        try:
            if p.stat().st_mtime > gm:
                return True
        except OSError:
            return True          # файл удалён — его узлы ещё в графе
    return False


def wait_graph_rebuild(gj: Path, base: float, timeout: float, settle: float = 6.0) -> bool:
    """Ждать, пока graphify-хук перепишет graph.json (mtime > base) и файл перестанет меняться
    settle секунд. False — не дождались за timeout (graphify упал/не запускался)."""
    t0 = time.time()
    prev, stable_since = None, None
    while time.time() - t0 < timeout:
        try:
            m = gj.stat().st_mtime
        except OSError:
            m = 0
        if m > base:
            if m == prev:
                if stable_since and time.time() - stable_since >= settle:
                    return True
            else:
                stable_since = time.time()
            prev = m
        time.sleep(2)
    return False


def graphify_update(root: Path, force: bool = False, no_cluster: bool = False) -> bool:
    """`graphify update .` — пере-экстракция КОДА без LLM. PYTHONHASHSEED=0 — детерминированная
    кластеризация (как в graphify-хуке), иначе метки сообществ «плавают» от прогона к прогону."""
    exe = shutil.which("graphify")
    if not exe:
        log("[grafit refresh] graphify не найден в PATH — пропускаю пересборку graph.json")
        return False
    cmd = [exe, "update", "."] + (["--force"] if force else []) + (["--no-cluster"] if no_cluster else [])
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    log(f"[grafit refresh] {root}: {' '.join(cmd[1:])}")
    r = subprocess.run(cmd, cwd=str(root), env=env)
    if r.returncode != 0:
        log(f"[grafit refresh] graphify update завершился с кодом {r.returncode}")
    return r.returncode == 0


def plan(name: str, root: Path, min_commits: int = 1) -> dict:
    """Нужно ли обновлять граф проекта и как: {stale, why, graphify, force, f}."""
    f = common.graph_freshness(name, root)
    gj = root / "graphify-out" / "graph.json"
    ign = ignored_in_graph(root, gj)
    stale = common.is_stale(f, min_commits) or bool(ign) or f.get("reason") == "no_meta"
    code = f.get("code_changed")
    why = []
    if f.get("reason") == "no_meta":
        why.append("нет метаданных сборки")
    if code:
        why.append(f"изменено файлов кода: {len(code)}")
    elif code is None and f["state"] not in ("fresh", "unknown"):
        why.append(f"состояние {f['state']}, список изменений недоступен")
    if ign:
        why.append(f"в графе узлы из игнорируемых файлов: {len(ign)} (нужна полная пересборка)")
    return {"stale": stale, "why": "; ".join(why) or "актуален", "f": f, "ignored": ign,
            "graphify": bool(ign) or graph_json_outdated(root, gj, code), "force": bool(ign)}


def refresh_project(root: Path, *, min_commits: int = 1, after_hook: bool = False,
                    skip_graphify: bool = False, dry_run: bool = False, wait_timeout: float = 660,
                    index_kw: dict | None = None) -> str:
    """Обновить граф одного проекта. after_hook — вызов из post-commit: graph.json пересобирает
    graphify-хук, мы только ждём его и заливаем (сами graphify не запускаем — не гоняться)."""
    root = common.project_root(root)
    name = common.graph_name(None, root)
    p = plan(name, root, min_commits)
    if not p["stale"]:
        return f"{name}: {p['why']} — пропуск"
    if dry_run:
        act = ("graphify update" + (" --force" if p["force"] else "") + " + load") if p["graphify"] else "load"
        return f"{name}: {p['why']} → {act}"
    gj = root / "graphify-out" / "graph.json"
    if after_hook:
        # База — время коммита: graph.json новее него ⇒ уже пересобран после этого коммита.
        try:
            base = float(subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%ct"],
                                        capture_output=True, text=True).stdout.strip()) - 1
        except ValueError:
            base = time.time() - 5
        if not wait_graph_rebuild(gj, base, wait_timeout):
            log(f"[grafit refresh] {name}: graph.json не пересобран за {int(wait_timeout)}s — заливаю как есть")
    kw = {"project": str(root), "host": "localhost", "port": 6399}
    kw.update(index_kw or {})
    if after_hook or not p["graphify"] or skip_graphify:
        done = load_coalesced(name, **kw)
    else:
        # graphify update — под тем же замком проекта, что и load: два refresh одного проекта
        # (несколько чатов стартовали MCP) не пишут graph.json одновременно
        if not common.become_active_loader(name):
            common.request_rerun(name)
            return f"{name}: обновление уже идёт — поставлен rerun ({p['why']})"
        graphify_update(root, force=p["force"])
        done = load_coalesced(name, already_active=True, **kw)
    return f"{name}: {'залит' if done else 'заливка уже идёт — поставлен rerun'} ({p['why']})"


# --- фоновый refresh при старте MCP-сервера ---

DEBOUNCE_S = 600


def spawn_background(root=None, debounce: float = DEBOUNCE_S):
    """Запустить `grafit refresh <проект>` отдельным процессом и не ждать (старт `grafit mcp`).

    Только для проекта, уже залитого в grafit (есть в meta.json): незнакомый каталог не
    индексируем. Не чаще раза в debounce секунд на проект — несколько чатов, открытых разом,
    не плодят процессы. Отключение: GRAFIT_MCP_REFRESH=0. → Popen или None."""
    if os.environ.get("GRAFIT_MCP_REFRESH", "1") == "0":
        return None
    try:
        root = common.project_root(root)
        name = common.graph_name(None, root)
        if name not in common.load_meta():
            return None
        stamp = common.HOME / f"refresh.{common.sanitize(name)}.stamp"
        if stamp.exists() and time.time() - stamp.stat().st_mtime < debounce:
            return None
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        logp = Path.home() / ".cache" / "grafit-refresh.log"
        logp.parent.mkdir(parents=True, exist_ok=True)
        out = open(logp, "a", encoding="utf-8")
        cmd = [sys.executable, "-m", "grafit.cli", "refresh", str(root)]
        return subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=str(root), start_new_session=True, close_fds=True)
    except Exception as ex:          # фон — best effort: MCP-сервер должен стартовать всегда
        log(f"[grafit mcp] фоновый refresh не запущен: {ex!r}")
        return None


def refresh_all_stale(min_commits: int = 1, dry_run: bool = False, index_kw: dict | None = None):
    """Все графы из meta.json, отставшие по коду (по одному, последовательно)."""
    for name, m in sorted(common.load_meta().items()):
        root = Path(m.get("root") or "")
        if not m.get("root") or not root.is_dir():
            log(f"{name}: корень {m.get('root')!r} не найден — пропуск")
            continue
        try:
            yield refresh_project(root, min_commits=min_commits, dry_run=dry_run, index_kw=index_kw)
        except SystemExit as ex:            # index.* зовёт sys.exit на фатальных ошибках проекта
            yield f"{name}: ошибка — {ex}"
        except Exception as ex:
            yield f"{name}: ошибка — {ex!r}"


# --- post-commit хук ---

_START_RE = re.compile(r"^\s*#.*grafit-hook-start")
_END_RE = re.compile(r"^\s*#.*grafit-hook-end")
_GRAPHIFY_END_RE = re.compile(r"^\s*#\s*graphify-hook-end")
_LOOSE_RE = re.compile(r"\bgrafit\s+(load|refresh)\b")


def hook_block(grafit_bin: str) -> str:
    """grafit-блок post-commit: фоновый `grafit refresh --after-hook` (ждёт graphify, заливает).
    Дубли гасит сам grafit (flock на проект + один rerun) — pgrep-проверки не нужны."""
    return f"""# --- grafit-hook-start (ставит `grafit hook install`; после graphify-блока) ---
# Фоном: дождаться пересборки graph.json graphify-хуком → залить в FalkorDB (grafit refresh).
# Дубли гасит сам grafit (flock на проект + один отложенный rerun); коммит не ждёт.
if [ "${{GRAFIT_SKIP_HOOK:-0}}" != "1" ]; then
    _GRAFIT_BIN='{grafit_bin}'
    [ -x "$_GRAFIT_BIN" ] || _GRAFIT_BIN=$(command -v grafit 2>/dev/null)
    if [ -n "$_GRAFIT_BIN" ]; then
        _GRAFIT_LOG="${{HOME}}/.cache/grafit-refresh.log"
        mkdir -p "$(dirname "$_GRAFIT_LOG")"
        _GRAFIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
        echo "[grafit hook] фоновый grafit refresh (log: $_GRAFIT_LOG)"
        ( ulimit -v 8000000 2>/dev/null; exec nohup nice -n 10 "$_GRAFIT_BIN" refresh "$_GRAFIT_ROOT" --after-hook ) \\
            >>"$_GRAFIT_LOG" 2>&1 </dev/null &
    fi
fi
# --- grafit-hook-end ---
"""


def hooks_dir(root: Path) -> Path:
    """Каталог хуков с учётом core.hooksPath (относительного — от корня worktree — и абсолютного)."""
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
                         capture_output=True, text=True, check=True).stdout.strip()
    p = Path(out)
    return p if p.is_absolute() else (root / p)


_LEGACY_IF_RE = re.compile(r"^if\s+command\s+-v\s+grafit\b")
_LEGACY_NOTE_RE = re.compile(r"^#.*\bgrafit\b", re.I)


def strip_blocks(text: str) -> tuple[str, int]:
    """Убрать все grafit-блоки: размеченные (`#…grafit-hook-start` … `#…grafit-hook-end`, любые
    варианты маркеров) и старый неразмеченный (`if command -v grafit …` … `fi` верхнего уровня
    вместе с комментарием `# grafit…` над ним). Незакрытый блок — ValueError: не режем вслепую."""
    out, n, inside, legacy = [], 0, False, False
    for ln in text.splitlines(keepends=True):
        if legacy:
            if re.match(r"^fi\s*$", ln):
                legacy = False
            continue
        if not inside and _START_RE.match(ln):
            inside, n = True, n + 1
            continue
        if inside:
            if _END_RE.match(ln):
                inside = False
            continue
        if _LEGACY_IF_RE.match(ln):
            legacy, n = True, n + 1
            while out and _LEGACY_NOTE_RE.match(out[-1]) and "graphify-hook-end" not in out[-1]:
                out.pop()
            continue
        out.append(ln)
    if inside or legacy:
        raise ValueError("grafit-блок без конца (grafit-hook-end / fi) — поправь хук вручную")
    body = "".join(out)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body, n


def install_into(text: str, block: str) -> tuple[str, int, list[str]]:
    """Текст хука с ОДНИМ актуальным grafit-блоком сразу после graphify-блока (или в конце).
    → (новый текст, сколько старых блоков заменено, строки с grafit load/refresh вне маркеров)."""
    if not text.strip():
        text = "#!/bin/sh\n"
    body, replaced = strip_blocks(text)
    lines = body.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    loose = [f"L{i}: {ln.strip()}" for i, ln in enumerate(lines, 1)
             if _LOOSE_RE.search(ln) and not ln.lstrip().startswith("#")]
    idx = next((i for i, ln in enumerate(lines) if _GRAPHIFY_END_RE.match(ln)), None)
    if idx is None:
        new = "".join(lines).rstrip("\n") + "\n\n" + block
    else:
        head, tail = "".join(lines[:idx + 1]), "".join(lines[idx + 1:]).lstrip("\n")
        new = head + "\n" + block + ("\n" + tail if tail else "")
    return new, replaced, loose


def hook_tip(root: Path, hook: Path) -> str | None:
    """Совет про локальность хука — только если он в РАБОЧЕМ ДЕРЕВЕ (кастомный core.hooksPath
    вроде .githooks) и git его видит: хуки в .git/ git не отслеживает никогда, а уже
    игнорируемый (.git/info/exclude, .gitignore) хук и так локален."""
    if not hook.exists():
        return None
    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)
    try:
        h, r = hook.resolve(), root.resolve()
        gdirs = [Path(git("rev-parse", flag).stdout.strip()).resolve()
                 for flag in ("--absolute-git-dir", "--git-common-dir")]
        gdirs = [d if d.is_absolute() else (r / d) for d in gdirs]
        if not h.is_relative_to(r) or any(h.is_relative_to(d) for d in gdirs):
            return None
    except Exception:
        return None
    if git("ls-files", "--error-unmatch", str(h)).returncode == 0:
        return "  ⚠ хук отслеживается git — не коммить grafit-блок (он личный)"
    if git("check-ignore", "-q", str(h)).returncode == 0:
        return None
    rel = os.path.relpath(h, r)
    return f"  хук внутри репо: держи локальным — `echo {rel} >> .git/info/exclude`"


def hook_install(root: Path, uninstall: bool = False) -> list[str]:
    root = common.project_root(root)
    hd = hooks_dir(root)
    hook = hd / "post-commit"
    old = hook.read_text(encoding="utf-8") if hook.exists() else ""
    if uninstall:
        new, n = strip_blocks(old)
        loose = []
        msg = [f"{hook}: удалено grafit-блоков: {n}"]
    else:
        gbin = shutil.which("grafit") or str(Path(sys.argv[0]).resolve())
        new, n, loose = install_into(old, hook_block(gbin))
        msg = [f"{hook}: grafit-блок {'обновлён (заменено старых: ' + str(n) + ')' if n else 'установлен'}"]
        if "graphify-hook-end" not in old:
            msg.append("  ⚠ graphify-блока нет — graph.json хук не пересоберёт: `graphify hook install`")
    if new != old:
        hd.mkdir(parents=True, exist_ok=True)
        if old and not (hd / "post-commit.grafit-bak").exists():
            (hd / "post-commit.grafit-bak").write_text(old, encoding="utf-8")
        hook.write_text(new, encoding="utf-8")
        hook.chmod(hook.stat().st_mode | 0o111)
    else:
        msg.append("  без изменений")
    for ln in loose:
        msg.append(f"  ⚠ вне маркеров осталось: {ln} — убери вручную, иначе заливка дважды")
    tip = hook_tip(root, hook)
    if tip:
        msg.append(tip)
    return msg
