"""Заход 1d, п.15: хук Claude Code «похоже на существующее» после Write/Edit: блок только на
НОВОЕ объявление с вероятным дублем, иначе тишина; fail-open при недоступном grafit;
установка в .claude/settings.local.json идемпотентна."""
import io
import json, subprocess
import pytest
from grafit import claude_hook as ch, common
from tests.fakes import FakeGraph

FMT = "frontend/src/utils/reportBuilderFormat.ts"
NEW = "frontend/src/utils/formatRub.ts"
RUB = ("// сумма в рублях\nexport function formatRubleAmount(v: number): string {\n"
       "  return new Intl.NumberFormat('ru-RU').format(v) + ' ₽';\n}\n")
HELPER = "\nexport function clampPercent(p: number): number {\n  return Math.min(100, p);\n}\n"


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                   check=True, capture_output=True)


# --- п.15: хук Claude Code --------------------------------------------------------

@pytest.fixture
def hook(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / NEW).parent.mkdir(parents=True)
    st = {"graph": FakeGraph([("n1", "numberFormatter()", "code", FMT, "L64", 0.06)]),
          "embedder": True, "root": tmp_path}
    name = common.graph_name(root=tmp_path)
    monkeypatch.setattr(common, "load_meta", lambda: {name: {"root": str(tmp_path)}})
    monkeypatch.setattr(common, "load_registry", lambda: {name: str(tmp_path)})
    monkeypatch.setattr(common, "load_config", lambda: {"model": "e5", "is_e5": True})

    class Emb:
        def embed(self, ts):
            import numpy as np
            return [np.zeros(2) for _ in ts]
    monkeypatch.setattr(ch, "_remote_embedder", lambda cfg, timeout: Emb() if st["embedder"] else None)

    class DB:
        def select_graph(self, n):
            if st["graph"] is None:
                raise ConnectionError("FalkorDB down")
            return st["graph"]
    monkeypatch.setattr(common, "connect", lambda h, p: DB())
    monkeypatch.setattr(common, "HOME", tmp_path / "home")
    return st


def _run(event):
    out = io.StringIO()
    assert ch.main(io.StringIO(json.dumps(event)), out) == 0
    return out.getvalue()


def _usage(st):
    p = st["root"] / "home" / "usage.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _write_event(root, content):
    (root / NEW).write_text(content)
    return {"hook_event_name": "PostToolUse", "tool_name": "Write", "cwd": str(root),
            "tool_input": {"file_path": str(root / NEW), "content": content}}


def test_hook_new_declaration_with_duplicate_shows_block(hook):
    out = _run(_write_event(hook["root"], RUB))
    ctx = json.loads(out)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse"
    text = ctx["additionalContext"]
    assert text.startswith("grafit: похоже на существующее")
    assert f"formatRubleAmount ({NEW}:2):\n  - {FMT}:64 numberFormatter() (score 0.94)" in text
    assert f'grafit_similar(file="{NEW}")' in text
    rec = _usage(hook)[-1]
    assert rec["tool"] == "claude_hook" and rec["fired"] and rec["n"] == 1 and "ms" in rec


def test_hook_candidates_capped_at_three(hook):
    hook["graph"] = FakeGraph([(f"n{i}", f"fmt{i}()", "code", f"src/f{i}.ts", "L1", 0.05 + i / 1000)
                               for i in range(6)])
    text = json.loads(_run(_write_event(hook["root"], RUB + HELPER)))["hookSpecificOutput"]["additionalContext"]
    assert text.count("\n  - ") == 3


def test_hook_body_edit_without_new_declaration_is_silent(hook, monkeypatch):
    (hook["root"] / NEW).write_text(RUB)
    called = []
    monkeypatch.setattr(ch, "find_candidates", lambda *a: called.append(a) or ("t", []))
    ev = {"tool_name": "Edit", "tool_input": {
        "file_path": str(hook["root"] / NEW), "old_string": "format(v) + ' ₽'",
        "new_string": "format(Math.round(v)) + ' ₽'"}}
    assert _run(ev) == "" and not called and _usage(hook) == []


