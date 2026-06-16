# Патч graphify: C# constructor-DI рёбра (#1)

graphify (`graphifyy`, проверено на **v0.8.39**) не извлекает рёбра от класса/конструктора к
типам параметров конструктора в C#. Для .NET CQRS это критично: `LoginCommandHandler(IJwtTokenService …)`
не давал связи `LoginCommandHandler → IJwtTokenService`, из-за чего `grafit impact/trace/find_path`
не видели DI-зависимости.

> **Это патч стороннего пакета в установленной копии** — слетает при `uv tool upgrade graphifyy`
> (или reinstall). Не форкаем; этот файл — источник для **переприменения** после апгрейда.
> Кандидат на upstream-PR в `github.com/safishamsi/graphify` (часть A зеркалит Java/Groovy-конфиги).

Файл: `<graphify>/extract.py`, где `<graphify>` =
```bash
/home/lex/.local/share/uv/tools/graphifyy/bin/python3 -c "import graphify,os;print(os.path.dirname(graphify.__file__))"
```

## Часть A — `constructor_declaration` в C#-конфиг (классические конструкторы)
`_CSHARP_CONFIG` (≈ строки 1965–1975). Java/Groovy уже содержат `constructor_declaration` — C# отстал.
```diff
-    function_types=frozenset({"method_declaration"}),
+    function_types=frozenset({"method_declaration", "constructor_declaration"}),
...
-    function_boundary_types=frozenset({"method_declaration"}),
+    function_boundary_types=frozenset({"method_declaration", "constructor_declaration"}),
```

## Часть B — primary constructors (C# 12) — главная для этой кодовой базы
DI-параметры висят на `class_declaration` как `parameter_list` (БЕЗ field-name), а не на
`constructor_declaration`. Часть A их не ловит. В `walk()`, сразу ПОСЛЕ создания узла класса
(`add_edge(file_nid, class_nid, "contains", line)`, ≈ строка 2293), добавить:
```python
            # C#: primary-constructor параметры (class Foo(IBar bar) : ...) — DI-зависимости.
            # Типы висят на parameter_list самого class_declaration; эмитим ребро от класса
            # к типам параметров (зеркалит блок обычного конструктора ниже).
            if config.ts_module == "tree_sitter_c_sharp":
                # primary-constructor parameter_list — дочерний узел без field-name
                pc_params = next((c for c in node.children if c.type == "parameter_list"), None)
                if pc_params is not None:
                    for p in pc_params.children:
                        if p.type != "parameter":
                            continue
                        type_node = p.child_by_field_name("type")
                        pc_refs: list[tuple[str, str]] = []
                        _csharp_collect_type_refs(type_node, source, False, pc_refs)
                        for ref_name, role in pc_refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != class_nid:
                                add_edge(class_nid, target_nid, "references", line, context=ctx)
```
Результат: `LoginCommandHandler --references(parameter_type)--> IApplicationDbContext / IPasswordHasher /
IJwtTokenService / IMapper`. Цель резолвится в реальный узел типа (не висячий).

## Переприменение после апгрейда graphifyy
1. Применить части A и B в `<graphify>/extract.py`.
2. Очистить AST-кэш проекта (semantic/LLM-кэш не трогать): удалить `graphify-out/cache/ast/`.
3. Пересобрать граф (AST, без LLM):
   ```bash
   PYTHONHASHSEED=0 /home/lex/.local/share/uv/tools/graphifyy/bin/python3 -c \
     "from pathlib import Path; from graphify.watch import _rebuild_code; \
      print(_rebuild_code(Path('/абс/путь/проекта'), changed_paths=None, force=True))"
   ```
4. `grafit load` — залить обновлённый graph.json в FalkorDB.

## Стыковка с grafit (что видно после патча)
- `grafit impact <Interface>` → хендлеры-потребители появляются среди зависимых (DI-impact).
- `grafit trace <Handler> --with-references` → DI-зависимости конструктора (рёбра типа `references`,
  поэтому `--with-references`, а не дефолтный flow).
- `grafit find_path <Handler> → <Impl>` всё ещё может не найти прямой путь: маршрут смешанного
  направления (`Handler→references→IFoo`, но `Foo→implements→IFoo`), а FalkorDB shortestPath
  только направленный. Прикрыто fallback'ом «связано: …» + конвенцией `impl_of`.

Проверено на bpm 2026-06-16: граф 3978/6946 → 5045/8008 рёбер (+DI).
