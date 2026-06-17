#!/usr/bin/env bash
# rebuild-all.sh — полная пересборка графов знаний всех проектов grafit.
#
# Что делает для КАЖДОГО проекта из реестра ~/.grafit/projects.json:
#   1) graphify .          — ПОЛНАЯ пересборка graph.json с нуля (чистит устаревшие узлы,
#                            которые накапливают инкрементальные пересборки post-commit-хука);
#   2) grafit load --force — заливка в FalkorDB. Переэмбеддит ТОЛЬКО реально изменившиеся
#                            узлы, остальное берёт из Redis-кэша; подмена графа атомарна,
#                            поэтому MCP-запросы во время заливки не ломаются.
#
# Когда запускать: по необходимости, удобно «на ночь». Обычные коммиты держат граф свежим
# через хук — этот скрипт нужен лишь для периодической чистки и гарантированного полного среза.
#
# Использование:
#   ./scripts/rebuild-all.sh                 # все проекты из реестра
#   ./scripts/rebuild-all.sh bpm hr          # только указанные проекты
#   ./scripts/rebuild-all.sh > rebuild.log   # лог в файл (плюс свой лог пишется в ~/.cache)
#
# Безопасно: проекты идут последовательно; глобальный замок grafit сериализует с любыми
# параллельными заливками; недоступный проект/тул — пропуск, а не падение.

set -uo pipefail

GRAFIT_HOME="${GRAFIT_HOME:-$HOME/.grafit}"
REG="$GRAFIT_HOME/projects.json"
LOG="$HOME/.cache/grafit-rebuild-all-$(date +%Y%m%d-%H%M%S).log"

command -v graphify >/dev/null 2>&1 || { echo "✗ нет graphify в PATH"; exit 1; }
command -v grafit   >/dev/null 2>&1 || { echo "✗ нет grafit в PATH";   exit 1; }
[ -f "$REG" ] || { echo "✗ нет реестра $REG — сначала выполни 'grafit load' хотя бы раз"; exit 1; }

mkdir -p "$(dirname "$LOG")"
echo "детальный лог: $LOG"
echo

# реестр { "name": "/abs/path" } → строки "name<TAB>path"
mapfile -t ROWS < <(python3 -c "import json; d=json.load(open('$REG')); [print(f'{k}\t{v}') for k,v in d.items()]")

# фильтр по аргументам (если заданы имена проектов)
WANT=" $* "

ok=0; fail=0; skip=0; t0=$(date +%s)
for row in "${ROWS[@]}"; do
    name="${row%%$'\t'*}"; path="${row#*$'\t'}"
    [ "$#" -gt 0 ] && [[ "$WANT" != *" $name "* ]] && continue

    echo "==================== $name  ($path) ===================="
    if [ ! -d "$path" ]; then
        echo "  ⊘ ПРОПУСК: каталог не найден"; skip=$((skip+1)); echo; continue
    fi

    echo "  [$(date +%H:%M:%S)] graphify . (полная пересборка graph.json) ..."
    if ! ( cd "$path" && PYTHONHASHSEED=0 graphify . ) >>"$LOG" 2>&1; then
        echo "  ✗ ОШИБКА graphify (см. лог) — пропуск"; fail=$((fail+1)); echo; continue
    fi

    echo "  [$(date +%H:%M:%S)] grafit load --force ..."
    grafit load "$path" --force 2>&1 | tee -a "$LOG" | grep -E "эмбеддинги|✓ граф|пропуск"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ]; then ok=$((ok+1)); else echo "  ✗ ОШИБКА grafit load (rc=$rc)"; fail=$((fail+1)); fi
    echo
done

dt=$(( $(date +%s) - t0 ))
echo "==================== ГОТОВО за $((dt/60))м $((dt%60))с: успешно $ok · ошибок $fail · пропущено $skip ===================="
echo "лог: $LOG"
[ "$fail" -eq 0 ]
