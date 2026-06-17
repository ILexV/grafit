"""Общие хелперы grafit: имя графа на проект, реестр, кэш эмбеддингов, фильтры узлов.

Разделение данных: один инстанс FalkorDB, один **именованный граф на проект**.
Имя графа = basename git-репозитория, очищенное до [a-z0-9_]. Реестр ловит коллизии.

Состояние (config/registry/cache) — в $GRAFIT_HOME (по умолчанию ~/.grafit), чтобы
пакет работал установленным и состояние было общим на все проекты машины.
"""
from __future__ import annotations
import hashlib, json, os, re, subprocess, sys
from pathlib import Path

HOME = Path(os.environ.get("GRAFIT_HOME", Path.home() / ".grafit"))
CONFIG_PATH = HOME / "config.json"        # модель эмбеддингов (общая для всех проектов)
REGISTRY_PATH = HOME / "projects.json"     # graph_name -> project abspath
META_PATH = HOME / "meta.json"             # graph_name -> метаданные сборки (commit/время/счётчики)

# Приоритет локальных мультиязычных моделей.
PREFERRED = [
    "intfloat/multilingual-e5-large",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "intfloat/multilingual-e5-small",
    "BAAI/bge-small-en-v1.5",
]


def project_root(start: Path | None = None) -> Path:
    start = start or Path.cwd()
    try:
        top = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if top:
            return Path(top)
    except Exception:
        pass
    return Path(start).resolve()


def sanitize(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")
    return re.sub(r"_+", "_", s) or "graph"


def graph_name(explicit: str | None = None, root: Path | None = None) -> str:
    if explicit:
        return sanitize(explicit)
    return sanitize((root or project_root()).name)


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text()) if REGISTRY_PATH.exists() else {}


def save_registry(reg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False))


def register(name: str, root: Path) -> None:
    """Записать graph->path; упасть при коллизии имён разных проектов."""
    reg = load_registry()
    prev = reg.get(name)
    ap = str(root)
    if prev and prev != ap:
        sys.exit(
            f"КОЛЛИЗИЯ ИМЁН: граф '{name}' уже занят проектом {prev}.\n"
            f"Текущий проект: {ap}\nЗапусти с явным именем: --graph <уникальное_имя>"
        )
    reg[name] = ap
    save_registry(reg)


def load_config() -> dict | None:
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else None


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def update_config(**kw) -> dict:
    """Слить ключи в config.json, не затирая остальные (напр. embed_url ↔ model/dim)."""
    cfg = load_config() or {}
    cfg.update({k: v for k, v in kw.items() if v is not None})
    save_config(cfg)
    return cfg


def now_iso() -> str:
    """Локальное время в ISO-8601 (для built_at в meta.json)."""
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# --- метаданные сборки графа (свежесть) ---
# meta.json: graph_name -> {commit, short, committed_at, branch, dirty_at_build,
# built_at, root, nodes, edges, model}. Отдельно от реестра (тот строковый — его
# читают list-инструменты), чтобы ничего не ломать.

def load_meta() -> dict:
    return json.loads(META_PATH.read_text()) if META_PATH.exists() else {}


def save_meta(name: str, **kw) -> dict:
    """Слить метаданные сборки для графа name (None-значения пропускаются)."""
    meta = load_meta()
    entry = meta.get(name, {})
    entry.update({k: v for k, v in kw.items() if v is not None})
    meta[name] = entry
    HOME.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return entry


# --- сериализация заливок (post-commit-хуки разных проектов не должны бить параллельно) ---
_LOAD_LOCK_FD = None


def acquire_load_lock(wait: bool = True) -> bool:
    """Глобальный межпроцессный замок на `grafit load` (один на машину за раз).

    Хуки разных проектов и быстрые коммиты подряд иначе запускают N конкурентных
    заливок, которые молотят общий эмбед-сервис (см. инцидент 2026-06-17). fd держится
    до конца процесса — ОС снимает замок при exit, явный unlock не нужен (load одноразовый).
    No-op на платформе без fcntl (Windows) — там вернёт True."""
    global _LOAD_LOCK_FD
    try:
        import fcntl
    except Exception:
        return True
    HOME.mkdir(parents=True, exist_ok=True)
    _LOAD_LOCK_FD = open(HOME / "load.lock", "w")
    try:
        fcntl.flock(_LOAD_LOCK_FD, fcntl.LOCK_EX if wait else (fcntl.LOCK_EX | fcntl.LOCK_NB))
        return True
    except OSError:
        return False


