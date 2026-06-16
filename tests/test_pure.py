"""Юнит-тесты чистых функций nav/common (без графа).

Фиксируют поведение, на которое опираются навигационные доработки: нормализацию имён,
конвенции, классификацию reference-фрагментов, рендер пути (структурный/мост/составной),
сшивку двунаправленного BFS, а также классификаторы common.
"""
from grafit import nav, common


# --- nav._norm / _bare ---

def test_norm_equates_method_function_bare():
    assert nav._norm(".Foo()") == "foo"
    assert nav._norm("Foo()") == "foo"
    assert nav._norm("Foo") == "foo"
    assert nav._norm("  .Bar()  ") == "bar"
    assert nav._norm(None) == ""


def test_bare_strips_dots_and_parens_keeps_case():
    assert nav._bare(".Foo()") == "Foo"
    assert nav._bare("Foo") == "Foo"
    assert nav._bare("") == ""


# --- nav._conv_names (конвенции имён) ---

def test_conv_names_command_to_handler():
    out = nav._conv_names("LoginCommand")
    assert ("handled_by", "LoginCommandHandler") in out


def test_conv_names_handler_to_command():
    out = nav._conv_names("LoginCommandHandler")
    assert ("handles", "LoginCommand") in out


def test_conv_names_interface_impl_both_directions():
    assert ("impl_of", "JwtTokenService") in nav._conv_names("IJwtTokenService")
    assert ("impl_of", "IJwtTokenService") in nav._conv_names("JwtTokenService")


# --- nav._is_fragment (reference-фрагмент vs определение) ---

def test_is_fragment_stub_without_sf_and_loc():
    assert nav._is_fragment(("id", "X", "code", "", ""), {}) is True


def test_is_fragment_reference_only_edges():
    assert nav._is_fragment(("id", "X", "code", "f.cs", "L1"), {"id": {"references"}}) is True
    assert nav._is_fragment(("id", "X", "code", "f.cs", "L1"),
                            {"id": {"references", "imports"}}) is True


def test_is_definition_has_structural_edges():
    assert nav._is_fragment(("id", "X", "code", "f.cs", "L1"),
                            {"id": {"contains", "method"}}) is False
    # смешанные: есть и не-ref ребро → это определение
    assert nav._is_fragment(("id", "X", "code", "f.cs", "L1"),
                            {"id": {"references", "method"}}) is False


def test_node_with_loc_but_no_edges_is_not_fragment():
    # есть source_location, рёбра не извлеклись — считаем определением, не фрагментом
    assert nav._is_fragment(("id", "X", "code", "f.cs", "L1"), {}) is False


# --- nav.render_path (типы переходов) ---

def test_render_structural_edge_no_suffix():
    out = nav.render_path("t", (["A", "B"], [{"rel": "calls", "bridge": False}]))
    assert "─calls→" in out
    assert "производные" not in out
    assert "составной" not in out


def test_render_bridge_marks_derived_and_suffix():
    out = nav.render_path("t", (["A", "B"], [{"rel": "impl_of", "bridge": True}]))
    assert "⋯impl_of→" in out
    assert "(⋯ производные: impl_of)" in out


def test_render_composed_keeps_solid_mark_but_warns_direction():
    path = (["A", "B", "C"],
            [{"rel": "references", "bridge": False, "composed": True},
             {"rel": "implements", "bridge": False, "composed": True}])
    out = nav.render_path("t", path)
    assert "─references→" in out and "─implements→" in out
    assert "составной путь — направление приблизительное" in out


# --- nav._compose_join (сшивка двунаправленного BFS) ---

def test_compose_join_stitches_forward_and_backward_halves():
    parent_f = {"a": (None, "A", None, False), "m": ("a", "M", "r1", False)}
    parent_b = {"b": (None, "B", None, False), "m": ("b", "M", "r2", False)}
    labels, rels = nav._compose_join(parent_f, parent_b, "m")
    assert labels == ["A", "M", "B"]
    assert [r["rel"] for r in rels] == ["r1", "r2"]
    assert all(r["composed"] for r in rels)


# --- common классификаторы ---

def test_relation_kind_structural_vs_inferred():
    assert common.relation_kind("calls") == "structural"
    assert common.relation_kind("references") == "structural"
    assert common.relation_kind("handled_by") == "inferred"
    assert common.relation_kind(None) == "inferred"


def test_is_generic():
    assert common.is_generic("string") is True
    assert common.is_generic("Task<bool>") is True
    assert common.is_generic("T") is True          # дженерик-параметр
    assert common.is_generic("LoginCommand") is False
    assert common.is_generic(None) is True


def test_is_test_path():
    assert common.is_test_path("backend/Tests/Foo.cs") is True
    assert common.is_test_path("frontend/src/x.test.tsx") is True
    assert common.is_test_path("backend/Bpm.Application/Foo.cs") is False
    assert common.is_test_path(None) is False


def test_kind_matches():
    assert common.kind_matches("a.cs", "code", "all") is True
    assert common.kind_matches("src/app.ts", "code", "code") is True
    assert common.kind_matches("x.test.ts", "code", "code") is False
    assert common.kind_matches("x.test.ts", "code", "tests") is True
    assert common.kind_matches("d.md", "document", "docs") is True
    assert common.kind_matches("backend/Data/Migrations/x.cs", "code", "prod") is False
    assert common.kind_matches("src/app.ts", "code", "prod") is True
