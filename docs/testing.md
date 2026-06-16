# Тесты

Регрессионная сетка grafit: фиксирует поведение навигации, чтобы будущие правки (или
upgrade graphify / ребилд графа) не сломали то, что работает.

## Запуск

```bash
./run-tests.sh            # uv run --extra test pytest (или python3 -m pytest)
./run-tests.sh -k pure    # только юнит-тесты чистых функций
```

Скрипт skip-friendly: без `uv`/`pytest` выходит с кодом 0 (как `run-qodana.sh`/`run-stryker.sh`).

## Состав

- **`tests/test_pure.py`** — юнит-тесты чистых функций без графа: нормализация имён
  (`_norm`/`_bare`), конвенции (`_conv_names`), классификация reference-фрагментов
  (`_is_fragment`), рендер пути (`render_path` — структурный/мост/составной), сшивка
  двунаправленного BFS (`_compose_join`), классификаторы `common` (`relation_kind`,
  `is_generic`, `is_test_path`, `kind_matches`). Идут всегда.

- **`tests/test_navigation.py`** — golden-eval против живого графа `bpm` в FalkorDB.
  **Skip-friendly**: пропускается, если FalkorDB недоступен или граф не залит (фикстура
  `graph` в `conftest.py`). Покрывает ключевые сценарии: направление моста
  Command↔Handler (`handles`/`handled_by`), DI/interface-мост (`impl_of`), полный
  backend-flow через compose, canonical resolution (определение vs reference-фрагменты,
  код vs doc-концепт), canonical `handles` в impact, file-level imports у функции.

Граф/порт настраиваются через `GRAFIT_HOST` / `GRAFIT_PORT` / `GRAFIT_TEST_GRAPH`.

Тесты не в CI (как и остальное качество в этом репо) — прогоняются вручную при правках
навигации.
