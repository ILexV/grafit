"""grafit hook install (замена старых вариантов блока, идемпотентность) и план refresh."""
import os
import pytest
from grafit import common, refresh

GRAPHIFY = "#!/bin/sh\n# graphify-hook-start\necho graphify\n# graphify-hook-end\n"
BLOCK = refresh.hook_block("/opt/bin/grafit")

OLD_MARKED = GRAPHIFY + """
# --- grafit-hook-start (после graphify-блока) ---
[ "${GRAFIT_SKIP_HOOK:-0}" = "1" ] && exit 0
grafit load
# --- grafit-hook-end ---
"""
OLD_SHORT = GRAPHIFY + "# grafit-hook-start\nnohup grafit load &\n# grafit-hook-end\necho tail\n"
OLD_LEGACY = GRAPHIFY + """
# grafit: после того как graphify допишет graph.json — залить граф в FalkorDB.
if command -v grafit >/dev/null 2>&1; then
    nohup sh -c '
        ulimit -v 8000000 2>/dev/null; grafit load
    ' >>~/.cache/grafit-load.log 2>&1 </dev/null &
fi
"""


@pytest.mark.parametrize("old", [OLD_MARKED, OLD_SHORT, OLD_LEGACY], ids=["marked", "short", "legacy"])
def test_install_replaces_old_variants(old):
    new, n, loose = refresh.install_into(old, BLOCK)
    assert n == 1 and loose == []
    assert new.count("grafit-hook-start") == 1 and "grafit load" not in new
    assert new.startswith(GRAPHIFY)                        # graphify-блок не тронут
    assert new.index("graphify-hook-end") < new.index("grafit-hook-start")
    again, n2, _ = refresh.install_into(new, BLOCK)
    assert again == new and n2 == 1                         # идемпотентно


def test_install_keeps_tail_after_block():
    new, _, _ = refresh.install_into(OLD_SHORT, BLOCK)
    assert new.rstrip().endswith("echo tail")


def test_install_into_empty_and_without_graphify():
    new, n, _ = refresh.install_into("", BLOCK)
    assert new.startswith("#!/bin/sh") and "grafit-hook-start" in new and n == 0
    new, _, _ = refresh.install_into("#!/bin/sh\necho x\n", BLOCK)
    assert new.index("echo x") < new.index("grafit-hook-start")


def test_loose_grafit_load_reported():
    _, _, loose = refresh.install_into(GRAPHIFY + "grafit load\n", BLOCK)
    assert loose and "grafit load" in loose[0]


def test_unclosed_block_refused():
    with pytest.raises(ValueError):
        refresh.strip_blocks(GRAPHIFY + "# grafit-hook-start\ngrafit load\n")


def test_block_is_valid_shell(tmp_path):
    p = tmp_path / "h.sh"
    p.write_text(refresh.install_into(OLD_MARKED, BLOCK)[0])
    assert os.system(f"sh -n {p}") == 0


def test_graph_json_outdated(tmp_path):
    gj = tmp_path / "graph.json"
    assert refresh.graph_json_outdated(tmp_path, gj, [])          # нет graph.json
    (tmp_path / "a.py").write_text("x")
    gj.write_text("{}")
    os.utime(gj, (2_000_000_000, 2_000_000_000))
    assert not refresh.graph_json_outdated(tmp_path, gj, ["a.py"])
    os.utime(tmp_path / "a.py", (2_000_000_100, 2_000_000_100))
    assert refresh.graph_json_outdated(tmp_path, gj, ["a.py"])
    assert refresh.graph_json_outdated(tmp_path, gj, ["deleted.py"])


# --- узлы из игнорируемых файлов (hr: 247 узлов .scratch/*.md после refresh) ---

def test_parse_ignore_line_strips_inline_comment_like_graphify():
    assert common.parse_ignore_line(".scratch/          # черновики спек") == ".scratch/"
    assert common.parse_ignore_line("# весь комментарий") == ""
    assert common.parse_ignore_line("path#with#hash.py") == "path#with#hash.py"
    assert common.parse_ignore_line("a\\#b  ") == "a#b"


