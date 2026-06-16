#!/usr/bin/env bash
# Прогон тестов grafit: юнит-тесты чистых функций + golden-eval навигации против живого
# графа bpm. Skip-friendly: golden-тесты пропускаются, если FalkorDB/граф недоступны;
# если нет pytest и нет uv — выходим с кодом 0 (как остальные tool-скрипты репо).
set -euo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  exec uv run --extra test pytest "$@"
fi
if python3 -c "import pytest" >/dev/null 2>&1; then
  exec python3 -m pytest "$@"
fi
echo "pytest недоступен — поставь uv (uv run --extra test pytest) или: pip install pytest"
exit 0
