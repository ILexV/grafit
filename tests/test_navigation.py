"""Golden-eval навигации против живого графа `bpm` (skip-friendly через фикстуру `graph`).

Регрессионная сетка: фиксирует ключевые сценарии, которые мы выверяли при доработке MCP,
чтобы будущие правки (или upgrade graphify / ребилд графа) не сломали то, что работает.
Ассерты толерантны к точным промежуточным узлам — проверяют семантику, не дословный путь.
"""
import pytest
from grafit import nav, dupes

NAME = "bpm"


def _route(graph, src, dst, hops=6):
    a, b = nav.resolve_node(graph, src), nav.resolve_node(graph, dst)
    assert a, f"не резолвится источник: {src}"
    assert b, f"не резолвится цель: {dst}"
    p = nav.find_route(graph, a, b, max_hops=hops)
    assert p, f"путь {src} → {dst} не найден"
    return nav.render_path(NAME, p)


# --- направление моста Command↔Handler (handles vs handled_by) ---

def test_handler_to_command_labeled_handles(graph):
    r = _route(graph, "LoginCommandHandler", "LoginCommand")
    assert "⋯handles→" in r
    assert "handled_by" not in r


def test_command_to_handler_labeled_handled_by(graph):
    r = _route(graph, "LoginCommand", "LoginCommandHandler")
    assert "⋯handled_by→" in r


# --- DI / interface-мост (один convention-хоп impl_of, не составной) ---

def test_di_interface_bridge_clean(graph):
    r = _route(graph, "LoginCommandHandler", "JwtTokenService")
    assert "IJwtTokenService" in r
    assert "impl_of" in r
    assert "составной" not in r  # это одномостовой путь, не compose-fallback


def test_organization_access_checker_bridge(graph):
    r = _route(graph, "OrganizationContributorAuthorizationHandler", "OrganizationAccessChecker")
    assert "IOrganizationAccessChecker" in r
    assert "impl_of" in r


# --- длинный backend-flow собирается целиком (compose-fallback) ---

def test_full_backend_flow_composes(graph):
    r = _route(graph, "AuthController", "JwtTokenService")
    for node in ("AuthController", "LoginCommand", "LoginCommandHandler",
                 "IJwtTokenService", "JwtTokenService"):
        assert node in r, f"в составном flow нет узла {node}: {r}"
    assert "составной путь" in r  # честная маркировка эвристического маршрута


# --- canonical resolution: reference-фрагменты не равноправны определению ---

def test_interface_resolves_to_canonical_definition(graph):
    r = nav.resolve_node(graph, "IJwtTokenService")
    assert r["sf"].endswith("Common/Interfaces/IJwtTokenService.cs")
    assert r["n_candidates"] == 1
    assert r["fragments"] >= 1       # reference-узлы свёрнуты в пометку, не в кандидаты
    assert r["ambiguous"] is False


def test_resolver_prefers_code_symbol_over_doc_concept(graph):
    r = nav.resolve_node(graph, "AuthProvider")
    assert r["ft"] == "code"
    assert r["sf"].endswith("AuthProvider.tsx")
    # code vs docs concept — честная неоднозначность (не шум reference-узлов)
    assert r["ambiguous"] is True
    assert any(a["ft"] == "concept" for a in r["alternatives"])


# --- impact: handles только к каноническому определению команды ---

def test_impact_handles_only_canonical_command(graph):
    lines = nav.format_impact(graph, NAME, "LoginCommandHandler")
    handles = [ln for ln in lines if "handles" in ln]
    assert len(handles) == 1, f"ожидался один canonical handles, получено: {handles}"
    assert "Auth/Commands/Login/LoginCommand.cs" in handles[0]
    # фрагменты из controller/validator не должны попадать в handles
    assert "AuthController.cs" not in handles[0]
    assert "LoginCommandValidator.cs" not in handles[0]


# --- file-level imports подтягиваются к function/component node ---

def test_file_imports_surface_on_function(graph):
    r = nav.resolve_node(graph, "AuthProvider")
    imps = nav.file_imports(graph, r["sf"], exclude_id=r["id"])
    assert imps, "ожидались file-level imports у компонента"
    targets = {t[2] for t in imps}  # (file_label, rel, target_label, target_sf)
    assert any("Tokens" in t or "getAccessToken" in t for t in targets)


def test_file_imports_excludes_self_for_file_node(graph):
    # для самого файл-узла imports уже его прямые рёбра — via-дублей быть не должно
    fr = nav.resolve_node(graph, "AuthProvider.tsx")
    assert fr is not None
    assert nav.file_imports(graph, fr["sf"], exclude_id=fr["id"]) == []