def test_hr_scratch_with_inline_comment_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFIT_DEFAULT_IGNORE", "")           # только шаблон проекта
    (tmp_path / ".graphifyignore").write_text(".scratch/          # тикеты автопилота\n")
    pats = common.load_ignore_patterns(tmp_path)
    assert common.is_ignored(".scratch/ticket/spec.md", pats)


def test_default_ignore_and_negation(tmp_path, monkeypatch):
    monkeypatch.delenv("GRAFIT_DEFAULT_IGNORE", raising=False)
    assert common.is_ignored(".autopilot/state.js", common.load_ignore_patterns(tmp_path))
    (tmp_path / ".graphifyignore").write_text("!.scratch/\n")  # проект возвращает каталог
    pats = common.load_ignore_patterns(tmp_path)
    assert not common.is_ignored(".scratch/a.md", pats) and common.is_ignored(".autopilot/b.sql", pats)
    assert common.load_ignore_patterns(tmp_path, defaults=False) == ["!.scratch/"]


def test_load_drops_nodes_and_edges_of_ignored_files(tmp_path):
    from grafit import index
    nodes = [{"id": "doc", "source_file": ".scratch/t/spec.md"},
             {"id": "abs", "source_file": str(tmp_path / ".scratch/t/x.md")},
             {"id": "code", "source_file": "src/a.ts"}, {"id": "pkg"}]
    links = [{"source": "code", "target": "doc"}, {"source": "code", "target": "pkg"}]
    keep, kl, dropped = index.drop_ignored(tmp_path, nodes, links, [".scratch/"])
    assert [n["id"] for n in keep] == ["code", "pkg"] and dropped == 2
    assert kl == [{"source": "code", "target": "pkg"}]


