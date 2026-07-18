#!/usr/bin/env python3
# aerointel.py — ORQUESTADOR del pipeline. Solo une las piezas; la lógica vive en módulos.
#
# ¿DÓNDE TOCO QUÉ?  (mapa del código)
#   config.py      → rutas, user-agents, carga de JSON
#   ingesta.py     → descarga/parseo de feeds, fechas, limpieza de titulares, dedup/cluster
#   relevancia.py  → keywords por categoría, severidad, aerolíneas, niveles RD, filtros de ruido
#   analisis.py    → el "porqué" (editorial heurístico), scoring y ajustes de ranking
#   ia.py          → prompts del LLM, proveedores, reintentos/cortacircuito, lectura de NOTAMs
#   imagenes.py    → og:image, filtros anti-placeholder, boost de foto
#   clima.py       → METAR server-side (/api/weather.json)
#   notams.py      → NOTAMs de MDPC (SkyLink): clasificación y lectura operativa
#   nas.py         → estado del NAS de la FAA (ground stops/demoras EE.UU., marca "Ruta PUJ")
#   salida.py      → dashboard, Mattermost (breaking + salud), briefing
#   store.py       → persistencia SQLite e historial
#   apiexport.py   → API estática JSON (/api/news/*, /api/analytics)
#
#   Config editable sin tocar código: sources.json (fuentes), airlines_puj.json (aerolíneas),
#   nas_puj_airports.json (aeropuertos EE.UU. con ruta a PUJ), dashboard_template.html (la web).
#
# Uso:  python3 aerointel.py
#   AEROINTEL_LLM=groq GROQ_API_KEY=…  → análisis editorial con IA (si no: heurística)
#   MATTERMOST_WEBHOOK_URL=…           → publica de verdad (si no: dry-run)
import os, json
from datetime import datetime

import store, apiexport, notams, nas
from config import OUT
from ingesta import (SOURCES, MAX_AGE_H, gnews_url, fetch, parse_feed, age_hours, human_age,
                     cluster, canonical)
from relevancia import is_relevant
from analisis import analyze_heuristic, apply_ranking_adjustments
from ia import upgrade_carousel_llm, interpret_notams_llm, _LLM_STATS
from imagenes import fetch_images_parallel, apply_image_boost
from clima import fetch_weather
from salida import to_mattermost, write_briefing, write_dashboard, post, health_payload, EMOJI