def file_sha(p) -> str:
    """sha1 содержимого файла (для coalesce: не перезаливать неизменённый graph.json)."""
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PROJECT_LOCK_FD = None


def acquire_project_load_lock(name: str, wait_kill: float = 10.0) -> None:
    """Преемптивный per-project замок: новый `grafit load` ОСТАНАВЛИВАЕТ ещё идущую заливку
    ТОГО ЖЕ графа — она грузит уже устаревшие данные, а её посчитанные вектора всё равно
    осели в Redis-кэше (HSET атомарен), так что новый load их переиспользует. Разные проекты
    не трогаем (для них — глобальная сериализация acquire_load_lock). No-op без fcntl."""
    global _PROJECT_LOCK_FD
    try:
        import fcntl, signal, time
    except Exception:
        return
    HOME.mkdir(parents=True, exist_ok=True)
    fd = open(HOME / f"load.{sanitize(name)}.lock", "a+")
    got = _try_flock(fd)
    if not got:
        # этот граф уже заливается — снять прошлый процесс (его данные устарели)
        try:
            fd.seek(0); raw = fd.read().strip()
            old_pid = int(raw) if raw.isdigit() else 0
        except Exception:
            old_pid = 0
        if old_pid and old_pid != os.getpid():
            print(f"[grafit] прерываю прошлую заливку '{name}' (pid {old_pid}) — есть свежие изменения")
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(old_pid, sig)
                except ProcessLookupError:
                    break
                except Exception:
                    break
                waited = 0.0
                while waited < wait_kill and not got:
                    got = _try_flock(fd)
                    if got:
                        break
                    time.sleep(0.2); waited += 0.2
                if got:
                    break
        if not got:
            import fcntl as _f
            _f.flock(fd, _f.LOCK_EX)   # подстраховка — дождаться блокирующе
    try:
        fd.seek(0); fd.truncate(); fd.write(str(os.getpid())); fd.flush()
    except Exception:
        pass
    _PROJECT_LOCK_FD = fd   # держим открытым до конца процесса (ОС снимет замок при exit)


def _try_flock(fd) -> bool:
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def git_info(root) -> dict | None:
    """Состояние git рабочего дерева: commit/short/время/ветка/грязно. None — не git-репо."""
    def g(*a):
        try:
            return subprocess.run(["git", "-C", str(root), *a],
                                  capture_output=True, text=True, check=True).stdout.strip()
        except Exception:
            return None
    commit = g("rev-parse", "HEAD")
    if not commit:
        return None
    return {
        "commit": commit,
        "short": g("rev-parse", "--short", "HEAD") or commit[:7],
        "committed_at": g("log", "-1", "--format=%cI"),
        "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(g("status", "--porcelain")),
    }


def graph_freshness(name: str, live_root=None) -> dict:
    """Насколько граф name отстал от текущего git-состояния рабочего дерева.

    Состояния: fresh | dirty | behind | diverged | unknown. live_root — дерево, с
    которым сравниваем (cwd-проект для MCP; meta.root как fallback для CLI).
    """
    meta = load_meta().get(name)
    if not meta or not meta.get("commit"):
        return {"state": "unknown", "reason": "no_meta", "graph": meta or {}}
    root = Path(live_root) if live_root else Path(meta.get("root") or project_root())
    live = git_info(root)
    if not live:
        return {"state": "unknown", "reason": "not_git", "graph": meta, "live": None}
    gc = meta["commit"]
    if live["commit"] == gc:
        return {"state": "dirty" if live["dirty"] else "fresh",
                "behind": 0, "graph": meta, "live": live}
    anc = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", gc, "HEAD"],
                         capture_output=True).returncode == 0
    if not anc:
        return {"state": "diverged", "graph": meta, "live": live}
    n = subprocess.run(["git", "-C", str(root), "rev-list", "--count", f"{gc}..HEAD"],
                       capture_output=True, text=True).stdout.strip()
    try:
        behind = int(n)
    except ValueError:
        behind = None
    return {"state": "behind", "behind": behind, "graph": meta, "live": live}


