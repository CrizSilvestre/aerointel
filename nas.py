#!/usr/bin/env python3
# nas.py — Estado del NAS (National Airspace System, FAA): ground stops, programas de demora,
# cierres y demoras de aeropuertos de EE.UU. Fuente oficial: https://nasstatus.faa.gov (el API
# moderno que alimenta esa página). Un ground stop en MIA/JFK/FLL cascadea directo a los
# itinerarios de PUJ — por eso es una categoría operativa, no una curiosidad.
# Solo librería estándar. Si el API falla → ([], err) y la sección no aparece (degradación).
import os, re, ssl, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

NAS_URL = "https://nasstatus.faa.gov/api/airport-status-information"
SOURCE_NAME = "FAA · NAS Status"
SOURCE_URL = "https://nasstatus.faa.gov/"

# Aeropuertos de EE.UU. con servicio directo / relevancia de red hacia PUJ. Un evento aquí
# se marca "Ruta PUJ" y sube al tope de la lista.
PUJ_US_AIRPORTS = {
    "MIA", "FLL", "MCO", "TPA", "PBI", "JFK", "LGA", "EWR", "SWF", "BOS", "PHL", "BWI",
    "IAD", "DCA", "ATL", "CLT", "ORD", "MDW", "DFW", "IAH", "MSP", "DTW", "STL", "PIT",
    "CVG", "CMH", "IND", "BNA", "RDU", "SJU",
}

# Traducción best-effort de las causas de la FAA (texto libre; lo no mapeado queda crudo).
_REASON_ES = [
    (r"thunderstorm", "tormentas eléctricas"),
    (r"low ceiling", "techos bajos"),
    (r"low visib", "baja visibilidad"),
    (r"\bwind\b|winds", "viento"),
    (r"\bsnow|ice\b|icing", "nieve/engelamiento"),
    (r"\brain\b", "lluvia"),
    (r"weather", "meteorología"),
    (r"volume", "volumen de tráfico"),
    (r"staff", "personal ATC"),
    (r"equipment|outage", "falla de equipo"),
    (r"runway", "pista"),
    (r"construction", "obras"),
    (r"security", "seguridad"),
    (r"other", "otra causa"),
]

def _reason_es(raw):
    t = (raw or "").lower()
    for pat, es in _REASON_ES:
        if re.search(pat, t):
            return es
    # Causa en formato NOTAM crudo (empieza con '!'): no ensuciar la tarjeta con el código.
    if t.startswith("!"):
        return "aviso publicado por NOTAM (ver fuente)"
    return raw or "causa no indicada"


def _mk(kind, label, airport, reason, detail, ga_only=False):
    ap = (airport or "").strip().upper()
    return {
        "kind": kind,                       # GS | GDP | CLOSURE | DELAY
        "label": label,                     # etiqueta en español para la tarjeta
        "airport": ap,
        "reason": (reason or "").strip(),
        "reason_es": _reason_es(reason),
        "detail": detail,                   # vigencia/demoras, tal cual las publica la FAA
        "puj_route": ap in PUJ_US_AIRPORTS,
        "ga_only": ga_only,                 # cierre solo aviación general → no afecta comerciales
        "source": SOURCE_NAME,
        "source_url": SOURCE_URL,
    }


