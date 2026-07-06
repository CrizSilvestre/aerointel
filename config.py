#!/usr/bin/env python3
# config.py — Configuración compartida de AeroIntel: rutas, identificadores de cliente (UA)
# y el helper de carga de JSON. QUÉ TOCAR AQUÍ: rutas de salida y user-agents.
import os, json

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")

# UA propio para las APIs de LLM (transparente y sin problema).
UA = "AeroIntel/0.1 (+local MVP)"
# UA de navegador para los FEEDS: algunos medios (p. ej. FlightGlobal) devuelven 403 a bots
# declarados.
FEED_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_json(name, default):
    p = os.path.join(HERE, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default