def freshness_line(name: str, live_root=None) -> str:
    """Однострочная шапка свежести для каждого ответа инструментов."""
    f = graph_freshness(name, live_root)
    st = f["state"]
    short = (f.get("graph") or {}).get("short") or "?"
    if st == "fresh":
        return f"граф @ {short} · свежий"
    if st == "dirty":
        return (f"⚠ граф @ {short} · рабочее дерево изменено, граф не видит несохранённого — "
                f"закоммить (хук перельёт) или вручную `graphify . && grafit load`")
    if st == "behind":
        n = f.get("behind")
        dirty = (f.get("live") or {}).get("dirty")
        tail = " · дерево грязное" if dirty else ""
        return f"⚠ граф @ {short} · HEAD +{n if n is not None else '?'} коммит(ов){tail} — перезалей `grafit load`"
    if st == "diverged":
        return f"⚠ граф @ {short} · другая ветка/история — перезалей `grafit load`"
    if f.get("reason") == "not_git":
        return f"граф @ {short} (вне git — свежесть не отслеживается)"
    return "граф без метки свежести (залит старой версией — перезалей `grafit load`)"


def freshness_report(name: str, live_root=None) -> str:
    """Многострочный отчёт свежести для `grafit status` / grafit_status."""
    f = graph_freshness(name, live_root)
    m = f.get("graph") or {}
    head = (f"граф '{name}'  ({m.get('model', '?')}, "
            f"{m.get('nodes', '?')} узлов / {m.get('edges', '?')} рёбер)")
    if not m:
        return head + "\n  нет метаданных сборки — перезалей: grafit load"
    built = m.get("built_at", "?")
    short = m.get("short", "?")
    branch = m.get("branch", "?")
    lines = [head, f"  построен:  {built}  на коммите {short} ({branch})"]
    if m.get("dirty_at_build"):
        lines.append("  внимание:  дерево было ГРЯЗНЫМ при сборке (часть правок не в графе)")
    live = f.get("live")
    st = f["state"]
    if st == "fresh":
        lines.append(f"  сейчас:    HEAD = {short} · совпадает, дерево чистое")
        lines.append("  вывод:     граф свежий ✓")
    elif st == "dirty":
        lines.append(f"  сейчас:    HEAD = {short} · совпадает, но дерево грязное  ⚠")
        lines.append("  вывод:     закоммить и перезалей, либо граф не видит правок: grafit load")
    elif st == "behind":
        ls = live.get("short", "?") if live else "?"
        lb = live.get("branch", "?") if live else "?"
        warn = " · дерево грязное  ⚠" if (live or {}).get("dirty") else ""
        lines.append(f"  сейчас:    HEAD = {ls} ({lb}) · +{f.get('behind')} коммита(ов){warn}")
        lines.append("  вывод:     граф устарел — перезалей: grafit load")
    elif st == "diverged":
        ls = live.get("short", "?") if live else "?"
        lines.append(f"  сейчас:    HEAD = {ls} · не потомок коммита сборки (ребейз/смена ветки)  ⚠")
        lines.append("  вывод:     история разошлась — перезалей: grafit load")
    else:
        lines.append("  сейчас:    вне git — свежесть не отслеживается")
    return "\n".join(lines)


def embed_url() -> str | None:
    """URL общего сервиса эмбеддингов: env GRAFIT_EMBED_URL приоритетнее config.json."""
    url = os.environ.get("GRAFIT_EMBED_URL")
    if url:
        return url.rstrip("/")
    cfg = load_config() or {}
    u = cfg.get("embed_url")
    return u.rstrip("/") if u else None


