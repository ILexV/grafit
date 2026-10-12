"""Объявления функций/классов в исходнике и строки, изменённые относительно git-базы.

Нужно, чтобы искать дубли НОВОГО кода, которого ещё нет в графе: similar по всему файлу или
диффу (приёмка: «git diff --name-only → один вызов на файл») и хук Claude Code после Write/Edit.
Разбор — регулярками по строкам, без парсеров: важна не полнота AST, а дешёвое «какие функции,
компоненты, классы, методы тут объявлены и на какой строке». Языки: TS/JS, C#, Python.
"""
from __future__ import annotations
import re, subprocess
from pathlib import Path

_TS_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}
_CS_EXT = {".cs"}
_PY_EXT = {".py"}

_ID = r"[A-Za-z_$][\w$]*"
_TS = [
    # function foo / export default async function foo / function* gen
    re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*({_ID})\s*[<(]"),
    # const foo = (…) => / const Foo: FC<P> = async x => / const foo = function
    re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_ID})\s*(?::[^=]+)?=\s*(?:async\s+)?"
               rf"(?:function\b|\([^)]*\)?\s*(?::\s*[^=]+)?=>|{_ID}\s*=>|\()"),
    re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+({_ID})"),
    # метод класса с модификатором: private async foo(…) / static bar<T>(…)
    re.compile(rf"^\s+(?:(?:public|private|protected|static|async|override|readonly)\s+)+({_ID})\s*[<(]"),
]
_CS_MOD = r"(?:public|private|protected|internal|static|async|override|virtual|sealed|abstract|new|extern|unsafe|partial|readonly)"
_CS = [
    re.compile(rf"^\s*(?:{_CS_MOD}\s+)*(?:class|record|struct|interface|enum)\s+(\w+)"),
    # метод: модификатор(ы) + тип + имя( — конструктор (без типа) сюда не попадает
    re.compile(rf"^\s*(?:{_CS_MOD}\s+)+(?!(?:class|record|struct|interface|enum)\b)"
               rf"[\w<>\[\],.?() ]+?\s+(\w+)\s*(?:<[^>()]*>)?\s*\("),
]
_PY = [
    re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\("),
    re.compile(r"^\s*class\s+(\w+)\s*[:(]"),
]
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "constructor",
             "using", "lock", "foreach", "nameof", "typeof", "await", "new", "get", "set"}


def _patterns(path: str):
    ext = Path(path).suffix.lower()
    if ext in _TS_EXT:
        return _TS
    if ext in _CS_EXT:
        return _CS
    if ext in _PY_EXT:
        return _PY
    return None


def supported(path: str) -> bool:
    return _patterns(path) is not None


def declarations(path: str, text: str) -> list[tuple[str, int]]:
    """[(имя, номер строки 1-based)] объявлений функций/классов/методов в тексте файла path,
    в порядке появления; одно имя — один раз (перегрузки — первая)."""
    pats = _patterns(path)
    if not pats:
        return []
    out, seen = [], set()
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("//", "#", "*", "/*")):
            continue
        for rx in pats:
            m = rx.match(line)
            if m and m.group(1) not in _KEYWORDS and m.group(1) not in seen:
                seen.add(m.group(1))
                out.append((m.group(1), i))
                break
    return out


def spans(decls: list[tuple[str, int]], lines: list[str]) -> list[tuple[str, int, int]]:
    """(имя, первая, последняя строка) — объявление тянется до следующего без хвостовых пустых
    строк (грубо, но для «правка задела это объявление» достаточно)."""
    out = []
    for j, (name, line) in enumerate(decls):
        end = decls[j + 1][1] - 1 if j + 1 < len(decls) else len(lines)
        while end > line and not lines[end - 1].strip():
            end -= 1
        out.append((name, line, max(line, end)))
    return out


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(root, rel: str, base: str = "HEAD") -> set[int] | None:
    """Номера строк рабочего файла, добавленных/изменённых относительно base (git diff -U0).
    None — файл не отслеживается git (новый целиком) или git недоступен."""
    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)
    try:
        if git("ls-files", "--error-unmatch", "--", rel).returncode != 0:
            return None
        r = git("diff", "-U0", base, "--", rel)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    lines: set[int] = set()
    for ln in r.stdout.splitlines():
        m = _HUNK.match(ln)
        if m:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            lines.update(range(start, start + count))
    return lines


def changed_declarations(path: str, text: str, changed: set[int] | None) -> list[tuple[str, int]]:
    """Объявления, чьё тело задето изменёнными строками (changed=None — все: файл новый)."""
    decls = declarations(path, text)
    if changed is None:
        return decls
    lines = text.splitlines()
    return [(name, a) for name, a, b in spans(decls, lines) if any(a <= x <= b for x in changed)]


def new_names(path: str, old: str, new: str) -> set[str]:
    """Имена, объявленные в new, но не в old (правка Edit: old_string → new_string)."""
    return {n for n, _ in declarations(path, new)} - {n for n, _ in declarations(path, old)}
