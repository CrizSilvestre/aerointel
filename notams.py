#!/usr/bin/env python3
# notams.py — NOTAMs activos de la estación (PUJ = MDPC) vía SkyLink API (RapidAPI).
# La clave SOLO se lee de entorno (RAPIDAPI_KEY) y vive server-side (pipeline / GitHub Actions):
# NUNCA se incrusta en el HTML ni se commitea. Si no hay clave o el API falla, devuelve [] y la
# sección NOTAM simplemente no aparece (degradación elegante, no rompe el dashboard).
import os, re, json, ssl, urllib.request, urllib.error
from datetime import datetime, timezone

ICAO_DEFAULT = os.environ.get("AEROINTEL_NOTAM_ICAO", "MDPC")   # MDPC = Aeropuerto Int. Punta Cana
RAPID_HOST = "skylink-api.p.rapidapi.com"


def _parse_dt(s):
    """Acepta ISO 8601 y el formato NOTAM (YYYYMMDDHHMM, con sufijo EST y tokens PERM/WIE)."""
    if not s:
        return None
    t = str(s).strip().upper()
    if t in ("PERM", "PERMANENT", "UFN", "WIE", "WEF"):
        return None
    if "-" in t or "T" in t:                       # ISO 8601
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
    digits = re.sub(r"\D", "", t)                  # formato NOTAM
    if len(digits) == 10:
        digits = "20" + digits                     # YYMMDDHHMM → YYYYMMDDHHMM
    if len(digits) == 12:
        try:
            return datetime.strptime(digits, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


_SCOPE_ES = {"AERODROME": "Aeródromo", "EN-ROUTE": "En ruta", "ENROUTE": "En ruta",
             "NAV WARNING": "Aviso a la navegación", "AERODROME/EN-ROUTE": "Aeródromo/En ruta",
             "W": "Aviso", "A": "Aeródromo", "E": "En ruta", "AE": "Aeródromo/En ruta"}

def _scope_es(s):
    return _SCOPE_ES.get(str(s or "").strip().upper(), str(s or "").title())


# Clasificación del SUJETO operativo del NOTAM (sobre texto en mayúsculas). El ORDEN importa:
# los equipos específicos (nav/luces/obstáculo) priman sobre la superficie (RWY/TWY) cuando ambos
# aparecen, porque el NOTAM trata del equipo (p. ej. "ILS RWY 08 GP U/S" = ayuda a navegación).
_SUBJECTS = [
    ("Obstáculo",            r"\bOBST\b|OBSTACLE|\bCRANE\b|\bTOWER\b"),
    ("Ayuda a navegación",   r"\bILS\b|\bVOR\b|\bDME\b|\bLOC\b|\bGP\b|\bNDB\b|\bRNAV\b|\bGNSS\b|\bGPS\b|\bGLS\b|\bGBAS\b|\bSBAS\b|\bMLS\b|\bTACAN\b"),
    ("Iluminación",          r"\bLGT\b|LIGHTING|\bPAPI\b|\bALS\b|\bAPAPI\b|\bRTIL\b|\bRCLL\b|\bHIRL\b"),
    ("Combustible",          r"\bFUEL\b|JET\s*A1?|AVGAS"),
    ("Fauna",                r"\bBIRD\b|WILDLIFE|FAUNA"),
    ("Actividad UAS/drones", r"\bUAS\b|\bRPAS\b|UNMANNED|\bDRONE\b|\bUA\b WILL"),
    ("Pista",                r"\bRWY\b|\bRUNWAY\b"),
    ("Calle de rodaje",      r"\bTWY\b|TAXIWAY"),
    ("Plataforma",           r"\bAPRON\b|\bRAMP\b|\bSTAND|PARKING|ACFT STAND"),
    ("Espacio aéreo",        r"AIRSPACE|\bCTR\b|\bTMA\b|\bFIR\b|PROHIBITED|RESTRICTED|\bDANGER\b|LATERAL LIMITS"),
    ("Servicios",            r"\bSER\b|\bSVC\b|\bATIS\b|\bAFIS\b|\bTWR\b|\bAPP\b|\bCUSTOMS\b|\bATC\b"),
]
# Alta importancia: cierres de pista/aeródromo o ayudas críticas fuera de servicio.
_HIGH_RE = re.compile(
    r"(RWY|AD|AERODROME|ARPT|AIRPORT).{0,40}(CLSD|CLOSED)|"
    r"(ILS|GP|LOC|PAPI|GBAS|VOR|DME|GLS|MLS).{0,20}(U/S|UNSERVICEABLE|OUT OF SERVICE|UNAVBL)|"
    r"\bAD\s+CLSD\b|\bCLOSED TO ALL\b|PROHIBITED", re.I)


def classify(text):
    t = (text or "").upper()
    subject = next((name for name, pat in _SUBJECTS if re.search(pat, t)), "General")
    importance = "alta" if _HIGH_RE.search(t) else "media"
    return subject, importance


_TYPE_ES = {"N": "Nuevo", "R": "Reemplazo", "C": "Cancelación"}


def normalize(n, now=None):
    now = now or datetime.now(timezone.utc)
    eff, exp = _parse_dt(n.get("effective")), _parse_dt(n.get("expiration"))
    permanent = str(n.get("expiration") or "").strip().upper() in ("PERM", "PERMANENT", "UFN")
    if exp and exp < now:
        status = "expirado"
    elif eff and eff > now:
        status = "programado"
    else:
        status = "vigente"
    body = (n.get("body") or "").strip()
    raw = (n.get("raw") or "").strip()
    subject, importance = classify(body + " " + raw)
    return {
        "id": n.get("notam_id") or n.get("notam_id_domestic") or "—",
        "tipo": _TYPE_ES.get((n.get("type") or "").upper(), n.get("type") or ""),
        "location": n.get("location") or "",
        "subject": subject,
        "importance": importance,
        "status": status,
        "effective": eff.isoformat() if eff else None,
        "expiration": exp.isoformat() if exp else None,
        "permanent": permanent,
        "body": body or raw,
        "raw": raw,
        # SkyLink no trae 'source'; usamos el alcance (scope) traducido como etiqueta de origen.
        "source": n.get("source") or _scope_es(n.get("scope")),
    }


def _demo_raw():
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    def iso(d):
        return d.isoformat() if d else None
    return [
        {"notam_id": "A1450/26", "type": "N", "location": "MDPC", "effective": iso(now - timedelta(days=2)),
         "expiration": iso(now + timedelta(days=20)), "source": "AIS RD",
         "body": "RWY 08/26 CLSD DAILY 0600-1000 DUE TO MAINTENANCE WORK IN PROGRESS",
         "raw": "!MDPC A1450/26 MDPC RWY 08/26 CLSD"},
        {"notam_id": "A1452/26", "type": "N", "location": "MDPC", "effective": iso(now - timedelta(days=1)),
         "expiration": iso(now + timedelta(days=60)), "source": "AIS RD",
         "body": "ILS RWY 08 GP U/S", "raw": "!MDPC A1452/26 ILS RWY 08 GP U/S"},
        {"notam_id": "A1455/26", "type": "N", "location": "MDPC", "effective": iso(now - timedelta(hours=5)),
         "expiration": iso(now + timedelta(days=5)), "source": "AIS RD",
         "body": "TWY C WIP, REDUCED WINGSPAN MAX 36M", "raw": "!MDPC A1455/26 TWY C WIP"},
        {"notam_id": "A1460/26", "type": "R", "location": "MDPC", "effective": iso(now + timedelta(days=1)),
         "expiration": iso(now + timedelta(days=3)), "source": "AIS RD",
         "body": "PAPI RWY 26 U/S", "raw": "!MDPC A1460/26 PAPI RWY 26 U/S"},
        {"notam_id": "A1462/26", "type": "N", "location": "MDPC", "effective": iso(now - timedelta(days=10)),
         "expiration": None, "source": "AIS RD",
         "body": "OBST CRANE ERECTED 350M SW THR RWY 08, 145FT AMSL, MARKED AND LGT", "raw": "!MDPC A1462/26 OBST CRANE"},
        {"notam_id": "A1465/26", "type": "N", "location": "MDPC", "effective": iso(now - timedelta(hours=12)),
         "expiration": iso(now + timedelta(days=2)), "source": "AIS RD",
         "body": "BIRD ACTIVITY VICINITY AD, EXC CAUTION ON APCH AND DEP", "raw": "!MDPC A1465/26 BIRD ACTIVITY"},
    ]


def fetch_notams(icao=ICAO_DEFAULT, key=None, timeout=25):
    """Devuelve (lista_normalizada, error|None). Lista vacía si no hay clave o falla el API."""
    if os.environ.get("AEROINTEL_NOTAM_DEMO", "").lower() in ("1", "true", "yes"):
        out = [normalize(n) for n in _demo_raw()]
        out = [n for n in out if n["status"] != "expirado"]
        rank, sstat = {"alta": 0, "media": 1}, {"vigente": 0, "programado": 1}
        out.sort(key=lambda n: (rank.get(n["importance"], 2), sstat.get(n["status"], 2), n["effective"] or ""))
        return out, None
    key = key or os.environ.get("RAPIDAPI_KEY") or os.environ.get("SKYLINK_API_KEY")
    if not key:
        return [], "sin clave (RAPIDAPI_KEY)"
    url = f"https://{RAPID_HOST}/notams/{icao}"
    req = urllib.request.Request(url, headers={
        "x-rapidapi-key": key, "x-rapidapi-host": RAPID_HOST, "User-Agent": "AeroIntel/1.0"})
    try:
        ctx = ssl._create_unverified_context()
        raw = json.loads(urllib.request.urlopen(req, timeout=timeout, context=ctx).read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", "")
        except Exception:
            msg = ""
        return [], f"HTTP {e.code} {msg}".strip()
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"
    items = raw.get("notams") or []
    out = [normalize(n) for n in items]
    out = [n for n in out if n["status"] != "expirado"]            # solo activos/programados
    rank = {"alta": 0, "media": 1}
    sstat = {"vigente": 0, "programado": 1}
    out.sort(key=lambda n: (rank.get(n["importance"], 2), sstat.get(n["status"], 2), n["effective"] or ""))
    return out, None
