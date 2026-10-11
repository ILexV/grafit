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


def test_ignored_in_graph_only_code(tmp_path):
    (tmp_path / ".graphifyignore").write_text("gen/\n.sonarqube/\n")
    gj = tmp_path / "graph.json"
    gj.write_text('{"nodes": [{"source_file": "gen/a.py"}, {"source_file": ".sonarqube/d.md"},'
                  ' {"source_file": "src/b.py"}]}')
    assert refresh.ignored_in_graph(tmp_path, gj) == ["gen/a.py"]


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
