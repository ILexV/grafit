"""Свежесть графа на временном git-репо: устаревание считается по изменённым файлам КОДА
(доки и .graphifyignore не в счёт), шапка мягкая, тревога — только при большом отставании."""
import subprocess
import pytest
from grafit import common


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})


def _commit(root, files: dict, msg="c"):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "META_PATH", tmp_path / "meta.json")
    monkeypatch.setattr(common, "HOME", tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _commit(root, {"src/app.py": "def run():\n    pass\n", "README.md": "# x\n",
                   ".graphifyignore": "gen/\n"})
    info = common.git_info(root)
    common.save_meta("proj", commit=info["commit"], short=info["short"], root=str(root))
    return root


def test_fresh(repo):
    f = common.graph_freshness("proj", repo)
    assert f["state"] == "fresh" and f["code_changed"] == []
    assert common.freshness_line("proj", repo) .endswith("свежий")


def test_docs_only_commit_is_not_stale(repo):
    _commit(repo, {"README.md": "# changed\n", "docs/new.md": "x\n"})
    f = common.graph_freshness("proj", repo)
    assert f["state"] == "behind" and f["behind"] == 1
    assert sorted(f["changed"]) == ["README.md", "docs/new.md"] and f["code_changed"] == []
    assert not common.is_stale(f)
    line = common.freshness_line("proj", repo)
    assert "код не менялся" in line and "⚠" not in line


def test_ignored_code_is_not_stale(repo):
    _commit(repo, {"gen/client.py": "x = 1\n"})
    f = common.graph_freshness("proj", repo)
    assert f["code_changed"] == [] and not common.is_stale(f)


def test_code_change_lists_files_softly(repo):
    _commit(repo, {"src/app.py": "def run():\n    return 1\n"})
    f = common.graph_freshness("proj", repo)
    assert f["code_changed"] == ["src/app.py"] and common.is_stale(f)
    assert not common.is_stale(f, min_commits=2)
    line = common.freshness_line("proj", repo)
    assert "src/app.py" in line and "остальное актуально" in line and "⚠" not in line


def test_uncommitted_code_change_counts(repo):
    (repo / "src/app.py").write_text("def run():\n    return 2\n")
    f = common.graph_freshness("proj", repo)
    assert f["state"] == "dirty" and f["code_changed"] == ["src/app.py"] and common.is_stale(f)


def test_big_lag_raises_alarm(repo, monkeypatch):
    monkeypatch.setattr(common, "ALARM_FILES", 2)
    _commit(repo, {f"src/m{i}.py": "x = 1\n" for i in range(3)})
    line = common.freshness_line("proj", repo)
    assert line.startswith("⚠") and "сильно отстал" in line and "grafit refresh" in line


def test_report_says_actual_when_only_docs(repo):
    _commit(repo, {"README.md": "# y\n"})
    rep = common.freshness_report("proj", repo)
    assert "кода графа 0" in rep and "граф актуален" in rep


@pytest.mark.parametrize("rel,pats,expected", [
    ("backend/obj/x.cs", ["**/obj/"], True),
    ("obj", ["**/obj/"], False),                 # dir-only шаблон не матчит файл
    ("a/b/c.py", ["/a/"], True),
    ("x/a/b.py", ["/a/"], False),                # привязан к корню
    ("x/a/b.py", ["a/b.py"], False),
    ("q/w.gen.cs", ["*.gen.cs", "!q/w.gen.cs"], False),   # отрицание, last-match-wins
    ("src/x.generated.cs", ["*.generated.cs"], True),
    ("./src/app.py", ["src/"], True),
])
def test_is_ignored(rel, pats, expected):
    assert common.is_ignored(rel, pats) is expected


def test_is_graph_code():
    assert common.is_graph_code("src/a.ts", [])
    assert not common.is_graph_code("docs/a.md", [])
    assert not common.is_graph_code("graphify-out/graph.json", [])