class RemoteEmbedder:
    """Клиент grafit-embed: тот же интерфейс .embed(texts), что у fastembed.TextEmbedding.

    Векторы считает общий контейнер (модель в RAM один раз). urllib — без лишних
    клиентских зависимостей. Возвращает numpy-массивы (нужен .tolist() в search).
    """

    def __init__(self, url: str, model: str, timeout: float = 300.0):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed(self, texts, batch_size: int = 64, parallel=None, **_):
        import json as _json
        import urllib.request

        import numpy as np
        texts = list(texts)
        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            payload = _json.dumps({"texts": chunk, "batch_size": batch_size}).encode("utf-8")
            req = urllib.request.Request(
                self.url + "/embed", data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = _json.loads(r.read())
            for v in data["embeddings"]:
                yield np.asarray(v, dtype="float32")


def _probe_embed(url: str, timeout: float = 3.0) -> dict | None:
    """GET /health общего сервиса. None, если недоступен."""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return _json.loads(r.read())
    except Exception:
        return None


def fastembed_version() -> str | None:
    try:
        import fastembed
        return getattr(fastembed, "__version__", None)
    except Exception:
        return None


def make_embedder(model_name: str, threads: int = 4):
    """Общий сервис эмбеддингов, если он сконфигурирован и совместим; иначе локальный fastembed.

    Безопасность векторов: remote используется ТОЛЬКО если совпадают и модель, и версия
    fastembed (вектора зависят от реализации — напр. CLS↔mean pooling между версиями).
    Эталон версии — config['fastembed'] (версия, на которой собран индекс). При
    недоступности/несовпадении честно предупреждаем и грузим модель локально.
    """
    url = embed_url()
    if url:
        health = _probe_embed(url)
        cfg = load_config() or {}
        want_fe = cfg.get("fastembed")
        if health is None:
            print(f"⚠ grafit-embed {url} недоступен — гружу модель локально "
                  f"(подними сервис: `grafit up`)")
        elif health.get("model") != model_name:
            print(f"⚠ grafit-embed отдаёт модель '{health.get('model')}', а нужна "
                  f"'{model_name}' — векторы бы не сошлись; гружу локально")
        elif want_fe and health.get("fastembed") and health["fastembed"] != want_fe:
            print(f"⚠ grafit-embed на fastembed {health['fastembed']}, индекс собран на "
                  f"{want_fe} — вектора не сойдутся; гружу локально (или перезалей `--no-cache`)")
        else:
            return RemoteEmbedder(url, model_name)
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=model_name, threads=threads)


def pick_model() -> str:
    from fastembed import TextEmbedding
    supported = {m["model"] for m in TextEmbedding.list_supported_models()}
    for name in PREFERRED:
        if name in supported:
            return name
    for m in TextEmbedding.list_supported_models():
        if "multilingual" in m["model"].lower():
            return m["model"]
    return next(iter(supported))


def connect(host: str, port: int):
    from falkordb import FalkorDB
    return FalkorDB(host=host, port=port)


def list_graphs(host: str, port: int) -> list[str]:
    db = connect(host, port)
    try:
        return db.connection.execute_command("GRAPH.LIST")
    except Exception:
        return list(load_registry().keys())


def read_snippet(root, source_file, loc, window=12, max_chars=500, cache=None) -> str:
    """Сниппет исходника вокруг source_location ('L18' или 'L18-L30'). Кэш по файлу."""
    if not source_file or not loc:
        return ""
    nums = re.findall(r"\d+", str(loc))
    if not nums:
        return ""
    start = int(nums[0])
    end = int(nums[1]) if len(nums) > 1 else None
    p = (Path(root) / source_file) if root else Path(source_file)
    key = str(p)
    if cache is None:
        cache = {}
    if key not in cache:
        try:
            cache[key] = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            cache[key] = None
    lines = cache[key]
    if not lines:
        return ""
    b = max(0, start - 1)
    e = min(len(lines), end if end else b + window)
    e = max(e, b + 1)
    return "\n".join(lines[b:e]).strip()[:max_chars]


# Парсер CYPHER-заголовка FalkorDB отвергает строковые параметры с сырыми
# управляющими байтами (NUL, прочие C0 кроме \t\n\r, DEL, C1) — запрос падает с
# «Failed to parse query parameter». Такие байты попадают из исходников (например
# регэксп `/[\x00-\x1f\x7f]/`, записанный буквальными control-символами). Семантики
# они не несут, поэтому вычищаем их из любой строки до отправки в граф/эмбеддер.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def strip_control(s):
    """Убрать управляющие символы (кроме \\t\\n\\r), ломающие парсер параметров FalkorDB."""
    if not isinstance(s, str):
        return s
    return _CONTROL_RE.sub("", s)


