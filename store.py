#!/usr/bin/env python3
# store.py — Persistencia SQLite de AeroIntel (solo librería estándar, cero dependencias/ops).
# Guarda artículos, scores, resúmenes IA, imágenes e historial entre corridas, y deriva analítica.
# La BD es un único archivo (aerointel.db) — ideal para un beta operado por una persona.
import os, json, sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aerointel.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  ingested    INTEGER, published INTEGER, breaking INTEGER, with_image INTEGER,
  llm_used    INTEGER
);
CREATE TABLE IF NOT EXISTS articles (
  link        TEXT PRIMARY KEY,           -- URL canónica = clave de dedup entre corridas
  title       TEXT, source TEXT,
  category    TEXT, severity TEXT, impact INTEGER, affects_puj INTEGER,
  dr_tier     TEXT, why TEXT, resumen TEXT, image TEXT,
  entities    TEXT,                        -- JSON
  published   TEXT, first_seen TEXT, last_seen TEXT,
  seen_count  INTEGER DEFAULT 1,
  peak_impact INTEGER
);
CREATE INDEX IF NOT EXISTS idx_articles_lastseen ON articles(last_seen);
CREATE INDEX IF NOT EXISTS idx_articles_cat ON articles(category);
"""


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_run(conn, ingested, published, breaking, with_image, llm_used):
    cur = conn.execute(
        "INSERT INTO runs (ts, ingested, published, breaking, with_image, llm_used) VALUES (?,?,?,?,?,?)",
        (_now(), ingested, published, breaking, with_image, int(bool(llm_used))))
    conn.commit()
    return cur.lastrowid


def upsert_events(conn, events, canonical):
    """Inserta o actualiza cada evento por su URL canónica. Mantiene first_seen, cuenta de
    apariciones e impacto pico — esto da el 'historial' que pedía la especificación."""
    now = _now()
    for ev in events:
        a, it = ev["analysis"], ev["items"][0]
        link = canonical(it["link"])
        ents = json.dumps(a.get("entidades") or {}, ensure_ascii=False)
        pub = ev["dt"].isoformat() if ev.get("dt") else None
        row = conn.execute("SELECT seen_count, peak_impact, first_seen FROM articles WHERE link=?",
                           (link,)).fetchone()
        title = a.get("titular") or it["title"]
        if row:
            conn.execute(
                """UPDATE articles SET title=?, source=?, category=?, severity=?, impact=?,
                   affects_puj=?, dr_tier=?, why=?, resumen=?, image=?, entities=?, published=?,
                   last_seen=?, seen_count=?, peak_impact=? WHERE link=?""",
                (title, it["source"], a["categoria"], a["severidad"], a["impact_score"],
                 int(bool(a.get("affects_puj"))), a.get("dr_tier"), a.get("angulo_editorial"),
                 a.get("resumen", ""), ev.get("image_url"), ents, pub, now,
                 row["seen_count"] + 1, max(row["peak_impact"] or 0, a["impact_score"]), link))
        else:
            conn.execute(
                """INSERT INTO articles (link, title, source, category, severity, impact, affects_puj,
                   dr_tier, why, resumen, image, entities, published, first_seen, last_seen,
                   seen_count, peak_impact) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (link, title, it["source"], a["categoria"], a["severidad"], a["impact_score"],
                 int(bool(a.get("affects_puj"))), a.get("dr_tier"), a.get("angulo_editorial"),
                 a.get("resumen", ""), ev.get("image_url"), ents, pub, now, now, a["impact_score"]))
    conn.commit()


def analytics(conn):
    """Resumen para /api/analytics: totales, distribución y serie histórica de corridas."""
    def rows(q, *p):
        return [dict(r) for r in conn.execute(q, p).fetchall()]
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return {
        "generated_at": _now(),
        "articles_total": total,
        "by_category": rows("SELECT category, COUNT(*) n, ROUND(AVG(impact),1) avg_impact "
                            "FROM articles GROUP BY category ORDER BY n DESC"),
        "by_severity": rows("SELECT severity, COUNT(*) n FROM articles GROUP BY severity"),
        "puj_share": conn.execute("SELECT COUNT(*) FROM articles WHERE affects_puj=1").fetchone()[0],
        "top_sources": rows("SELECT source, COUNT(*) n FROM articles GROUP BY source ORDER BY n DESC LIMIT 10"),
        "recent_runs": rows("SELECT ts, ingested, published, breaking, with_image FROM runs "
                           "ORDER BY id DESC LIMIT 24"),
    }
