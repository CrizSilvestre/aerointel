#!/usr/bin/env python3
# api_server/app.py — API REST OPCIONAL de AeroIntel sobre la misma BD SQLite que llena el pipeline.
# La producción NO la necesita (GitHub Pages sirve la API estática JSON). Esto es para quien quiera
# una API consultable en vivo (filtros, integración con otros sistemas, panel propio).
#   pip install -r api_server/requirements.txt
#   uvicorn api_server.app:app --reload --port 8000
from __future__ import annotations  # permite "str | None" también en Python 3.9
import os, sys, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store  # noqa: E402  (misma BD aerointel.db que escribe el pipeline)

from fastapi import FastAPI, Query, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app = FastAPI(title="AeroIntel API", version="1.0",
              description="Inteligencia aeronáutica · Hub PUJ (República Dominicana)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


def _rows(q, params=()):
    conn = store.connect()
    try:
        out = [dict(r) for r in conn.execute(q, params).fetchall()]
    finally:
        conn.close()
    for r in out:
        if r.get("entities"):
            try:
                r["entities"] = json.loads(r["entities"])
            except Exception:
                pass
    return out


@app.get("/")
def root():
    return {"service": "AeroIntel API", "hub": "PUJ", "docs": "/docs",
            "endpoints": ["/api/news/latest", "/api/news/categories", "/api/news/sources", "/api/analytics"]}


@app.get("/api/news/latest")
def latest(limit: int = Query(50, ge=1, le=200),
           category: str | None = None,
           puj: bool | None = None,
           min_impact: int = Query(0, ge=0, le=100)):
    """Inteligencia priorizada por impacto. Filtros opcionales: categoría, solo-PUJ, impacto mínimo."""
    q = "SELECT * FROM articles WHERE impact >= ?"
    p = [min_impact]
    if category:
        q += " AND category = ?"; p.append(category)
    if puj is not None:
        q += " AND affects_puj = ?"; p.append(int(puj))
    q += " ORDER BY impact DESC, last_seen DESC LIMIT ?"; p.append(limit)
    return {"count_param": limit, "items": _rows(q, tuple(p))}


@app.get("/api/news/categories")
def categories():
    return {"categories": _rows(
        "SELECT category, COUNT(*) n, ROUND(AVG(impact),1) avg_impact, MAX(impact) max_impact "
        "FROM articles GROUP BY category ORDER BY n DESC")}


@app.get("/api/news/sources")
def sources():
    return {"sources": _rows(
        "SELECT source, COUNT(*) n, MAX(last_seen) last_seen FROM articles "
        "GROUP BY source ORDER BY n DESC")}


@app.get("/api/news/{link:path}")
def by_link(link: str):
    rows = _rows("SELECT * FROM articles WHERE link = ?", (link,))
    if not rows:
        raise HTTPException(404, "artículo no encontrado")
    return rows[0]


@app.get("/api/analytics")
def analytics():
    conn = store.connect()
    try:
        return store.analytics(conn)
    finally:
        conn.close()