# --- кэш эмбеддингов (инкрементальная заливка) ---
# Ключ = sha1(модель + '\0' + текст узла). Один вектор на уникальный текст;
# перезаливка считает только новые/изменённые узлы.

def text_hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\x00{text}".encode("utf-8")).hexdigest()


# Бэкенд кэша — Redis-hash в том же FalkorDB (key `grafit:emb:<model>`, field=text_hash,
# value=сырые float32-байты). Преимущества против прежнего общего `<model>.npz`:
#   • запись по полю (HSET) атомарна — параллельные `grafit load` дописывают свои
#     вектора, не затирая чужие (раньше целиковый np.savez давал last-writer-wins + рваный файл);
#   • кросс-процессный (все агенты видят один кэш), переживает рестарт (volume FalkorDB /data).
# ВАЖНО: falkordb.FalkorDB.connection создаётся с decode_responses=True и испортил бы
# бинарь — поэтому держим ОТДЕЛЬНЫЙ redis-клиент с decode_responses=False к тому же порту.

def _emb_key(model: str) -> str:
    return f"grafit:emb:{sanitize(model)}"


def emb_redis(host: str, port: int):
    """Бинарный redis-клиент к тому же FalkorDB (None при недоступности — кэш просто off)."""
    try:
        import redis
        c = redis.Redis(host=host, port=port, decode_responses=False)
        c.ping()
        return c
    except Exception:
        return None


def emb_cache_get(conn, model: str, hashes: list) -> dict:
    """hash -> np.float32-вектор для присутствующих ключей. {} при недоступности."""
    if conn is None or not hashes:
        return {}
    import numpy as np
    uniq = list(dict.fromkeys(hashes))   # без дублей в HMGET
    try:
        vals = conn.hmget(_emb_key(model), uniq)
    except Exception:
        return {}
    out = {}
    for h, v in zip(uniq, vals):
        if v:
            out[h] = np.frombuffer(v, dtype="float32")
    return out


def emb_cache_put(conn, model: str, mapping: dict) -> None:
    """Дописать новые вектора (HSET по полю — атомарно, без wipe)."""
    if conn is None or not mapping:
        return
    import numpy as np
    payload = {h: np.asarray(list(v), dtype="float32").tobytes() for h, v in mapping.items()}
    try:
        conn.hset(_emb_key(model), mapping=payload)
    except Exception:
        pass


# Генерик/шумовые узлы из AST (типы, дженерик-параметры, xUnit-атрибуты, примитивы).
GENERIC_LABELS = {
    "task", "cancellationtoken", "fact", "theory", "inlinedata", "memberdata",
    "guid", "string", "int", "bool", "void", "object", "var", "double", "long",
    "float", "decimal", "byte", "char", "datetime", "datetimeoffset", "timespan",
    "func", "action", "predicate", "ienumerable", "list", "ilist", "dictionary",
    "idictionary", "icollection", "exception", "task<bool>", "task<guid>",
    "httpclient", "httpcontext", "httprequest", "httpresponse", "jsonserializeroptions",
    "imapper", "iconfiguration", "iserviceprovider", "irequest", "irequesthandler",
    "ivalidator", "abstractvalidator", "trequest", "tresponse", "tresult", "tkey",
    "tvalue", "tentity", "tcommand", "stringcomparison", "jsonelement", "jsondocument",
    "type", "nullable",
}


def is_generic(label: str | None) -> bool:
    """Шумовой/генерик-узел (тип, дженерик-параметр, примитив, xUnit-атрибут)?"""
    if not label:
        return True
    l = label.strip().lower().rstrip("()")
    if l in GENERIC_LABELS:
        return True
    raw = label.strip()
    if len(raw) <= 2 and raw.isalpha() and raw[0].isupper():  # дженерик-параметры T, K, TV
        return True
    return False


def is_test_path(sf: str | None) -> bool:
    """Узел из тестов? (Tests/, *.test.*, *.spec.*, *Tests.cs, e2e-tests)."""
    if not sf:
        return False
    s = sf.lower()
    if ".test." in s or ".spec." in s or s.endswith("tests.cs") or s.endswith("test.cs"):
        return True
    for p in s.split("/"):
        if p in ("tests", "test") or p.endswith("-tests") or p.endswith("-test") or p.endswith(".tests"):
            return True
    return False


