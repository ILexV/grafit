"""Формат ответов MCP: score и вердикт, честные пустые ответы, метки «изменён после сборки»,
сверка цитаты с label, журнал вызовов, GRAFIT_TOOLS. Без живой БД и эмбеддера."""
import asyncio
import json
import pytest
from grafit import mcp_server as m, search
from tests.fakes import FakeGraph

CODE = "backend/App/BranchAccess.cs"


@pytest.fixture
def env(tmp_path, monkeypatch):
    src = tmp_path / CODE
    src.parent.mkdir(parents=True)
    src.write_text("namespace App;\n\npublic class BranchAccess\n{\n    public bool Can() => true;\n}\n")
    nodes = [("c1", "BranchAccess", "code", CODE, "L3", 0.15),
             ("d1", "Branch docs", "document", "docs/branch.md", "L1", 0.14)]
    state = {"graph": FakeGraph(nodes), "changed": []}
    monkeypatch.setattr(m, "_embed", lambda q: ([0.0], {"is_e5": True}))
    monkeypatch.setattr(m, "_graph", lambda p: (m._stat_set(project="t") or state["graph"], "t"))
    monkeypatch.setattr(m, "_root", lambda name: str(tmp_path))
    monkeypatch.setattr(m.common, "graph_freshness", lambda name, live=None: {
        "state": "behind", "behind": 2, "graph": {"short": "abc"},
        "changed": state["changed"], "code_changed": state["changed"]})
    monkeypatch.setattr(m.common, "project_root", lambda *a: tmp_path)
    monkeypatch.setattr(m, "USAGE_LOG", tmp_path / "usage.jsonl")
    state["tmp"] = tmp_path
    return state


def test_search_prod_default_shows_score_and_verdict(env):
    out = m.grafit_search("доступ к филиалу")
    assert "docs/branch.md" not in out                 # kind=prod по умолчанию
    assert "score 0.85" in out and "совпадение: сильное" in out
    assert "граф @ abc" in out


def test_search_nothing_similar_is_honest(env):
    env["graph"] = FakeGraph([("c1", "Weather", "code", CODE, "L3", 0.24)])
    out = m.grafit_search("рецепт борща")
    assert "ничего похожего" in out and "●" not in out


def test_search_kind_filter_empty_suggests_all(env):
    env["graph"] = FakeGraph([("d1", "Branch docs", "document", "docs/branch.md", "L1", 0.14)])
    out = m.grafit_search("доступ")
    assert "по фильтру kind=prod ничего" in out and "kind=all" in out


def test_changed_file_marked_and_quote_checked(env):
    env["changed"] = [CODE]
    out = m.grafit_search("доступ", snippet=True)
    assert "⚠ изменён после сборки" in out
    assert "public class BranchAccess" in out          # имя на месте — цитата показана


def test_quote_detects_shifted_line(env):
    root = str(env["tmp"])
    lines = m._quote(root, CODE, "L1", {}, "BranchAccess", changed=True)
    assert len(lines) == 1 and "строка сместилась" in lines[0] and "L3" in lines[0]
    assert m._quote(root, CODE, "L3", {}, "BranchAccess")[0].startswith("    │")
    gone = m._quote(root, CODE, "L1", {}, "Vanished")
    assert "не найден" in gone[0]
    assert m._quote(root, CODE, "L1", {}, "BranchAccess.cs")[0].startswith("    │")  # файл-узел


def test_find_existing_advises_reuse(env):
    out = m.grafit_find_existing("проверить доступ пользователя к филиалу")
    assert "похожее уже есть" in out and "public class BranchAccess" in out


def test_usage_log_via_mcp_call(env):
    asyncio.run(m.mcp.call_tool("grafit_search", {"question": "доступ к филиалу"}))
    rec = json.loads((env["tmp"] / "usage.jsonl").read_text().splitlines()[-1])
    assert rec["tool"] == "grafit_search" and rec["project"] == "t" and rec["n"] == 1
    assert rec["q_len"] == len("доступ к филиалу") and "question" not in rec and rec["ok"]
    assert isinstance(rec["ms"], int)


def test_tool_filter():
    assert m.tool_enabled("grafit_find_path", "all")
    assert not m.tool_enabled("grafit_find_path", "core")
    assert m.tool_enabled("grafit_similar", "core") and m.tool_enabled("grafit_dupes", "core")
    assert m.tool_enabled("grafit_impact", "search,impact")
    assert not m.tool_enabled("grafit_tests", "search,impact")


def test_tool_descriptions_short_and_lead_with_trigger():
    tools = {t.name: t for t in asyncio.run(m.mcp.list_tools())}
    assert "grafit_find_existing" in tools
    for name, t in tools.items():
        assert len(t.description) < 600, name
    assert "hybrid" not in tools["grafit_search"].inputSchema["properties"]
    assert tools["grafit_search"].inputSchema["properties"]["kind"]["default"] == "prod"
    assert "где проверяется доступ к филиалу" in tools["grafit_search"].description


def test_stdout_clean_on_embedder_fallback(capsys, monkeypatch):
    # предупреждение фоллбэка эмбеддера не должно попадать в stdout (там протокол MCP)
    monkeypatch.setattr(m.common, "embed_url", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(m.common, "_probe_embed", lambda url: None)
    import sys, types
    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=lambda **kw: "local"))
    assert m.common.make_embedder("x") == "local"
    cap = capsys.readouterr()
    assert cap.out == "" and "недоступен" in cap.err