def test_ignore_change_makes_graph_stale_without_graphify_force(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "META_PATH", tmp_path / "meta.json")
    monkeypatch.delenv("GRAFIT_DEFAULT_IGNORE", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    common.save_meta("proj", commit="abc", root=str(root))     # залит до учёта игнора
    assert refresh.ignore_changed("proj", root)
    common.save_meta("proj", ignore_sha=common.ignore_sha(common.load_ignore_patterns(root)))
    assert not refresh.ignore_changed("proj", root)
    (root / ".graphifyignore").write_text(".scratch/\n")
    assert refresh.ignore_changed("proj", root)
    monkeypatch.setattr(common, "graph_freshness", lambda *a: {"state": "fresh", "code_changed": []})
    p = refresh.plan("proj", root)
    assert p["stale"] and not p["force"] and "шаблоны игнора" in p["why"]


# --- graphify и невалидный UTF-8 в именах файлов (bi: '\\udcd1') ---

def test_non_utf8_paths_found(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.ts").write_text("x")
    open(bytes(tmp_path / "src") + b"/\xd0\xa1\xd0.json", "wb").write(b"{}")
    (tmp_path / "node_modules").mkdir()
    open(bytes(tmp_path / "node_modules") + b"/\xd0.js", "wb").write(b"")
    assert refresh.non_utf8_paths(tmp_path) == [b"src/\xd0\xa1\xd0.json"]


def test_graphify_update_wraps_when_bad_paths(tmp_path, monkeypatch):
    py = tmp_path / "python3"
    py.write_text("")
    exe = tmp_path / "graphify"
    exe.write_text(f"#!{py}\nimport sys\n")
    monkeypatch.setattr(refresh.shutil, "which", lambda n: str(exe))
    open(bytes(tmp_path) + b"/\xd0.json", "wb").write(b"{}")
    seen = []
    monkeypatch.setattr(refresh.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or type("R", (), {"returncode": 0})())
    assert refresh.graphify_update(tmp_path)
    cmd = seen[0]
    assert cmd[0] == str(py) and cmd[1] == "-c" and "UnicodeEncodeError" in cmd[2]
    assert cmd[3:] == ["update", "."]


def test_skip_bad_paths_bootstrap_filters_detect():
    import types, sys as _sys
    fake = types.ModuleType("graphify.detect")
    fake.detect = lambda *a, **k: {"files": {"code": ["ok.ts", "bad\udcd0.json"]}}
    got = {}
    main_mod = types.ModuleType("graphify.__main__")
    main_mod.main = lambda: got.setdefault("files", fake.detect()["files"]["code"])
    pkg = types.ModuleType("graphify")
    mods = {"graphify": pkg, "graphify.detect": fake, "graphify.__main__": main_mod}
    saved = {k: _sys.modules.get(k) for k in mods}
    argv = list(_sys.argv)
    try:
        _sys.modules.update(mods)
        pkg.detect = fake
        exec(refresh._SKIP_BAD_PATHS, {})
    finally:
        _sys.argv[:] = argv
        for k, v in saved.items():
            if v is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = v
    assert got["files"] == ["ok.ts"]


def test_load_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HOME", tmp_path)
    monkeypatch.setenv("GRAFIT_MAX_LOADS", "2")
    import fcntl
    held = open(tmp_path / "load.lock", "w")           # слот 0 занят «чужой» заливкой
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert common.acquire_load_lock(wait=False)    # взяли слот 1
        assert common._LOAD_LOCK_FD.name.endswith("load.lock.1")
        common.release_load_lock()
        monkeypatch.setenv("GRAFIT_MAX_LOADS", "1")
        assert not common.acquire_load_lock(wait=False)
    finally:
        held.close()


# --- фоновый refresh при старте MCP ---

@pytest.fixture
def meta_home(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HOME", tmp_path)
    monkeypatch.setattr(common, "META_PATH", tmp_path / "meta.json")
    monkeypatch.setattr(common, "project_root", lambda *a: tmp_path / "proj")
    monkeypatch.setattr(refresh.Path, "home", staticmethod(lambda: tmp_path))
    calls = []
    monkeypatch.setattr(refresh.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or "proc")
    (tmp_path / "proj").mkdir()
    return tmp_path, calls


def test_spawn_background_only_for_known_project_and_debounced(meta_home, monkeypatch):
    tmp, calls = meta_home
    assert refresh.spawn_background() is None and not calls     # проекта нет в meta.json
    common.save_meta("proj", commit="abc", root=str(tmp / "proj"))
    assert refresh.spawn_background() == "proc"
    cmd, kw = calls[0]
    assert cmd[-2:] == ["refresh", str(tmp / "proj")] and kw["start_new_session"]
    assert refresh.spawn_background() is None and len(calls) == 1   # debounce
    assert refresh.spawn_background(debounce=0) == "proc"


def test_spawn_background_disabled_by_env(meta_home, monkeypatch):
    tmp, calls = meta_home
    common.save_meta("proj", commit="abc", root=str(tmp / "proj"))
    monkeypatch.setenv("GRAFIT_MCP_REFRESH", "0")
    assert refresh.spawn_background() is None and not calls


def test_refresh_takes_project_lock_before_graphify(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HOME", tmp_path)
    monkeypatch.setattr(common, "project_root", lambda *a: tmp_path)
    monkeypatch.setattr(refresh, "plan", lambda *a, **k: {
        "stale": True, "why": "код", "graphify": True, "force": False})
    monkeypatch.setattr(common, "become_active_loader", lambda name: False)
    ran = []
    monkeypatch.setattr(refresh, "graphify_update", lambda *a, **k: ran.append(1))
    out = refresh.refresh_project(tmp_path)
    assert "уже идёт" in out and not ran and common.rerun_pending(common.graph_name(None, tmp_path))


# --- догоняющий проход (rerun): не теряется и пересобирает graph.json ---

@pytest.fixture
def loader(tmp_path, monkeypatch):
    from grafit import index
    monkeypatch.setattr(common, "HOME", tmp_path)
    monkeypatch.setattr(common, "acquire_load_lock", lambda *a, **k: True)
    monkeypatch.setattr(common, "release_load_lock", lambda: None)
    passes = []
    monkeypatch.setattr(index, "index_project", lambda **kw: passes.append(kw.get("graph")))
    yield passes
    common.release_project_lock()


def test_rerun_requested_during_load_runs_again(loader):
    hook = []

    def prepare():
        hook.append(1)
        if len(hook) == 1:
            common.request_rerun("p")          # триггер пришёл, пока шла заливка
    assert refresh.load_coalesced("p", prepare=prepare)
    assert loader == ["p", "p"] and len(hook) == 2 and not common.rerun_pending("p")


def test_rerun_arriving_at_lock_release_not_lost(loader, monkeypatch):
    # окно гонки: активный проверил «пометок нет», и ровно тут пришёл триггер
    real = common.release_project_lock
    fired = []

    def release():
        real()
        if not fired:
            fired.append(1)
            common.request_rerun("p")
    monkeypatch.setattr(common, "release_project_lock", release)
    assert refresh.load_coalesced("p")
    assert loader == ["p", "p"] and not common.rerun_pending("p")


def test_busy_project_leaves_rerun_mark_and_releases_lock_after(loader):
    other = open(common.HOME / "load.p.lock", "a+")
    import fcntl
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert not refresh.load_coalesced("p") and common.rerun_pending("p") and loader == []
    finally:
        other.close()
    assert refresh.load_coalesced("p") and loader == ["p"]
    assert common._PROJECT_LOCK_FD is None            # замок отпущен, а не держится до exit


def test_refresh_rerun_pass_runs_graphify_when_code_newer(tmp_path, monkeypatch, loader):
    monkeypatch.setattr(common, "project_root", lambda *a: tmp_path)
    monkeypatch.setattr(refresh, "plan", lambda *a, **k: {
        "stale": True, "why": "код", "graphify": True, "force": False})
    monkeypatch.setattr(common, "graph_freshness", lambda *a: {"code_changed": ["a.py"]})
    monkeypatch.setattr(refresh, "graph_json_outdated", lambda *a: True)
    ran = []

    def gupd(root, force=False):
        ran.append(force)
        if len(ran) == 1:
            common.request_rerun(common.graph_name(None, tmp_path))
    monkeypatch.setattr(refresh, "graphify_update", gupd)
    out = refresh.refresh_project(tmp_path)
    assert "залит" in out and len(ran) == 2 and len(loader) == 2


# --- совет «держи хук локальным» — только для видимого git хука в рабочем дереве ---

def _repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_hook_tip_silent_for_hook_inside_dot_git(tmp_path):
    root = _repo(tmp_path)
    hook = root / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\n")
    assert refresh.hook_tip(root, hook) is None


def test_hook_tip_for_custom_hooks_path_until_excluded(tmp_path):
    root = _repo(tmp_path)
    hook = root / ".githooks" / "post-commit"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\n")
    tip = refresh.hook_tip(root, hook)
    assert tip and ".githooks/post-commit >> .git/info/exclude" in tip
    with open(root / ".git" / "info" / "exclude", "a") as f:
        f.write(".githooks/post-commit\n")
    assert refresh.hook_tip(root, hook) is None          # уже исключён — совет лишний


def test_hook_tip_warns_when_tracked(tmp_path):
    import subprocess
    root = _repo(tmp_path)
    hook = root / ".githooks" / "post-commit"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\n")
    subprocess.run(["git", "-C", str(root), "add", ".githooks/post-commit"], check=True)
    assert "отслеживается" in refresh.hook_tip(root, hook)


def test_hook_install_backup_goes_to_dot_git_not_worktree(tmp_path):
    """core.hooksPath=.githooks: бэкап старого хука — в .git/grafit/, рабочее дерево чистое."""
    import subprocess
    root = _repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "config", "core.hooksPath", ".githooks"], check=True)
    hook = root / ".githooks" / "post-commit"
    hook.parent.mkdir()
    hook.write_text(OLD_MARKED)
    with open(root / ".git" / "info" / "exclude", "a") as f:
        f.write(".githooks/post-commit\n")
    refresh.hook_install(root)
    assert not list((root / ".githooks").glob("*bak*"))
    assert (root / ".git" / "grafit" / "post-commit.bak").read_text() == OLD_MARKED
    st = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                        capture_output=True, text=True, check=True).stdout
    assert st == ""
