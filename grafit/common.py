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
CACHE_DIR = HOME / "cache"                 # контент-адресуемый кэш эмбеддингов (по модели)

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


# --- кэш эмбеддингов (инкрементальная заливка) ---
# Ключ = sha1(модель + '\0' + текст узла). Один вектор на уникальный текст;
# перезаливка считает только новые/изменённые узлы.

def text_hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\x00{text}".encode("utf-8")).hexdigest()


def _cache_path(model: str) -> Path:
    return CACHE_DIR / f"{sanitize(model)}.npz"


def load_emb_cache(model: str) -> dict:
    """hash -> вектор (numpy). Пусто, если кэша нет."""
    p = _cache_path(model)
    if not p.exists():
        return {}
    import numpy as np
    with np.load(p, allow_pickle=True) as d:
        keys = d["keys"].tolist()   # каждый массив читаем РОВНО один раз
        vecs = d["vecs"]            # (иначе NpzFile перечитывает файл на каждом [i] -> RAM-бомба)
    return {k: vecs[i] for i, k in enumerate(keys)}


def save_emb_cache(model: str, mapping: dict) -> None:
    if not mapping:
        return
    import numpy as np
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    keys = np.array(list(mapping.keys()))
    vecs = np.array([list(v) for v in mapping.values()], dtype="float32")
    np.savez(_cache_path(model), keys=keys, vecs=vecs)


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


def node_text(n: dict, comm_labels: dict, root=None, cache=None, snippets=True) -> str:
    # Порядок: label → сниппет → путь → сообщество → тип. label+сниппет первыми,
    # чтобы при обрезке по лимиту токенов не терялось имя символа + сигнатура.
    parts = [n.get("label", "")]
    if snippets:
        snip = read_snippet(root, n.get("source_file"), n.get("source_location"), cache=cache)
        if snip:
            parts.append(snip)
    sf = n.get("source_file") or ""
    if sf:
        parts.append(sf.replace("/", " ").replace(".", " "))
    cl = comm_labels.get(str(n.get("community")))
    if cl:
        parts.append(cl)
    if n.get("file_type"):
        parts.append(n["file_type"])
    return " | ".join(p for p in parts if p)
