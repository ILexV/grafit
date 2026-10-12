"""Эталон антидублей: формат фикстуры, подсчёт рангов/recall.
Живой прогон по графам — только с GRAFIT_BENCH=1 (несколько десятков эмбеддингов)."""
import os
import pytest
from grafit import bench


def test_fixture_is_well_formed():
    b = bench.load_bench()
    ids = [it["id"] for it in b["items"]]
    assert len(ids) >= 15 and len(set(ids)) == len(ids)
    for it in b["items"]:
        assert {"graph", "file", "symbol", "q", "q_en", "body", "expect"} <= it.keys()
        assert it["symbol"] in it["body"] and it["expect"]
        assert all(e["sf"] != it["file"] for e in it["expect"])     # иначе similar отсечёт co-location
    assert b["none"]["q"] and b["none"]["graphs"]


def test_rank_matches_member_of_collapsed_row():
    exp = [{"sf": "src/a.ts", "name": "numberFormatter"}]
    rows = [("percentFormatter", "src/p.ts", []),
            ("x()", "src/x.ts", [("numberFormatter()", "src/a.ts")])]
    assert bench.first_rank(rows, exp) == 2
    assert bench.first_rank([("numberFormatter()", "src/b.ts", [])], exp) is None   # другой файл
    assert bench.recall([1, 3, None, 9]) == {1: 0.25, 3: 0.5, 8: 0.5}


def test_summary_and_table():
    res = {"items": [{"id": "a", "q": 1, "q_en": 1, "similar": None, "q_strong": True, "q_top_ok": True},
                     {"id": "b", "q": 4, "q_en": 2, "similar": 1, "q_strong": True, "q_top_ok": False}],
           "none": [{"graph": "g", "q": "x", "quality": "none"}]}
    s = bench.summary(res)
    assert s["q"][1] == 0.5 and s["strong_precision"] == 0.5 and s["none_strong"] == 0
    assert s["strong_top3"] == 0.5
    assert "| a | 1 | 1 | — |" in bench.format_table(res)


@pytest.mark.skipif(os.environ.get("GRAFIT_BENCH") != "1", reason="живой эталон: GRAFIT_BENCH=1")
def test_live_bench_recall_floor():
    # пол по замеру 2026-10-12 после захода 1c; падение ниже — регрессия поиска антидублей
    s = bench.summary(bench.run(log=lambda *a: None))
    if not s["n"]:
        pytest.skip("графы эталона не залиты")
    assert s["q"][8] >= 0.85 and s["q_en"][8] >= 0.95 and s["similar"][8] >= 0.85
    assert s["none_strong"] == 0


def test_decls_extracts_every_bench_symbol():
    # similar по файлу и хук видят только то, что извлёк decls — эталонные символы обязаны быть
    from grafit import decls
    for it in bench.load_bench()["items"]:
        names = [n for n, _ in decls.declarations(it["file"], it["body"])]
        assert it["symbol"] in names, (it["id"], names)
