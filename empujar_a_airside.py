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

    salida = []
    for r in con.execute(sql, args):
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
        })
    return salida


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

    items = leer(a.dias, solo_puj=not a.todo)
    print(f"AeroIntel → Airside: {len(items)} artículos de los últimos {a.dias} días")
    if not items:
        print("  nada que mandar")
        return 0

    try:
        empujar(a.url, clave, "noticias", items)
    except urllib.error.HTTPError as e:
        # No se rompe el ciclo de AeroIntel porque Airside diga que no.
        print(f"  Airside rechazó ({e.code}): {e.read().decode()[:200]}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"  Airside no responde: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
