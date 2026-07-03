#!/usr/bin/env python3
# apiexport.py — API estática JSON de AeroIntel. El pipeline la regenera cada corrida y
# GitHub Pages la sirve (cero servidor, cero ops). Rutas:
#   /api/news/latest.json       lista priorizada de inteligencia
#   /api/news/categories.json   conteos + notas por categoría
#   /api/news/sources.json      fuentes y volumen
#   /api/analytics.json         métricas históricas (desde SQLite)
# El mismo esquema lo consume el FastAPI opcional (api_server/) sobre la misma BD.
import os, json
from datetime import datetime, timezone


def _event_public(ev, human_age):
    a, it = ev["analysis"], ev["items"][0]
    return {
        "id": it["link"],
        "titular": a.get("titular") or it["title"],
        "categoria": a["categoria"],
        "severidad": a["severidad"],
        "impact_score": a["impact_score"],
        "affects_puj": bool(a.get("affects_puj")),      # criterio amplio (aerolíneas con operación en PUJ)
        "puj_direct": bool(a.get("puj_direct")),        # mención directa del hub (Punta Cana/PUJ/MDPC)
        "dr_tier": a.get("dr_tier"),
        "angulo_editorial": a.get("angulo_editorial", ""),
        "resumen": a.get("resumen", ""),
        "entidades": a.get("entidades") or {},
        "aerolineas": a.get("aerolineas") or [],
        "imagen": ev.get("image_url"),
        "fuente": it["source"],
        "n_fuentes": len({i["source"] for i in ev["items"]}),
        "url": it["link"],
        "publicado": ev["dt"].isoformat() if ev.get("dt") else None,
        "antiguedad": human_age(ev.get("dt")),
    }


def write_api(events, out_dir, sources, analytics_data, human_age):
    api = os.path.join(out_dir, "api")
    news = os.path.join(api, "news")
    os.makedirs(news, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pub = [_event_public(ev, human_age) for ev in events]

    def dump(path, obj):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    dump(os.path.join(news, "latest.json"),
         {"generated_at": now, "count": len(pub), "items": pub})

    by_cat = {}
    for e in pub:
        by_cat.setdefault(e["categoria"], []).append(e)
    dump(os.path.join(news, "categories.json"),
         {"generated_at": now,
          "categories": [{"categoria": c, "count": len(v), "items": v} for c, v in
                         sorted(by_cat.items(), key=lambda kv: -len(kv[1]))]})

    src_counts = {}
    for e in pub:
        src_counts[e["fuente"]] = src_counts.get(e["fuente"], 0) + 1
    dump(os.path.join(news, "sources.json"),
         {"generated_at": now,
          "sources": [{"name": s["name"], "type": s["type"],
                       "published": src_counts.get(s["name"], 0)} for s in sources]})

    dump(os.path.join(api, "analytics.json"), analytics_data)
    return len(pub)