# --- tests lookup (convention tested_by) ---

def test_tests_lookup_finds_handler_tests(graph):
    lines = "\n".join(nav.format_tests(graph, NAME, "LoginCommandHandler"))
    assert "LoginCommandHandlerTests" in lines


# --- дубликаты: режим similar (node-to-node) ---

def test_similar_finds_cross_file_duplicate(graph):
    # .BuildExportDto() продублирован в двух export-хендлерах (PoC-находка, dist≈0.0006)
    res = dupes.similar(graph, "BuildExportDto", k=6, threshold=0.10)
    assert res is not None
    sfs = {c[3] for c in res["candidates"]}
    assert any("ExportProcessVersionModel" in s or "ExportCurrentProcessModel" in s for s in sfs), \
        f"ожидался дубль BuildExportDto в другом export-хендлере, получено: {sfs}"


def test_similar_excludes_same_file_colocation(graph):
    # Command и Handler лежат рядом и графово связаны — это НЕ дубль, не должны попасть
    res = dupes.similar(graph, "LoginCommand", k=8, threshold=0.10)
    assert res is not None
    labels = {c[1] for c in res["candidates"]}
    assert "LoginCommandHandler" not in labels
    assert "LoginCommandValidator" not in labels


def test_similar_unknown_symbol_returns_none(graph):
    assert dupes.similar(graph, "NoSuchSymbolXYZ123") is None


def test_similar_drops_reference_fragments(graph):
    # co-located тип-узлы (Process/ProcessVersion как параметры метода) — usage, не дубли:
    # фильтр reference-фрагментов должен их убрать, оставив реальный клон метода
    res = dupes.similar(graph, "BuildExportDto", k=8, threshold=0.10)
    assert res is not None
    labels = {c[1] for c in res["candidates"]}
    assert ".BuildExportDto()" in labels                 # реальный клон жив
    assert "Process" not in labels and "ProcessVersion" not in labels


# --- дубликаты: глобальный скан ---

def test_find_duplicates_surfaces_authorization_handlers(graph):
    # HandleRequirementAsync скопирован в OrganizationAdmin/User/Contributor-хендлерах
    clusters = dupes.find_duplicates(graph, kind="prod", threshold=0.06)
    joined = "\n".join(
        m[2] for c in clusters for m in c["members"])
    assert "OrganizationAdminAuthorizationHandler" in joined
    assert "OrganizationUserAuthorizationHandler" in joined


def test_find_duplicates_skips_framework_handle_by_default(graph):
    # голый .Handle() (MediatR-шаблон) по умолчанию не должен плодить кластеры
    clusters = dupes.find_duplicates(graph, kind="prod", threshold=0.06)
    bare_handle = [m for c in clusters for m in c["members"] if nav._norm(m[1]) == "handle"]
    assert bare_handle == [], f"шаблонные .Handle() просочились: {bare_handle}"


# --- обогащение: Jaccard, score-сортировка, аннотации ---

def test_clusters_carry_annotations_and_score_sorted(graph):
    clusters = dupes.find_duplicates(graph, kind="prod", threshold=0.06)
    assert clusters
    for c in clusters:                               # все новые поля присутствуют
        assert {"max_jaccard", "literal", "shared_name", "contract", "score"} <= c.keys()
        assert 0.0 <= c["max_jaccard"] <= 1.0
    scores = [c["score"] for c in clusters]
    assert scores == sorted(scores, reverse=True)    # сортировка по выгоде (убывание score)
    # хотя бы один кластер — «один метод в N местах» (shared_name проставлен)
    assert any(c["shared_name"] for c in clusters)


def test_duplicate_pairs_sorted_literal_first(graph):
    pairs = dupes.duplicate_pairs(graph, kind="prod", threshold=0.06)
    assert pairs
    for p in pairs:
        assert 0.0 <= p["jaccard"] <= 1.0
        assert p["literal"] == (p["jaccard"] >= dupes.LITERAL_JACCARD)
    # буквальные (высокий Jaccard) идут раньше семантических
    jacc = [p["jaccard"] for p in pairs]
    assert jacc == sorted(jacc, reverse=True)


def test_similar_candidates_carry_jaccard(graph):
    res = dupes.similar(graph, "BuildExportDto", k=6, threshold=0.10)
    assert res and res["candidates"]
    top = res["candidates"][0]                        # (id,label,ft,sf,loc,text,dist,jaccard)
    assert len(top) == 8
    assert 0.0 <= top[7] <= 1.0
