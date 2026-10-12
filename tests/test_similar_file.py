"""Заход 1d, п.13: similar по всему файлу или диффу — объявления извлекает decls, выдача
группируется по символу, чистые символы — одной строкой без обещания «дублей нет»."""
import subprocess
import pytest
from grafit import decls, mcp_server as m
from tests.fakes import FakeGraph

FMT = "frontend/src/utils/reportBuilderFormat.ts"
NEW = "frontend/src/utils/formatRub.ts"
RUB = ("// сумма в рублях\nexport function formatRubleAmount(v: number): string {\n"
       "  return new Intl.NumberFormat('ru-RU').format(v) + ' ₽';\n}\n")
HELPER = "\nexport function clampPercent(p: number): number {\n  return Math.min(100, p);\n}\n"


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                   check=True, capture_output=True)


# --- извлечение объявлений и диффа ------------------------------------------------

def test_declarations_ts_cs_py():
    ts = ("export function a(x) {}\nexport const B: FC<P> = ({x}) => {\n"
          "  const h = useCallback(() => {}, []);\n};\nconst n = 5;\nconst r = foo(1);\n"
          "export default class C {\n  private async load(id: string) {}\n}\n")
    assert decls.declarations("x.tsx", ts) == [("a", 1), ("B", 2), ("C", 7), ("load", 8)]
    cs = ("public sealed class Svc : ISvc\n{\n    public async Task<decimal> CalcAsync(int id)\n"
          "    {\n        if (x) return 0;\n    }\n    public Svc(IDb db) { }\n}\n")
    assert decls.declarations("x.cs", cs) == [("Svc", 1), ("CalcAsync", 3)]
    assert decls.declarations("x.py", "def f():\n  pass\nclass A:\n  pass\n") == [("f", 1), ("A", 3)]
    assert decls.declarations("x.md", "function a() {}") == []


def test_changed_declarations_vs_head(tmp_path):
    _git(tmp_path, "init", "-q")
    f = tmp_path / NEW
    f.parent.mkdir(parents=True)
    f.write_text(RUB)
    assert decls.changed_lines(tmp_path, NEW) is None            # untracked — новый целиком
    _git(tmp_path, "add", "."); _git(tmp_path, "commit", "-qm", "init")
    assert decls.changed_lines(tmp_path, NEW) == set()
    f.write_text(RUB + HELPER)
    text = f.read_text()
    ch_lines = decls.changed_lines(tmp_path, NEW)
    assert decls.changed_declarations(NEW, text, ch_lines) == [("clampPercent", 6)]


# --- п.13: similar по файлу -------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / NEW).parent.mkdir(parents=True)
    (tmp_path / NEW).write_text(RUB + HELPER)
    state = {"graph": FakeGraph([])}
    monkeypatch.setattr(m, "_embed_passages", lambda ts: ([[0.0]] * len(ts), {"is_e5": True}))
    monkeypatch.setattr(m, "_graph", lambda p: (state["graph"], "t"))
    monkeypatch.setattr(m, "_root", lambda name: str(tmp_path))
    monkeypatch.setattr(m.common, "graph_freshness", lambda name, live=None: {
        "state": "fresh", "graph": {"short": "abc"}, "changed": [], "code_changed": []})
    monkeypatch.setattr(m, "USAGE_LOG", tmp_path / "usage.jsonl")
    state["root"] = tmp_path
    return state


def test_similar_file_groups_by_symbol_and_folds_clean(env):
    env["graph"] = FakeGraph([("n1", "numberFormatter()", "code", FMT, "L64", 0.06),
                              ("n2", "formatReportValue()", "code", FMT, "L150", 0.12)])
    out = m.grafit_similar(file=NEW)
    assert "все объявления: 2 из 2" in out
    # FakeGraph отвечает одинаково на любой вектор → дубль у обоих; проверяем группировку
    assert out.count("■ ") == 2 and "■ formatRubleAmount" in out and "≈0.0600" in out


def test_similar_file_clean_symbols_one_line_and_honest(env):
    env["graph"] = FakeGraph([("n2", "formatReportValue()", "code", FMT, "L150", 0.12)])
    out = m.grafit_similar(file=NEW)
    assert "■ " not in out
    assert "проверено 2, без вероятных дублей (dist≤0.1" in out
    assert "formatRubleAmount (~0.120 formatReportValue())" in out
    assert "НЕ значит, что дублей нет" in out and "grafit_find_existing" in out


def test_similar_file_diff_only_changed(env):
    root = env["root"]
    (root / NEW).write_text(RUB)
    _git(root, "init", "-q"); _git(root, "add", "."); _git(root, "commit", "-qm", "init")
    (root / NEW).write_text(RUB + HELPER)
    env["graph"] = FakeGraph([("n1", "numberFormatter()", "code", FMT, "L64", 0.06)])
    out = m.grafit_similar(file=NEW, base="HEAD")
    assert "diff vs HEAD: 1 из 2" in out and "■ clampPercent" in out
    assert "formatRubleAmount" not in out
    (root / NEW).write_text(RUB)
    assert "новых/изменённых объявлений нет" in m.grafit_similar(file=NEW, base="HEAD")