# Структурные связи (извлечены из AST — высокое доверие) vs выводные (по смыслу/LLM).
STRUCTURAL_RELATIONS = {
    "calls", "references", "method", "contains", "imports", "imports_from",
    "implements", "inherits", "defines", "re_exports",
}


def relation_kind(rel: str | None) -> str:
    """'structural' (AST, высокое доверие) или 'inferred' (выводная связь по смыслу)."""
    return "structural" if (rel or "").lower() in STRUCTURAL_RELATIONS else "inferred"


# Пути сгенерированного/несемантического кода для режима prod (#7).
_NONPROD_PARTS = ("migrations", "obj", "bin", "node_modules", "dist", "__generated__")


# Слой узла по расширению (сильнее каталога — язык однозначнее имени папки),
# с фолбэком на сегмент пути для неоднозначных случаев.
_FRONT_EXT = {"tsx", "jsx", "ts", "js", "mjs", "cjs", "vue", "svelte",
              "css", "scss", "sass", "less", "html"}
_BACK_EXT = {"cs", "py", "go", "java", "kt", "rb", "php", "rs", "scala",
             "ex", "exs", "c", "cc", "cpp", "h", "hpp", "swift"}
_FRONT_DIRS = {"frontend", "client", "web", "webapp", "ui"}
_BACK_DIRS = {"backend", "server", "api"}


def layer_of(sf: str | None) -> str | None:
    """frontend | backend | None — слой узла по пути исходника (для kind-фильтра)."""
    s = (sf or "").lower()
    base = s.rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    if ext in _BACK_EXT:
        return "backend"
    if ext in _FRONT_EXT:
        return "frontend"
    segs = s.split("/")
    if any(p in _BACK_DIRS for p in segs):
        return "backend"
    if any(p in _FRONT_DIRS for p in segs):
        return "frontend"
    return None


def kind_matches(sf: str | None, ft: str | None, kind: str) -> bool:
    """Подходит ли узел под режим фильтра: all|code|tests|docs|prod|frontend|backend."""
    if kind == "all":
        return True
    is_test = is_test_path(sf)
    if kind == "tests":
        return is_test
    if kind == "docs":
        return (ft or "") in ("document", "concept", "rationale")
    if kind == "code":
        return (ft or "") == "code" and not is_test
    if kind in ("frontend", "backend"):
        # слой = только код (не тесты/доки), по стороне исходника
        return (ft or "") == "code" and not is_test and layer_of(sf) == kind
    if kind == "prod":
        if (ft or "") != "code" or is_test:
            return False
        s = (sf or "").lower()
        if ".generated." in s:
            return False
        return not any(p in s.split("/") for p in _NONPROD_PARTS)
    return True


# --- UI-текст из frontend-исходников (надписи экранов для семантического поиска) ---
# JSX-текст между тегами, значения UI-атрибутов и многословные строковые литералы кладём
# в текст узла → «найди экран по надписи» работает (узлом графа сам текст не является).
# Тугие фильтры отсекают classNames, пути, идентификаторы. Только frontend-код.
_JSX_TEXT_RE = re.compile(r">\s*([^<>{}\n][^<>{}\n]*?)\s*<")
_UI_ATTR_RE = re.compile(
    r"""\b(?:placeholder|title|label|aria-label|alt|tooltip|heading|header|caption|"""
    r"""subtitle|description)\s*=\s*["']([^"'\n{}]{2,60})["']""", re.I)
_QUOTED_RE = re.compile(r"""["']([^"'\n]{3,60})["']""")
_CYR = re.compile(r"[А-Яа-яЁё]")


def _looks_code(s: str) -> bool:
    return bool(re.search(r"[=;(){}\[\]<>]|=>|&&|\|\||\$\{", s))


# tailwind/className-токен: начинается с буквы/цифры/!, дальше только css-символы (вкл. точку
# и слэш — `px-2.5`, `first:pt-0`, `!p-0`, `w-1/2`). Строка из ОДНИХ таких токенов = не UI-текст.
_CSS_TOKEN = re.compile(r"[!a-z0-9][a-z0-9:.\-/!]*")


