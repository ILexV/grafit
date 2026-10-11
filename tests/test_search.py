"""search.search_ex: фильтр kind без фоллбэка, дедуп по sf:loc, скрытие reference-фрагментов,
score (дистанция) и порог «ничего похожего». Без живой БД (FakeGraph)."""
from grafit import search
from tests.fakes import FakeGraph

CODE = "backend/App/Branch.cs"


def _run(nodes, rels=None, **kw):
    return search.search_ex(FakeGraph(nodes, rels), [0.0], "q", **kw)


def test_kind_filter_cut_everything_returns_empty_not_unfiltered():
    nodes = [("d1", "Branch access", "document", "docs/a.md", "L1", 0.15),
             ("d2", "Notes", "document", ".scratch/b.md", "L3", 0.16)]
    res = _run(nodes, kind="prod")
    assert res["rows"] == []                      # раньше молча возвращались доки
    assert res["filtered_out"] == 2 and res["candidates"] == 2


def test_kind_prod_excludes_docs_and_tests():
    nodes = [("d1", "Branch access", "document", "docs/a.md", "L1", 0.14),
             ("t1", "BranchTests", "code", "tests/BranchTests.cs", "L5", 0.15),
             ("c1", "BranchAccessService", "code", CODE, "L10", 0.16)]
    rows = _run(nodes, kind="prod")["rows"]
    assert [r[0] for r in rows] == ["c1"]


def test_dedupe_by_source_location():
    nodes = [("f1", "Branch.cs", "code", CODE, "L1", 0.14),
             ("c1", "Branch", "code", CODE, "L1", 0.15),
             ("m1", ".Grant()", "code", CODE, "L20", 0.16)]
    rows = _run(nodes, k=5)["rows"]
    assert [r[0] for r in rows] == ["c1", "m1"]          # класс вместо файла на той же строке
    assert len(_run(nodes, k=5, dedupe=False, collapse=False)["rows"]) == 3


def test_reference_fragments_hidden():
    nodes = [("ref", "Branch", "code", "backend/Other.cs", "L7", 0.13),
             ("def", "Branch", "code", CODE, "L9", 0.15)]
    rels = {"ref": {"references"}, "def": {"contains", "method"}}
    rows = _run(nodes, rels, k=5)["rows"]
    assert [r[0] for r in rows] == ["def"]
    assert len(_run(nodes, rels, k=5, hide_fragments=False)["rows"]) == 2


def test_score_column_is_vector_distance_and_best_dist():
    nodes = [("a", "Alpha", "code", "src/a.py", "L1", 0.150),
             ("b", "Beta", "code", "src/b.py", "L1", 0.170)]
    res = _run(nodes, k=2)
    assert [r[7] for r in res["rows"]] == [0.150, 0.170]
    assert res["best_dist"] == 0.150


def test_match_quality_thresholds():
    assert search.match_quality(0.14) == "strong"
    assert search.match_quality(search.STRONG_DIST + 0.005) == "weak"
    assert search.match_quality(search.NONE_DIST + 0.01) == "none"
    assert search.match_quality(None) == "weak"


def test_filtered_search_widens_pool_once_when_filter_eats_it():
    docs = [(f"d{i}", f"Doc {i}", "document", f"docs/{i}.md", "L1", 0.10 + i / 10000)
            for i in range(300)]
    code = [("c1", "BranchAccessService", "code", CODE, "L10", 0.2)]
    g = FakeGraph(docs + code)
    res = search.search_ex(g, [0.0], "q", k=8, kind="prod")
    assert [r[0] for r in res["rows"]] == ["c1"]
    assert len(g.vec_calls) == 2 and g.vec_calls[1] == g.vec_calls[0] * 4


def test_empty_graph():
    res = _run([])
    assert res["rows"] == [] and res["candidates"] == 0


# --- сворачивание: файл-узел ↔ класс, интерфейс ↔ реализация ---

IFACE = "backend/App/INormService.cs"
IMPL = "backend/Infra/NormService.cs"


def test_file_node_collapses_into_class_from_same_file():
    nodes = [("f", "Branch.cs", "code", CODE, "L1", 0.14),
             ("x", "Other", "code", "src/x.cs", "L3", 0.15),
             ("c", "Branch", "code", CODE, "L5", 0.16)]
    rows = _run(nodes, k=5)["rows"]
    assert [r[0] for r in rows] == ["c", "x"]          # класс встал на место файла
    assert [r[0] for r in _run(nodes, k=5, collapse=False)["rows"]] == ["f", "x", "c"]


def test_lonely_file_node_kept():
    nodes = [("f", "Branch.cs", "code", CODE, "L1", 0.14)]
    assert [r[0] for r in _run(nodes)["rows"]] == ["f"]


def test_interface_and_implementation_collapse_to_implementation():
    nodes = [("it", "INormService", "code", IFACE, "L5", 0.14),
             ("im", ".Calc()", "code", IFACE, "L9", 0.145),
             ("ct", "NormService", "code", IMPL, "L7", 0.15),
             ("cm", ".Calc()", "code", IMPL, "L12", 0.155),
             ("o", ".Other()", "code", "src/o.cs", "L1", 0.16)]
    g = FakeGraph(nodes, owners={"im": "INormService", "cm": "NormService", "o": "Other"},
                  supers={"NormService": ["INormService"]})
    res = search.search_ex(g, [0.0], "q", k=5)
    assert [r[0] for r in res["rows"]] == ["ct", "cm", "o"]
    assert res["contracts"]["ct"][0] == "INormService" and res["contracts"]["cm"][1] == IFACE


def test_convention_and_override_without_edges():
    # без implements-ребра: конвенция IFoo↔Foo; override — база по ребру inherits
    nodes = [("a", ".Run()", "code", "src/IJob.cs", "L3", 0.14),
             ("b", ".Run()", "code", "src/Job.cs", "L4", 0.15),
             ("c", ".Save()", "code", "src/Base.cs", "L3", 0.16),
             ("d", ".Save()", "code", "src/Derived.cs", "L3", 0.17)]
    g = FakeGraph(nodes, owners={"a": "IJob", "b": "Job", "c": "BaseRepo", "d": "UserRepo"},
                  supers={"UserRepo": ["BaseRepo<User>"]})
    res = search.search_ex(g, [0.0], "q", k=5)
    assert [r[0] for r in res["rows"]] == ["b", "d"]


def test_same_named_methods_of_unrelated_types_not_collapsed():
    nodes = [("a", ".Run()", "code", "src/A.cs", "L3", 0.14),
             ("b", ".Run()", "code", "src/B.cs", "L4", 0.15)]
    g = FakeGraph(nodes, owners={"a": "Alpha", "b": "Beta"})
    assert len(search.search_ex(g, [0.0], "q", k=5)["rows"]) == 2