def main():
    os.makedirs(OUT, exist_ok=True)
    prov = os.environ.get("AEROINTEL_LLM", "") or ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "")
    mode = f"LLM · {prov}" if prov else "heurística (sin API key)"
    print(f"AeroIntel · MVP Fase 1 · {datetime.now():%Y-%m-%d %H:%M} · análisis: {mode}\n")

    items, src_health = [], []
    for s in SOURCES:
        url = gnews_url(s) if s["type"] == "gnews" else s["url"]
        try:
            got = parse_feed(fetch(url), s["name"])
            print(f"  ✓ {s['name']}: {len(got)} ítems")
            items += got
            src_health.append({"name": s["name"], "ok": True, "items": len(got), "error": None})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"  ✗ {s['name']}: {err}")
            src_health.append({"name": s["name"], "ok": False, "items": 0, "error": err})

    before = len(items)
    items = [it for it in items if (age_hours(it.get("dt")) or 0) <= MAX_AGE_H]   # solo noticias recientes
    rel = [it for it in items if is_relevant(it["title"] + " " + it["desc"])]
    events = cluster(rel)
    print(f"\nIngestado: {before} · recientes (≤{int(MAX_AGE_H/24)}d): {len(items)} · relevantes: {len(rel)} · eventos: {len(events)}")

    # 1) pre-score barato (heurístico) en TODOS, para decidir a cuáles vale la pena gastar LLM
    for ev in events:
        ev["_txt"] = " ".join(it["title"] for it in ev["items"]) + " " + (ev["items"][0]["desc"] or "")
        ev["dt"] = max((i["dt"] for i in ev["items"] if i.get("dt")), default=None)
        ev["analysis"] = analyze_heuristic(ev["_txt"], len(ev["items"]), ev["dt"])
    events.sort(key=lambda e: e["analysis"]["impact_score"], reverse=True)

    # 2) ajustes deterministas (recencia/ruido/recap/piso RD) + filtro de publicación,
    #    todo sobre la heurística — así sabemos QUÉ se publica antes de gastar un token.
    for ev in events:
        apply_ranking_adjustments(ev)
    events.sort(key=lambda e: e["analysis"]["impact_score"], reverse=True)
    MIN_SCORE = int(os.environ.get("AEROINTEL_MIN_SCORE", "30"))
    n_before = len(events)
    events = [e for e in events if e["analysis"]["impact_score"] >= MIN_SCORE]
    print(f"  Relevancia (score ≥ {MIN_SCORE}): {n_before} → {len(events)} eventos publicables.")

    # 3) imágenes en paralelo (define quién va al carrusel)
    pause = float(os.environ.get("AEROINTEL_LLM_SLEEP", "2"))
    n_img = int(os.environ.get("AEROINTEL_IMG_N", "48"))
    if os.environ.get("AEROINTEL_NO_IMG", "").lower() not in ("1", "true", "yes"):
        fetch_images_parallel(events, n=n_img)

    # 4) IA con PRIORIDAD DE PORTADA: el carrusel es lo primero que ve una persona, así que
    #    los PRIMEROS tokens del presupuesto van a sus historias; después el top por impacto.
    #    Si la cuota muere a mitad de la corrida, lo más visible ya quedó con análisis de IA.
    if prov:
        top = int(os.environ.get("AEROINTEL_LLM_MAX", "20"))
        car_max = int(os.environ.get("AEROINTEL_CAROUSEL_MAX", "6"))
        carousel = [e for e in events[:30] if e.get("image_url")][:car_max]
        car_set = set(map(id, carousel))
        queue = (carousel + [e for e in events if id(e) not in car_set])[:top]
        done = upgrade_carousel_llm(queue, pause, apply_ranking_adjustments)
        print(f"  IA ({prov}): {done}/{len(queue)} eventos analizados — carrusel primero, luego el top.")
        events.sort(key=lambda e: e["analysis"]["impact_score"], reverse=True)
        # la IA puede bajar un score bajo el umbral → segundo filtro (barato) para coherencia
        events = [e for e in events if e["analysis"]["impact_score"] >= MIN_SCORE]

    apply_image_boost(events)              # nota con foto real sube un poco (solo reordena)

    # 4b) NOTAMs activos de la estación (MDPC). Server-side; sin clave/sin suscripción → [] y se omite.
    notam_list, notam_err = notams.fetch_notams()
    if notam_err:
        print(f"  NOTAM ({notams.ICAO_DEFAULT}): omitido — {notam_err}")
    else:
        alta = sum(1 for n in notam_list if n["importance"] == "alta")
        print(f"  NOTAM ({notams.ICAO_DEFAULT}): {len(notam_list)} activos ({alta} de alta importancia)")
        # Lectura operativa con IA (si hay proveedor LLM); si no, queda la heurística.
        if notam_list and prov:
            notam_cap = int(os.environ.get("AEROINTEL_NOTAM_LLM_MAX", "14"))
            done = interpret_notams_llm(notam_list, prov, cap=notam_cap)
            if done:
                print(f"  NOTAM · IA interpretó {done}/{len(notam_list)} (resto: lectura heurística)")

    # 4c') Estado del NAS (FAA): ground stops / demoras en EE.UU. que cascadean a PUJ.
    nas_data, nas_err = nas.fetch_nas()
    if nas_err:
        print(f"  NAS (FAA): omitido — {nas_err}")
    else:
        n_puj = sum(1 for e in nas_data["events"] if e["puj_route"])
        print(f"  NAS (FAA): {len(nas_data['events'])} eventos activos ({n_puj} en red PUJ)")

    # 4c) METAR server-side → /api/weather.json (el navegador ya no puede llamar a
    # aviationweather.gov por CORS; mismo-origen no tiene ese problema).
    wx = fetch_weather()
    api_dir = os.path.join(OUT, "api")
    os.makedirs(api_dir, exist_ok=True)
    json.dump(wx or {"fetched_at": None, "station": None, "metar": None},
              open(os.path.join(api_dir, "weather.json"), "w", encoding="utf-8"), ensure_ascii=False)
    if wx:
        print(f"  METAR ({wx['station']}): {str(wx['metar'].get('rawOb', ''))[:70]}")

    json.dump([to_mattermost(ev) for ev in events],
              open(os.path.join(OUT, "mattermost_payloads.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump(nas_data, open(os.path.join(api_dir, "nas.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    write_briefing(events)
    write_dashboard(events, notam_list, nas_data)

    # 5) persistencia (SQLite) + API estática JSON. Aislado en try: nunca debe tumbar la corrida.
    breaking_n = sum(1 for ev in events if ev["analysis"]["severidad"] in ("crítico", "importante"))
    with_img = sum(1 for ev in events if ev.get("image_url"))
    try:
        conn = store.connect()
        store.upsert_events(conn, events, canonical)
        store.record_run(conn, before, len(events), breaking_n, with_img, bool(prov))
        apiexport.write_api(events, OUT, SOURCES, store.analytics(conn), human_age)
        conn.close()
        json.dump({"icao": notams.ICAO_DEFAULT, "count": len(notam_list), "notams": notam_list},
                  open(os.path.join(OUT, "api", "notams.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"  → SQLite: {os.path.basename(store.DB_PATH)} · API estática: output/api/news/latest.json")
    except Exception as e:
        print(f"  ⚠ persistencia/API omitida ({type(e).__name__}: {e})")

    print("\n══════════ TOP EVENTOS (por impacto) ══════════")
    for ev in events[:8]:
        a, it = ev["analysis"], ev["items"][0]
        flag = " · PUJ" if a.get("affects_puj") else ""
        print(f"\n{EMOJI.get(a['severidad'])} [{a['impact_score']:>3}] {a['categoria'].upper()} · {a['severidad']}{flag}")
        print(f"   {a.get('titular') or it['title']}")
        print(f"   → {a['angulo_editorial']}")
        print(f"   {len({i['source'] for i in ev['items']})} fuente(s) · {it['source']} · 📅 {human_age(ev.get('dt'))}")

    hook = os.environ.get("MATTERMOST_WEBHOOK_URL")
    breaking = [ev for ev in events if ev["analysis"]["severidad"] in ("crítico", "importante")]
    print(f"\n{len(breaking)} evento(s) breaking/update para Mattermost.")
    if hook:
        for ev in breaking[:10]:
            post(hook, to_mattermost(ev))
        print("  → publicados en Mattermost ✓")
    else:
        print("  → DRY-RUN (no se publicó nada). Define MATTERMOST_WEBHOOK_URL para enviar de verdad.")

    # Salud de la corrida: aviso solo si algo falló (fuente caída / NOTAM con clave / LLM degradado).
    src_fails = [h for h in src_health if not h["ok"]]
    notam_alert = notam_err if (notam_err and "sin clave" not in notam_err) else None
    if src_fails or notam_alert or nas_err or _LLM_STATS["fallbacks"]:
        resumen = (f"⚠ salud: {len(src_fails)} fuente(s) caída(s)"
                   + (f" · NOTAM: {notam_alert}" if notam_alert else "")
                   + (f" · NAS: {nas_err}" if nas_err else "")
                   + (f" · LLM→heurística: {_LLM_STATS['fallbacks']}" if _LLM_STATS["fallbacks"] else "")
                   + (f" · reintentos LLM: {_LLM_STATS['retries']}" if _LLM_STATS["retries"] else ""))
        print(f"  {resumen}")
        if hook:
            try:
                post(hook, health_payload(src_fails, len(SOURCES), notam_alert, _LLM_STATS["fallbacks"], nas_err))
                print("  → aviso de salud publicado en Mattermost ✓")
            except Exception as e:
                print(f"  ⚠ no se pudo publicar el aviso de salud ({type(e).__name__}: {e})")
    else:
        print("  ✓ salud: todas las fuentes respondieron.")
    print("  → output/dashboard.html  (panel web)")
    print("  → output/mattermost_payloads.json  (lo que se enviaría)")
    print("  → output/briefing.md  (resumen diario)")


if __name__ == "__main__":
    main()