def test_hook_edit_adding_function_fires(hook):
    (hook["root"] / NEW).write_text(RUB + HELPER)
    ev = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(hook["root"] / NEW), "edits": [
        {"old_string": "", "new_string": HELPER}]}}
    assert "clampPercent" in _run(ev)


def test_hook_no_close_duplicate_is_silent(hook):
    hook["graph"] = FakeGraph([("n2", "formatReportValue()", "code", FMT, "L150", 0.12)])
    assert _run(_write_event(hook["root"], RUB)) == ""
    assert _usage(hook)[-1]["fired"] is False


@pytest.mark.parametrize("broken", ["graph", "embedder", "config"])
def test_hook_grafit_unavailable_exit0_silent(hook, monkeypatch, broken):
    if broken == "graph":
        hook["graph"] = None
    elif broken == "embedder":
        hook["embedder"] = False
    else:
        monkeypatch.setattr(common, "load_config", lambda: None)
    assert _run(_write_event(hook["root"], RUB)) == ""


def test_hook_garbage_stdin_exit0():
    out = io.StringIO()
    assert ch.main(io.StringIO("not json"), out) == 0 and out.getvalue() == ""
    assert ch.main(io.StringIO(json.dumps({"tool_name": "Read"})), out) == 0 and out.getvalue() == ""


def test_hook_timeout_is_silent(hook, monkeypatch):
    import time
    monkeypatch.setattr(ch, "BUDGET_S", 0.2)
    monkeypatch.setattr(ch, "find_candidates", lambda *a: time.sleep(2))
    assert _run(_write_event(hook["root"], RUB)) == ""
    assert _usage(hook)[-1]["why"] == "timeout"


def test_install_idempotent_keeps_foreign_hooks(tmp_path):
    _git(tmp_path, "init", "-q")
    p = tmp_path / ".claude" / "settings.local.json"
    p.parent.mkdir()
    foreign = {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PostToolUse": [
        {"matcher": "Write", "hooks": [{"type": "command", "command": "prettier --write"}]}]}}
    p.write_text(json.dumps(foreign))
    ch.install(tmp_path)
    first = p.read_text()
    msg = ch.install(tmp_path)
    assert p.read_text() == first and any("без изменений" in x for x in msg)
    data = json.loads(first)
    post = data["hooks"]["PostToolUse"]
    assert post[0] == foreign["hooks"]["PostToolUse"][0] and data["permissions"] == foreign["permissions"]
    ours = [h for e in post for h in e["hooks"] if "claude-hook" in h["command"]]
    assert len(ours) == 1 and post[1]["matcher"] == "Write|Edit|MultiEdit"
    ch.install(tmp_path, uninstall=True)
    assert json.loads(p.read_text()) == foreign


def test_install_fresh_and_uninstall_cleans(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))   # без глобального gitignore
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    _git(tmp_path, "init", "-q")
    msg = ch.install(tmp_path)
    p = tmp_path / ".claude" / "settings.local.json"
    assert "установлен" in msg[0] and any("не в .gitignore" in x for x in msg)
    ch.install(tmp_path, uninstall=True)
    assert json.loads(p.read_text()) == {}


def test_install_refuses_broken_json(tmp_path):
    p = tmp_path / ".claude" / "settings.local.json"
    p.parent.mkdir()
    p.write_text("{oops")
    with pytest.raises(ValueError):
        ch.install(tmp_path)
    assert p.read_text() == "{oops"


def test_hook_budget_keeps_partial_result(hook, monkeypatch):
    # эмбеддинг ≈1 с на объявление: не успеваем второе — показываем найденное по первому
    import time
    import numpy as np

    class Slow:
        def embed(self, ts):
            time.sleep(0.4)
            return [np.zeros(2) for _ in ts]
    monkeypatch.setattr(ch, "_remote_embedder", lambda cfg, timeout: Slow())
    monkeypatch.setattr(ch, "BUDGET_S", 1.0)
    monkeypatch.setattr(ch, "EMBED_EST_S", 0.1)
    text = json.loads(_run(_write_event(hook["root"], RUB + HELPER)))["hookSpecificOutput"]["additionalContext"]
    assert "formatRubleAmount" in text and "clampPercent" not in text
