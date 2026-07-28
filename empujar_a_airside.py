#!/usr/bin/env python3
"""Empuja lo que AeroIntel ya sabe hacia Airside.

Por qué en esta dirección y no al revés: AeroIntel corre en Python contra
SQLite, con su propio ciclo de recogida. Si Airside le consultara en vivo,
heredaría su disponibilidad y su latencia para pintar una pantalla. Empujando,
Airside guarda lo último que recibió y lo enseña con su fecha aunque AeroIntel
lleve dos días apagado.

Y por eso mismo, si Airside no responde, esto NO rompe nada: registra y sigue.
AeroIntel no puede quedarse parado porque Airside esté caído.

Uso:
    AIRSIDE_INTEL_CLAVE=... python3 empujar_a_airside.py [--dias 30] [--todo]
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
BD = AQUI / "aerointel.db"

# AeroIntel clasifica en español con tilde; Airside espera estas tres.
GRAVEDAD = {"crítico": "critico", "critico": "critico",
            "importante": "importante", "info": "info"}


def _entidades(bruto) -> list[str]:
    """Aerolíneas, aeropuertos y aeronaves en una sola lista de etiquetas."""
    if not bruto:
        return []
    try:
        d = json.loads(bruto)
    except Exception:
        return []
    fuera: list[str] = []
    for clave in ("aerolineas", "aeropuertos", "aeronaves", "vuelos"):
        for v in (d.get(clave) or []):
            v = str(v).strip()
            if v and v not in fuera:
                fuera.append(v)
    return fuera[:6]


def leer(dias: int, solo_puj: bool) -> list[dict]:
    if not BD.exists():
        raise SystemExit(f"No encuentro la base de AeroIntel en {BD}")
    con = sqlite3.connect(BD)
    con.row_factory = sqlite3.Row
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()

    sql = "select * from articles where published >= ?"
    args: list = [desde]
    if solo_puj:
        # 346 de 488 afectan a PUJ. El resto es ruido para un panel de PUJ.
        sql += " and affects_puj = 1"
    sql += " order by published desc limit 300"

    # Las dos pertenencias que ordenan la portada de AeroIntel y que Airside
    # necesita para sus filtros. Se calculan con las MISMAS reglas de AeroIntel
    # —su regex y su columna—, no con una copia que se quedaría vieja.
    from relevancia import PUJ_DIRECT_RE

    salida = []
    for r in con.execute(sql, args):
        # La mención se busca en el TITULAR, como hace AeroIntel. Buscarla en el
        # "porqué" contamina: ese texto es análisis de AeroIntel y nombra a PUJ
        # casi siempre — salían 295 de 300 y el filtro no cortaba nada.
        texto = r["title"] or ""
        salida.append({
            "id": r["link"],
            "titulo": r["title"],
            "enlace": r["link"],
            "fuente": r["source"],
            "categoria": r["category"],
            "gravedad": GRAVEDAD.get((r["severity"] or "").lower(), "info"),
            "impacto": r["impact"],
            "porque": r["why"],          # lo que convierte un titular en información
            "publicado": r["published"],
            "imagen": r["image"],
            # Las entidades vienen como JSON de texto. Se aplanan aquí y no en
            # Airside: quien conoce el formato es quien lo escribió.
            "entidades": _entidades(r["entities"]),
            "puj": bool(PUJ_DIRECT_RE.search(texto)),   # mención directa del aeropuerto
            "rd": r["dr_tier"] == "core",               # noticia del país
        })
    return salida


# El sitio publicado de AeroIntel. Su pipeline corre en GitHub Actions —donde
# SÍ viven las claves, como secretos— y expone el resultado ya normalizado en
# /api/notams.json. Cuando la recogida local falla (la FAA bloquea IPs
# residenciales a ratos, y la clave de SkyLink no se guarda en esta máquina),
# ahí está el mismo dato, recogido por quien sí pudo.
AEROINTEL_PUBLICADO = os.environ.get(
    "AEROINTEL_API_URL", "https://crizsilvestre.github.io/aerointel")


def _notams_publicados() -> list[dict]:
    url = AEROINTEL_PUBLICADO.rstrip("/") + "/api/notams.json"
    with urllib.request.urlopen(url, timeout=20) as r:
        d = json.loads(r.read())
    lista = d.get("notams") or []
    cuando = d.get("generated_at", "¿?")
    print(f"  NOTAM: {len(lista)} del sitio publicado (generado {cuando})")
    return lista


def leer_notams() -> list[dict]:
    """NOTAMs de MDPC, traducidos al formato de Airside. Primero la recogida
    local (FAA, respaldo SkyLink); si no hay, el sitio publicado de AeroIntel,
    que es el MISMO dato recogido por el pipeline con sus claves. La
    clasificación —sujeto, importancia, CIERRE— es la de AeroIntel: aquí solo
    se traduce, no se opina."""
    from notams import fetch_notams
    crudos, err = fetch_notams()
    if err or not crudos:
        if err:
            print(f"  NOTAM: recogida local sin datos ({err}) — probando el sitio publicado", file=sys.stderr)
        try:
            crudos = _notams_publicados()
        except Exception as e:
            print(f"  NOTAM: el sitio publicado tampoco ({type(e).__name__}: {e})", file=sys.stderr)
            return []
    fuera = []
    for n in crudos:
        if n.get("status") == "expirado":
            continue                      # un NOTAM vencido no es información, es ruido
        etiquetas = [n.get("location") or "", n.get("tipo") or ""]
        if n.get("cierre"):
            etiquetas.append("CIERRE")
        fuera.append({
            "id":        f'{n.get("location","MDPC")} {n["id"]}',
            "codigo":    n["id"],                       # A429/2026 — la ficha lo lleva en grande
            "titulo":    n.get("body") or n.get("raw") or n["id"],
            "enlace":    n.get("source_url"),
            "fuente":    n.get("source"),
            "categoria": n.get("subject"),
            # Traducción de la clasificación de AeroIntel, no una opinión nueva:
            # CIERRE explícito → crítico; importancia alta → importante.
            "gravedad":  "critico" if n.get("cierre")
                         else ("importante" if n.get("importance") == "alta" else "info"),
            "porque":    n.get("lectura"),
            "publicado": n.get("effective"),
            "estado":    n.get("status"),               # vigente | programado
            "desde":     n.get("effective"),
            "hasta":     None if n.get("permanent") else n.get("expiration"),
            "crudo":     n.get("raw") or None,          # el texto tal cual lo publicó la autoridad
            "entidades": [e for e in etiquetas if e],
        })
    return fuera


def leer_nas() -> list[dict]:
    """Estado del espacio aéreo de EE.UU. (Ground Stops, demoras, cierres) vía
    nas.py. `puj_route` lo calcula AeroIntel: aeropuertos con ruta a PUJ."""
    from nas import fetch_nas
    datos, err = fetch_nas()
    if err or not datos:
        print(f"  NAS: sin datos ({err})", file=sys.stderr)
        return []
    fuera = []
    for e in datos.get("events", []):
        etiquetas = ["RUTA PUJ"] if e.get("puj_route") else []
        fuera.append({
            "id":        f'{e.get("kind")}-{e.get("airport")}',
            "codigo":    e.get("airport"),              # BOS — la ficha lo lleva en grande
            "titulo":    e.get("label") or e.get("kind"),
            "enlace":    e.get("source_url"),
            "fuente":    e.get("source"),
            # La etiqueta en español ES la categoría: así los filtros salen
            # como en AeroIntel — «Programa de demoras · 5», «Demoras · 7».
            "categoria": e.get("label") or "operaciones",
            "gravedad":  "importante" if e.get("puj_route") else "info",
            "porque":    " · ".join(x for x in (e.get("reason_es"), e.get("detail")) if x),
            "publicado": datos.get("updated") or datos.get("fetched_at"),
            "entidades": etiquetas,
        })
    return fuera


# Pronóstico: Open-Meteo. Sin clave, sin cuota y con modelo propio. Lo pide
# AeroIntel y no el navegador de Airside — la regla del proyecto es que el
# navegador no sale a internet, y por eso existe hasta el proxy de /api/csv.
OPEN_METEO = ("https://api.open-meteo.com/v1/forecast"
              "?latitude=18.567&longitude=-68.363"
              "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
              "&hourly=temperature_2m,precipitation_probability"
              "&forecast_days=7&timezone=America/Santo_Domingo")


def _pronostico() -> "dict | None":
    """Siete días y las próximas horas. Si falla, se devuelve None y el módulo
    enseña sólo la observación: media meteorología es mejor que ninguna."""
    try:
        with urllib.request.urlopen(OPEN_METEO, timeout=20) as r:
            d = json.loads(r.read())
    except Exception as e:
        print(f"  Clima: sin pronóstico ({type(e).__name__}: {e})", file=sys.stderr)
        return None

    dia = d.get("daily") or {}
    hor = d.get("hourly") or {}
    dias = [{"fecha": f, "codigo": c, "max": mx, "min": mn, "lluviaPct": pp}
            for f, c, mx, mn, pp in zip(dia.get("time", []), dia.get("weather_code", []),
                                        dia.get("temperature_2m_max", []),
                                        dia.get("temperature_2m_min", []),
                                        dia.get("precipitation_probability_max", []))]
    # Sólo las próximas 24 h de la curva: 168 puntos en una franja de 900 px no
    # se leen, y el resto ya lo resume la tira de días.
    ahora = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:00")
    horas = list(zip(hor.get("time", []), hor.get("temperature_2m", []),
                     hor.get("precipitation_probability", [])))
    desde = next((i for i, h in enumerate(horas) if h[0] >= ahora), 0)
    return {
        "dias": dias[:7],
        "horas": [{"hora": h, "temp": tp, "lluviaPct": pp}
                  for h, tp, pp in horas[desde:desde + 24]],
        "fuente": "Open-Meteo",
    }


def leer_clima() -> list[dict]:
    """El METAR de MDPC vía clima.py, ENTERO.

    La fuente devuelve el METAR ya descodificado —viento, visibilidad, techo,
    temperatura, QNH, categoría de vuelo— y durante un tiempo esto mandaba
    sólo el texto crudo y lo tiraba todo. Airside tenía que enseñar un
    aeropuerto entero con una línea de texto."""
    from clima import fetch_weather
    d = fetch_weather()
    if not d:
        print("  Clima: sin METAR", file=sys.stderr)
        return []
    m = d.get("metar") or {}
    est = d.get("station") or "MDPC"
    # `visib` llega como "6+" (millas) o como número. Se manda tal cual y que
    # Airside lo presente: traducir unidades aquí sería opinar por él.
    return [{
        "id":        f"METAR {est}",
        "codigo":    est,
        "titulo":    f"METAR {est}",
        "enlace":    f"https://aviationweather.gov/data/metar/?id={est}",
        "fuente":    "aviationweather.gov · NOAA",
        "categoria": "meteo",
        "gravedad":  "info",
        "publicado": m.get("reportTime") or d.get("fetched_at"),
        "crudo":     m.get("rawOb") or "",
        "entidades": [est],
        # El METAR descodificado. Nombres en español porque los lee Airside.
        "metar": {
            "temp":       m.get("temp"),
            "rocio":      m.get("dewp"),
            "vientoDir":  m.get("wdir"),
            "vientoKt":   m.get("wspd"),
            "rafagaKt":   m.get("wgst"),
            "visib":      m.get("visib"),
            "qnh":        m.get("altim"),
            "categoria":  m.get("fltCat"),
            "nubes":      [{"cobertura": c.get("cover"), "baseFt": c.get("base")}
                           for c in (m.get("clouds") or [])],
            "estacion":   m.get("name"),
            "elevM":      m.get("elev"),
        },
        "pronostico": _pronostico(),
    }]


def empujar(url: str, clave: str, tipo: str, items: list[dict]) -> None:
    cuerpo = json.dumps({"tipo": tipo, "items": items, "origen": "aerointel"}).encode()
    pet = urllib.request.Request(
        url.rstrip("/") + "/api/inteligencia",
        data=cuerpo, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + clave},
    )
    with urllib.request.urlopen(pet, timeout=20) as r:
        print("  Airside responde:", json.loads(r.read()))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("AIRSIDE_URL", "http://localhost:8202"))
    p.add_argument("--dias", type=int, default=365)
    p.add_argument("--todo", action="store_true",
                   help="mandar también lo que no afecta a PUJ")
    a = p.parse_args()

    clave = os.environ.get("AIRSIDE_INTEL_CLAVE")
    if not clave:
        print("Falta AIRSIDE_INTEL_CLAVE. Sin clave, Airside rechaza la ingesta "
              "—y hace bien.", file=sys.stderr)
        return 2

    # Cada tipo por separado y con fallo suave: que la FAA no responda no puede
    # dejar sin noticias a Airside, ni al revés. Se manda lo que se tenga.
    lotes: list[tuple[str, list[dict]]] = [
        ("noticias", leer(a.dias, solo_puj=not a.todo)),
    ]
    for tipo, lector in (("notam", leer_notams), ("nas", leer_nas), ("clima", leer_clima)):
        try:
            lotes.append((tipo, lector()))
        except Exception as e:
            print(f"  {tipo}: no se pudo leer ({type(e).__name__}: {e})", file=sys.stderr)

    fallo = 0
    for tipo, items in lotes:
        print(f"AeroIntel → Airside · {tipo}: {len(items)} items")
        if not items:
            print("  nada que mandar")
            continue
        try:
            empujar(a.url, clave, tipo, items)
        except urllib.error.HTTPError as e:
            # No se rompe el ciclo de AeroIntel porque Airside diga que no.
            print(f"  Airside rechazó ({e.code}): {e.read().decode()[:200]}", file=sys.stderr)
            fallo = 1
        except Exception as e:
            print(f"  Airside no responde: {e}", file=sys.stderr)
            fallo = 1
    return fallo


if __name__ == "__main__":
    raise SystemExit(main())
