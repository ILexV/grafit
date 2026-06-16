"""Golden-eval навигации против живого графа `bpm` (skip-friendly через фикстуру `graph`).

Регрессионная сетка: фиксирует ключевые сценарии, которые мы выверяли при доработке MCP,
чтобы будущие правки (или upgrade graphify / ребилд графа) не сломали то, что работает.
Ассерты толерантны к точным промежуточным узлам — проверяют семантику, не дословный путь.
"""
import pytest
from grafit import nav

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