def _is_ui_copy(s: str) -> bool:
    """Строковый литерал похож на человеческую UI-надпись (не className/путь/ключ/идентификатор)?"""
    s = s.strip()
    if not (3 <= len(s) <= 60) or not re.search(r"[^\W\d_]", s):
        return False
    if s[0] in "./@#$%&" or any(ch in s for ch in ("/", "\\", "_")):
        return False
    if _looks_code(s):
        return False
    toks = s.split()
    if toks and all(_CSS_TOKEN.fullmatch(t) for t in toks):           # className
        return False
    if " " not in s and (re.search(r"[.:]", s) or re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", s)):
        return False  # i18n-ключ (menu:changePassword, pagination.pageOf) / одиночный идентификатор
    return True


def _jsx_ok(s: str) -> bool:
    s = s.strip()
    if not (2 <= len(s) <= 60) or not re.search(r"[^\W\d_]", s):
        return False
    if _looks_code(s) or s.split(" ", 1)[0] in ("import", "export", "const", "return", "function"):
        return False
    if " " not in s and (re.search(r"[.:_/]", s)
                         or (not _CYR.search(s) and re.search(r"[a-z][A-Z]|[A-Z]{3,}", s))):
        return False  # ключ/путь или camelCase/UPPER идентификатор без пробела — вероятно код
    return True


def extract_ui_text(root, sf, loc, cache=None, max_chars=240) -> str:
    """Значимые надписи из тела frontend-узла (JSX-текст + UI-атрибуты + многословные литералы)."""
    if layer_of(sf) != "frontend" or is_test_path(sf) or not loc:
        return ""
    nums = re.findall(r"\d+", str(loc))
    if not nums:
        return ""
    p = (Path(root) / sf) if root else Path(sf)
    key = str(p)
    if cache is None:
        cache = {}
    if key not in cache:
        try:
            cache[key] = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            cache[key] = None
    lines = cache[key]
    if not lines:
        return ""
    start = int(nums[0])
    end = int(nums[1]) if len(nums) > 1 else start + 80
    body = "\n".join(lines[max(0, start - 1):min(len(lines), end)])
    out, seen, total = [], set(), 0

    def add(s):
        nonlocal total
        s = s.strip()
        k = s.lower()
        if s and k not in seen:
            seen.add(k); out.append(s); total += len(s)

    for m in _JSX_TEXT_RE.finditer(body):
        if total >= max_chars:
            break
        if _jsx_ok(m.group(1)):
            add(m.group(1))
    for m in _UI_ATTR_RE.finditer(body):
        if total >= max_chars:
            break
        add(m.group(1))
    for m in _QUOTED_RE.finditer(body):
        if total >= max_chars:
            break
        if _is_ui_copy(m.group(1)):
            add(m.group(1))
    return " | ".join(out)


def node_text(n: dict, comm_labels: dict, root=None, cache=None, snippets=True,
              include_community=True) -> str:
    # Порядок: label → сниппет → путь → сообщество → тип. label+сниппет первыми,
    # чтобы при обрезке по лимиту токенов не терялось имя символа + сигнатура.
    # include_community=False — для текста, по которому считается эмбеддинг/ключ кэша:
    # метку сообщества graphify регенерирует при каждой пересборке (недетерминированная
    # кластеризация), и её участие в ключе инвалидировало бы кэш целиком (см. index.py).
    parts = [n.get("label", "")]
    if snippets:
        snip = read_snippet(root, n.get("source_file"), n.get("source_location"), cache=cache)
        if snip:
            parts.append(snip)
        ui = extract_ui_text(root, n.get("source_file"), n.get("source_location"), cache=cache)
        if ui:
            parts.append(ui)
    sf = n.get("source_file") or ""
    if sf:
        parts.append(sf.replace("/", " ").replace(".", " "))
    if include_community:
        cl = comm_labels.get(str(n.get("community")))
        if cl:
            parts.append(cl)
    if n.get("file_type"):
        parts.append(n["file_type"])
    return strip_control(" | ".join(p for p in parts if p))
