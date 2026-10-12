"""Антидубли без ложного «дублей нет» (находки rustel 12.10, п.1b): similar показывает
ближайших за порогом и не обещает отсутствия дублей; «сильное» у find_existing — только при
отрыве от фона; служебный код (.claude/.scratch/…) не перебивает продуктовый в kind=prod."""
import pytest
from grafit import common, dupes, mcp_server as m, search
from tests.fakes import FakeGraph

FMT = "frontend/src/utils/reportBuilderFormat.ts"
NEW = "frontend/src/utils/formatRub.ts"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / NEW).parent.mkdir(parents=True)
    (tmp_path / NEW).write_text(
        "// сумма в рублях\nexport function formatRubleAmount(v: number): string {\n"
        "  return new Intl.NumberFormat('ru-RU').format(v) + ' ₽';\n}\n")
    state = {"graph": FakeGraph([])}
    monkeypatch.setattr(m, "_embed", lambda q: ([0.0], {"is_e5": True}))
    monkeypatch.setattr(m, "_embed_passage", lambda t: ([0.0], {"is_e5": True}))
    monkeypatch.setattr(m, "_graph", lambda p: (state["graph"], "t"))
    monkeypatch.setattr(m, "_root", lambda name: str(tmp_path))
    monkeypatch.setattr(m.common, "graph_freshness", lambda name, live=None: {
        "state": "fresh", "graph": {"short": "abc"}, "changed": [], "code_changed": []})
    monkeypatch.setattr(m, "USAGE_LOG", tmp_path / "usage.jsonl")
    return state


# --- п.1: similar на символе из рабочего дерева -----------------------------------

def test_similar_worktree_shows_duplicate_beyond_threshold(env):
    # bi: numberFormatter лежал на 0.1057 при пороге 0.10 → раньше «похожих нет (это хорошо)»
    env["graph"] = FakeGraph([("n1", "numberFormatter()", "code", FMT, "L64", 0.1057),
                              ("n2", "formatReportValue()", "code", FMT, "L150", 0.1186)])
    out = m.grafit_similar("formatRubleAmount", file=NEW)
    assert "numberFormatter()" in out and "formatReportValue()" in out
    assert "score 0.89" in out
    assert "это хорошо" not in out and "дублей не видно" not in out
    assert "НЕ значит, что дублей нет" in out
    assert "grafit_find_existing" in out


def test_similar_within_threshold_marked_probable(env):
    env["graph"] = FakeGraph([("n1", "formatRub()", "code", FMT, "L10", 0.04),
                              ("n2", "numberFormatter()", "code", FMT, "L64", 0.1057)])
    out = m.grafit_similar("formatRubleAmount", file=NEW)
    assert "вероятные дубли (dist≤0.1): 1" in out
    assert out.index("formatRub()") < out.index("ближайшие за порогом") < out.index("numberFormatter()")
    assert "grafit_find_existing" not in out          # дубль найден — подсказка не нужна


def test_similar_nothing_at_all_never_claims_no_duplicates(env):
    out = m.grafit_similar("formatRubleAmount", file=NEW)
    assert "дублей не видно" not in out and "grafit_find_existing" in out


def test_similar_auto_kind_skips_docs_and_service_code(env):
    env["graph"] = FakeGraph([("d", "Spec", "document", "docs/spec.md", "L1", 0.05),
                              ("s", "parseScalar()", "code", ".claude/scripts/lib/t.mjs", "L1", 0.06),
                              ("n", "numberFormatter()", "code", FMT, "L64", 0.1057)])
    out = m.grafit_similar("formatRubleAmount", file=NEW)
    assert "kind=prod" in out and "numberFormatter()" in out
    assert "Spec" not in out and "parseScalar" not in out


def test_split_near_fills_up_to_k():
    rows = [(str(i), f"f{i}", "code", "a.ts", "L1", "", d) for i, d in
            enumerate([0.05, 0.09, 0.11, 0.12, 0.13])]
    dup, near = dupes.split_near(rows, 0.10, 4)
    assert [r[6] for r in dup] == [0.05, 0.09] and [r[6] for r in near] == [0.11, 0.12]


def test_auto_kind():
    assert dupes.auto_kind("frontend/src/a.ts") == "prod"
    assert dupes.auto_kind("frontend/src/a.test.ts") == "tests"
    assert dupes.auto_kind(".opencode/tasks/x/sample.ts") == "code"


def test_instructions_drop_empty_means_absent():
    text = m._INSTRUCTIONS
    assert "значит, такого кода нет" not in text
    assert "НЕ доказывает" in text
    assert "дублей не видно" not in (m.grafit_similar.__doc__ or "")


# --- п.2: «сильное» — только при отрыве от фона ------------------------------------

def _flat(best, rest, n=15):
    """Граф: лучший узел best и n узлов фона на дистанции rest (разные файлы)."""
    return FakeGraph([("b", "numberFormatter()", "code", FMT, "L64", best)] +
                     [(f"x{i}", f"noise{i}()", "code", f"src/n{i}.ts", "L1", rest + i * 0.0002)
                      for i in range(n)])


def test_match_quality_needs_gap_from_background():
    assert search.match_quality(0.15) == "strong"                  # без фона — как раньше
    assert search.match_quality(0.15, bg=0.175) == "strong"        # отрыв 0.025
    assert search.match_quality(0.15, bg=0.158) == "weak"          # отрыв 0.008 — фон
    assert search.match_quality(0.19, bg=0.25) == "weak"           # отрыв есть, но далеко
    assert search.match_quality(0.22, bg=0.30) == "none"


def test_background_is_median_of_pool():
    assert search.background([0.1] * 5) is None                    # мало данных
    assert search.background([0.1, 0.2] + [0.3] * 9) == 0.3


def test_find_existing_flat_scores_not_strong(env):
    # bi: «сумма в рублях» — релевантный 0.85, а фон 0.84–0.85 → не «сильное»
    env["graph"] = _flat(0.150, 0.155)
    out = m.grafit_find_existing("отформатировать денежную сумму в рублях")
    assert "совпадение: слабое" in out and "отрыв от фона" in out
    assert "явный лидер не выделяется" in out and "numberFormatter()" in out


def test_find_existing_clear_leader_is_strong(env):
    env["graph"] = _flat(0.130, 0.165)
    out = m.grafit_find_existing("отформатировать денежную сумму в рублях")
    assert "совпадение: сильное" in out and "похожее уже есть" in out


def test_nothing_similar_is_not_a_guarantee(env):
    env["graph"] = FakeGraph([("c1", "Weather", "code", "src/w.ts", "L3", 0.24)])
    out = m.grafit_find_existing("рецепт борща")
    assert "ничего похожего" in out and "не доказывает" in out
    assert "можно писать новый" not in out


def test_prod_excludes_service_dirs():
    assert common.is_service_path(".claude/scripts/lib/tasklib.mjs")
    assert common.is_service_path("tools/.autopilot/state.js")
    assert not common.is_service_path("frontend/src/a.ts")
    assert not common.is_service_path(".eslintrc.js")              # файл с точкой в корне — не каталог
    assert not common.kind_matches(".scratch/x/shot.mjs", "code", "prod")
    assert common.kind_matches(".scratch/x/shot.mjs", "code", "code")


def test_props_is_noise_for_dupes():
    assert dupes.is_noise_label("Props")
