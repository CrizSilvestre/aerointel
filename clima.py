#!/usr/bin/env python3
# clima.py — METAR de la estación, traído SERVER-SIDE. aviationweather.gov dejó de permitir
# CORS en el navegador; el pipeline lo obtiene cada corrida (el METAR se emite cada hora →
# el cron de 30 min lo mantiene fresco) y lo publica en /api/weather.json para lectura
# mismo-origen. QUÉ TOCAR AQUÍ: la estación (icao) o la fuente del METAR.
import json
from datetime import datetime, timezone

from ingesta import fetch


def fetch_weather(icao="MDPC"):
    """Devuelve {"fetched_at", "station", "metar": <objeto aviationweather>} o None si falla."""
    try:
        raw = json.loads(fetch(f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json",
                               timeout=15))
        if raw:
            return {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "station": icao, "metar": raw[0]}
        print(f"  ⚠ METAR ({icao}): respuesta vacía — el dashboard usará Open-Meteo en vivo")
    except Exception as e:
        print(f"  ⚠ METAR ({icao}): {type(e).__name__}: {e} — el dashboard usará Open-Meteo en vivo")
    return None
