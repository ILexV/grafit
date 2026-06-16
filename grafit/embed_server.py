"""grafit-embed: HTTP-сервис эмбеддингов. Модель грузится ОДИН раз и отвечает всем.

Назначение: убрать N×копий модели в RAM (по копии на каждый MCP-процесс/агента).
Один контейнер держит `intfloat/multilingual-e5-large` в памяти, клиенты (MCP, `grafit
load`, `grafit query`) считают вектор по HTTP. Векторы идентичны локальному fastembed —
это та же модель/реализация, поэтому переиндексация не нужна.

Файл намеренно БЕЗ внутренних импортов grafit: он копируется в контейнер как одиночный
`server.py` (см. cli `_up` и Dockerfile.embed). Зависимости: fastembed, fastapi, uvicorn.

ENV:
  GRAFIT_EMBED_MODEL   модель fastembed (по умолч. intfloat/multilingual-e5-large)
  GRAFIT_EMBED_THREADS число потоков onnxruntime (0/пусто = по умолчанию fastembed)
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from pydantic import BaseModel

MODEL = os.environ.get("GRAFIT_EMBED_MODEL", "intfloat/multilingual-e5-large")
_THREADS_RAW = os.environ.get("GRAFIT_EMBED_THREADS", "").strip()
THREADS = int(_THREADS_RAW) if _THREADS_RAW.isdigit() and int(_THREADS_RAW) > 0 else None

app = FastAPI(title="grafit-embed", version="1.0")

_model = None
_dim: int | None = None


def _load():
    """Лениво-идемпотентная загрузка модели + прогрев (фиксирует размерность)."""
    global _model, _dim
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=MODEL, threads=THREADS)
        _dim = len(next(iter(_model.embed(["warmup"]))))
    return _model


@app.on_event("startup")
def _startup():
    # Грузим при старте, чтобы healthcheck отражал реальную готовность,
    # а первый запрос клиента не платил за загрузку модели.
    _load()


class EmbedRequest(BaseModel):
    texts: list[str]
    batch_size: int | None = 64


@app.get("/health")
def health():
    # fastembed-версия в health: клиент сверяет её с версией, на которой собран индекс,
    # и отказывается от сервиса при расхождении (вектора зависят от реализации).
    import fastembed
    return {"status": "ok" if _model is not None else "loading", "model": MODEL,
            "dim": _dim, "fastembed": getattr(fastembed, "__version__", None)}


@app.post("/embed")
def embed(req: EmbedRequest):
    # Префиксы e5 ("query:"/"passage:") добавляет КЛИЕНТ — сервис эмбеддит текст как есть,
    # чтобы векторы совпадали с тем, что уже лежит в FalkorDB (кэш/индекс не ломаются).
    model = _load()
    vecs = [v.tolist() for v in model.embed(req.texts, batch_size=req.batch_size or 64, parallel=None)]
    return {"model": MODEL, "dim": (len(vecs[0]) if vecs else _dim), "embeddings": vecs}


def main():
    import uvicorn
    port = int(os.environ.get("GRAFIT_EMBED_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