def parse_nas(xml_bytes):
    """Parsea el XML de nasstatus.faa.gov → (update_time, [eventos])."""
    root = ET.fromstring(xml_bytes)
    updated = (root.findtext("Update_Time") or "").strip()
    out = []
    # Ground stops: nada despega hacia ese aeropuerto hasta End_Time.
    for p in root.iter("Program"):
        out.append(_mk("GS", "Ground Stop", p.findtext("ARPT"), p.findtext("Reason"),
                       f"hasta {p.findtext('End_Time')}" if p.findtext("End_Time") else "vigente"))
    # Ground Delay Programs: demoras controladas con promedio/máximo.
    for g in root.iter("Ground_Delay"):
        avg, mx = g.findtext("Avg"), g.findtext("Max")
        det = f"demora media {avg}" + (f" · máx {mx}" if mx else "") if avg else "programa activo"
        out.append(_mk("GDP", "Programa de demoras", g.findtext("ARPT"), g.findtext("Reason"), det))
    # Cierres de aeropuerto. Un cierre "TO TRANSIENT GA ACFT" es solo para aviación general
    # en tránsito: se muestra, pero SIN marcar alta (no afecta la operación comercial).
    for c in root.iter("Airport_Closure_List"):
        for a in c.iter("Airport"):
            reopen = a.findtext("Reopen")
            reason = a.findtext("Reason") or ""
            ga = bool(re.search(r"TRANSIENT GA|GA ACFT|GENERAL AVIATION", reason.upper()))
            out.append(_mk("CLOSURE",
                           "Cierre · aviación general" if ga else "Aeropuerto cerrado",
                           a.findtext("ARPT"), reason,
                           f"reapertura {reopen}" if reopen else "cierre vigente", ga_only=ga))
    # Demoras generales de llegada/salida.
    for d in root.iter("Delay"):
        ad = d.find("Arrival_Departure")
        kind_txt = (ad.get("Type") if ad is not None else "") or ""
        mn = ad.findtext("Min") if ad is not None else None
        mx = ad.findtext("Max") if ad is not None else None
        rng = f"{mn}–{mx}" if mn and mx and mn != mx else (mn or mx or "")
        tipo = {"Departure": "salidas", "Arrival": "llegadas"}.get(kind_txt, kind_txt.lower())
        out.append(_mk("DELAY", "Demoras", d.findtext("ARPT"), d.findtext("Reason"),
                       f"{tipo} {rng}".strip()))
    # Orden operativo: primero lo que toca la red PUJ; dentro, cierres/GS > GDP > demoras.
    # Un cierre solo-GA baja al final de su grupo (no toca la operación comercial).
    sev = {"CLOSURE": 0, "GS": 1, "GDP": 2, "DELAY": 3}
    out.sort(key=lambda e: (not e["puj_route"],
                            4 if e.get("ga_only") else sev.get(e["kind"], 9), e["airport"]))
    return updated, out


def _demo_xml():
    return (b"<AIRPORT_STATUS_INFORMATION><Update_Time>Sun Jul 5 20:00:00 2026 GMT</Update_Time>"
            b"<Delay_type><Name>Ground Stop Programs</Name><Ground_Stop_List>"
            b"<Program><ARPT>JFK</ARPT><Reason>thunderstorms</Reason><End_Time>9:30 pm EDT</End_Time></Program>"
            b"<Program><ARPT>SAN</ARPT><Reason>other</Reason><End_Time>9:15 pm EDT</End_Time></Program>"
            b"</Ground_Stop_List></Delay_type>"
            b"<Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List>"
            b"<Ground_Delay><ARPT>MIA</ARPT><Reason>thunderstorms</Reason>"
            b"<Avg>1 hour and 12 minutes</Avg><Max>2 hours and 40 minutes</Max></Ground_Delay>"
            b"</Ground_Delay_List></Delay_type></AIRPORT_STATUS_INFORMATION>")


def fetch_nas(timeout=20):
    """Devuelve ({"updated":…, "events":[…]}, error|None)."""
    if os.environ.get("AEROINTEL_NAS_DEMO", "").lower() in ("1", "true", "yes"):
        upd, ev = parse_nas(_demo_xml())
        return {"updated": upd, "fetched_at": _now(), "events": ev}, None
    req = urllib.request.Request(NAS_URL, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/xml, text/xml, */*"})
    try:
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read()
        except (ssl.SSLError, urllib.error.URLError):
            ctx = ssl._create_unverified_context()
            raw = urllib.request.urlopen(req, timeout=timeout, context=ctx).read()
        upd, ev = parse_nas(raw)
        return {"updated": upd, "fetched_at": _now(), "events": ev}, None
    except Exception as e:
        return {"updated": None, "fetched_at": _now(), "events": []}, f"{type(e).__name__}: {e}"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
