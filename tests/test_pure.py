"""Юнит-тесты чистых функций nav/common (без графа).

Фиксируют поведение, на которое опираются навигационные доработки: нормализацию имён,
конвенции, классификацию reference-фрагментов, рендер пути (структурный/мост/составной),
сшивку двунаправленного BFS, а также классификаторы common.
"""
from grafit import nav, common, dupes


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


# --- dupes.is_noise_label (framework-шаблонные/generic имена) ---

def test_noise_label_framework_methods():
    # навязаны контрактом фреймворка → структурно одинаковы не из-за копипаста
    assert dupes.is_noise_label(".Handle()") is True
    assert dupes.is_noise_label("HandleAsync") is True
    assert dupes.is_noise_label(".Dispose()") is True


def test_noise_label_includes_generic():
    assert dupes.is_noise_label("string") is True   # делегирует common.is_generic
    assert dupes.is_noise_label(None) is True


def test_noise_label_keeps_domain_symbols():
    # доменно-значимые — НЕ шум, их дубли реальны (кандидаты на рефакторинг)
    assert dupes.is_noise_label(".BuildExportDto()") is False
    assert dupes.is_noise_label("HandleRequirementAsync") is False
    assert dupes.is_noise_label("VersionStatusBadge") is False


# --- dupes.is_family_pair (именная семья CQRS/MediatR) ---

def test_family_pair_command_handler_validator():
    assert dupes.is_family_pair("LoginCommand", "LoginCommandHandler") is True
    assert dupes.is_family_pair("LoginCommand", "LoginCommandValidator") is True
    assert dupes.is_family_pair("LoginCommandHandler", "LoginCommand") is True   # симметрично


def test_family_pair_identical_names_are_real_clones():
    # одинаковое имя в разных файлах — это искомый дубль, НЕ семья
    assert dupes.is_family_pair(".BuildExportDto()", ".BuildExportDto()") is False


def test_family_pair_prefix_difference_not_family():
    # разные стемы (отличие по префиксу) — возможен реальный дубль, не семья
    assert dupes.is_family_pair("UserDto", "OrgUserDto") is False


# --- dupes.is_symbol_node (символ vs файл-узел/конфиг) ---

def test_is_symbol_node_keeps_real_symbols():
    assert dupes.is_symbol_node(".BuildExportDto()", "x/Handler.cs") is True
    assert dupes.is_symbol_node("VersionStatusBadge", "x/Panel.tsx") is True


def test_is_symbol_node_drops_file_and_config_nodes():
    assert dupes.is_symbol_node("ProcessModelExportDto.cs", "x/Dto.cs") is False   # файл-узел
    assert dupes.is_symbol_node("Microsoft.EntityFrameworkCore", "Bpm.Api.csproj") is False
    assert dupes.is_symbol_node(None, "x.cs") is False


# --- dupes.pair_key (ненаправленный ключ пары) ---

def test_pair_key_is_order_independent():
    assert dupes.pair_key("a", "b") == dupes.pair_key("b", "a") == ("a", "b")


# --- dupes.shingles / jaccard (второй сигнал — буквальность) ---

def test_shingles_kgrams_of_tokens():
    sh = dupes.shingles("a b c d", k=2)
    assert sh == frozenset({"a b", "b c", "c d"})


def test_shingles_short_text_fallback():
    assert dupes.shingles("a b", k=4) == frozenset({"a b"})
    assert dupes.shingles("", k=4) == frozenset()


def test_jaccard_identical_and_disjoint():
    a = dupes.shingles("foo bar baz qux", k=2)
    assert dupes.jaccard(a, a) == 1.0
    assert dupes.jaccard(a, dupes.shingles("zzz www yyy", k=2)) == 0.0
    assert dupes.jaccard(a, frozenset()) == 0.0


def test_jaccard_partial_overlap_literal_vs_semantic():
    # сильно пересекающийся текст → выше порога копипаста; разный → ниже
    copy = dupes.jaccard(dupes.shingles("if x return a else return b plus c", k=3),
                         dupes.shingles("if x return a else return b plus d", k=3))
    assert copy >= dupes.LITERAL_JACCARD
    diff = dupes.jaccard(dupes.shingles("parse json from response body now", k=3),
                         dupes.shingles("compute average salary for department", k=3))
    assert diff < dupes.LITERAL_JACCARD


# --- dupes.is_interface_member (аннотация «реализации контракта») ---

def test_interface_member_by_filename_and_path():
    assert dupes.is_interface_member(".CreateAsync()", "Common/Audit/IAuditRegistry.cs") is True
    assert dupes.is_interface_member(".Foo()", "Domain/Interfaces/Service.cs") is True
    assert dupes.is_interface_member(".Foo()", "Application/Audit/AuditRegistry.cs") is False


# --- dupes._cluster_score (порядок по выгоде) / dist_histogram ---

def test_cluster_score_prefers_bigger_tighter_literal():
    big = dupes._cluster_score(size=22, min_dist=0.0001, max_jaccard=0.9)
    pair = dupes._cluster_score(size=2, min_dist=0.0006, max_jaccard=0.1)
    assert big > pair


def test_dist_histogram_buckets():
    pairs = [{"dist": 0.001}, {"dist": 0.015}, {"dist": 0.025}]
    hist = dict(dupes.dist_histogram(pairs, bucket=0.02))
    assert hist[0.02] == 2 and hist[0.04] == 1     # 0.001,0.015 → ≤0.02; 0.025 → ≤0.04


# --- dupes.cluster_pairs (union-find кластеризация) ---

def test_cluster_pairs_transitive_merge():
    # A~B, B~C ⇒ один кластер {A,B,C}; D~E — отдельный
    clusters = dupes.cluster_pairs([("A", "B", 0.01), ("B", "C", 0.02), ("D", "E", 0.03)])
    by_size = sorted((frozenset(c) for c in clusters), key=len, reverse=True)
    assert by_size[0] == frozenset({"A", "B", "C"})
    assert frozenset({"D", "E"}) in by_size


def test_cluster_pairs_empty():
    assert dupes.cluster_pairs([]) == []
