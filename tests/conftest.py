"""Общие фикстуры тестов grafit.

`graph` — подключение к живому графу `bpm` в FalkorDB для golden-eval навигации.
Skip-friendly: если FalkorDB недоступен или граф не залит, golden-тесты пропускаются
(юнит-тесты чистых функций от этого не зависят и идут всегда).
"""
import os
import pytest
from grafit import common

HOST = os.environ.get("GRAFIT_HOST", "localhost")
PORT = int(os.environ.get("GRAFIT_PORT", "6399"))
GRAPH_NAME = os.environ.get("GRAFIT_TEST_GRAPH", "bpm")


@pytest.fixture(scope="session")
def graph():
    try:
        graphs = common.list_graphs(HOST, PORT)
    except Exception as ex:  # FalkorDB не поднят / недоступен
        pytest.skip(f"FalkorDB недоступен ({ex!r}) — golden-eval пропущен")
    if GRAPH_NAME not in graphs:
        pytest.skip(f"граф '{GRAPH_NAME}' не залит в FalkorDB — golden-eval пропущен")
    return common.connect(HOST, PORT).select_graph(GRAPH_NAME)
