#!/usr/bin/env python3
# aerointel.py — AeroIntel · MVP Fase 1 (LOCAL, sin commits).
# Pipeline: ingesta RSS -> dedup/cluster -> relevancia/clasificación -> análisis editorial
# (heurístico, o LLM si hay ANTHROPIC_API_KEY) -> formato Mattermost (o dry-run) + briefing.
# Solo librería estándar (nada que instalar).  Uso:  python3 aerointel.py
#   ANTHROPIC_API_KEY=...        -> usa LLM real para el análisis editorial (modelo barato)
#   MATTERMOST_WEBHOOK_URL=...   -> publica de verdad en Mattermost (si no, dry-run)
import os, re, json, ssl, html, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from difflib import SequenceMatcher
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import store, apiexport, notams

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
UA = "AeroIntel/0.1 (+local MVP)"
# UA de navegador para los FEEDS: algunos medios (p. ej. FlightGlobal) devuelven 403 a bots
# declarados. Para las APIs de LLM se mantiene el UA propio (transparente y sin problema).
FEED_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_PER_SOURCE = 25
WHEN = os.environ.get("AEROINTEL_WHEN", "7d")                    # ventana de Google News (recencia)
MAX_AGE_H = float(os.environ.get("AEROINTEL_MAX_AGE_H", "168"))  # descarta noticias más viejas (168h = 7 días)

def load_json(name, default):
    p = os.path.join(HERE, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default

SOURCES = load_json("sources.json", {"sources": []})["sources"]
AIRLINES = load_json("airlines_puj.json", {"airlines": []})["airlines"]

# ───────────────────────── ingesta ─────────────────────────
def gnews_url(s):
    q = urllib.parse.quote(f"{s['query']} when:{WHEN}")   # restringe a noticias recientes
    lang, gl = s.get("lang", "en-US"), s.get("gl", "US")
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={gl}&ceid={gl}:{lang.split('-')[0]}"

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": FEED_UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    try:
        return urllib.request.urlopen(req, timeout=timeout).read()
    except (ssl.SSLError, urllib.error.URLError):
        # respaldo solo-lectura para feeds públicos (certs de macOS); es un demo local
        ctx = ssl._create_unverified_context()
        return urllib.request.urlopen(req, timeout=timeout, context=ctx).read()

def localname(tag):
    return tag.split('}')[-1].lower()

def clean(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()

# Limpieza de titulares crudos de Google News: quita el sufijo " - Publicación" y blobs de
# tracking "(AbC123…)". Se aplica cuando el LLM no reescribió (fallback heurístico/429).
_SRC_SUFFIX_RE = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,45}$")
_TRACK_RE = re.compile(r"\s*\((?:[A-Za-z0-9_]{6,}|[A-Za-z0-9]{1,3}\d[A-Za-z0-9]{4,})\)\s*")

def clean_title(t):
    t = (t or "").strip()
    t = _TRACK_RE.sub(" ", t)
    t = _SRC_SUFFIX_RE.sub("", t).strip()
    t = re.sub(r"\s+", " ", t)
    return (t[:117] + "…") if len(t) > 120 else t

def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt); break
            except Exception:
                continue
    if dt and dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

def age_hours(dt):
    return None if dt is None else (datetime.utcnow() - dt).total_seconds() / 3600

def human_age(dt):
    a = age_hours(dt)
    if a is None:
        return "fecha s/d"
    if a < 1:
        return f"hace {max(1, int(a * 60))} min"
    if a < 24:
        return f"hace {int(a)} h"
    return dt.strftime("%d %b %H:%M")

def parse_feed(xml_bytes, source_name):
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return out
    for el in root.iter():
        if localname(el.tag) not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "desc": "", "pub": ""}
        for ch in el:
            lt = localname(ch.tag)
            if lt == "title":
                d["title"] = clean(ch.text)
            elif lt == "link" and not d["link"]:
                d["link"] = (ch.get("href") or ch.text or "").strip()
            elif lt in ("description", "summary", "content") and not d["desc"]:
                d["desc"] = clean(ch.text)
            elif lt in ("pubdate", "published", "updated", "date") and not d["pub"]:
                d["pub"] = (ch.text or "").strip()
        if d["title"] and d["link"]:
            d["source"] = source_name
            d["dt"] = parse_date(d["pub"])
            out.append(d)
    return out[:MAX_PER_SOURCE]

# Regex para extraer thumbnail de previsualización de Google News (lh3.googleusercontent.com).
# Estas imágenes son del artículo original, cacheadas/escaladas por Google.
_GNEWS_IMG_RE = re.compile(
    r'(https://lh3\.googleusercontent\.com/[A-Za-z0-9_-]+=[^\s"\'<>]+)',
    re.I
)
# Captura og:image y twitter:image en cualquier orden de atributos (para URLs directas).
_OG_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:image["\']|name=["\']twitter:image["\'])[^>]+content=["\']([^"\' <>]+)["\']'
    r'|<meta[^>]+content=["\']([^"\' <>]+)["\'][^>]+(?:property=["\']og:image["\']|name=["\']twitter:image["\'])',
    re.I | re.S
)
# Imágenes a rechazar siempre: logos/branding de plataformas y thumbnails diminutos (no son
# la foto del artículo). El logo-tarjeta de Google News cae aquí o por el filtro de frecuencia.
GENERIC_IMG_RE = re.compile(
    r'(gstatic\.com|/logo|_logo|sprite|favicon|placeholder|default[-_]?(?:image|thumb)|'
    r'=w(?:16|24|32|48|64)\b|googlelogo|news[-_]?google|if\.not\.exist|not[-_.]?exist|'
    r'no[-_]?image|sin[-_]?imagen)', re.I)

def _fetch_html(url, timeout=10, max_bytes=65536):
    """Descarga hasta max_bytes de una URL y retorna el texto."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read(max_bytes).decode("utf-8", errors="ignore")

def fetch_og_image(url, timeout=10):
    """Extrae imagen de previsualización para un artículo.
    - Google News: extrae el thumbnail cacheado de lh3.googleusercontent.com (escalado a 1200px).
    - URLs directas: extrae og:image / twitter:image del <head> del artículo.
    Retorna la URL de la imagen (str) o None."""
    if not url or not url.startswith("http"):
        return None
    try:
        is_gnews = "news.google.com/" in url
        # Google News embebe la imagen de artículo casi al final del HTML (~600 KB)
        body = _fetch_html(url, timeout=timeout, max_bytes=720000 if is_gnews else 65536)
        if is_gnews:
            # Extraer thumbnail de previsualización de Google News
            imgs = _GNEWS_IMG_RE.findall(body)
            # Filtrar favicons (w16, w24, w32, w48) y quedarnos con la imagen de artículo
            article_imgs = [i for i in imgs if re.search(r'=s\d+-w\d+-rw', i)]
            if article_imgs:
                # Escalar a 1200px de ancho
                return re.sub(r'=s\d+-w\d+-rw', '=s0-w1200-rw', article_imgs[0])
            # Fallback: cualquier imagen grande de googleusercontent
            large = [i for i in imgs if '=w16' not in i and '=w24' not in i and '=w32' not in i and '=w48' not in i]
            if large:
                return large[0]
        # Para URLs directas (o fallback de GNews): buscar og:image
        m = _OG_RE.search(body)
        if m:
            img = html.unescape((m.group(1) or m.group(2) or "").strip())
            if img.startswith("http"):
                return img
    except Exception:
        pass
    return None

def fetch_images_parallel(events, n=20, max_workers=8):
    """Enriquece los top-N eventos con 'image_url' en paralelo.
    Los que no tienen imagen quedan con image_url=None (el dashboard usa fallback de color)."""
    top = events[:n]
    if not top:
        return
    print(f"  Extrayendo imágenes para {len(top)} eventos ({max_workers} workers paralelos)…", end="", flush=True)
    urls = [ev["items"][0]["link"] for ev in top]
    results = {i: None for i in range(len(top))}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch_og_image, url): i for i, url in enumerate(urls)}
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                results[idx] = fut.result()
            except Exception:
                pass
    # Filtro de calidad de imagen: una foto de artículo REAL es única. Un placeholder/logo
    # (p. ej. la tarjeta genérica de Google News) se repite entre muchos artículos → se rechaza.
    counts = Counter(u for u in results.values() if u)
    kept = 0
    for i, ev in enumerate(top):
        u = results.get(i)
        if u and (counts[u] > 1 or GENERIC_IMG_RE.search(u)):
            u = None                                  # placeholder/logo compartido → ficha de categoría
        if u:
            kept += 1
        ev["image_url"] = u
    print(f" {kept}/{len(top)} con imagen real y única (resto → ficha de inteligencia).")

# Boost visual: una nota con foto real sube un poco en el ranking (portada más atractiva) sin
# alterar el fondo del modelo. Se aplica DESPUÉS del umbral de publicación: solo reordena.
IMG_BOOST = int(os.environ.get("AEROINTEL_IMG_BOOST", "4"))

def apply_image_boost(events, boost=None):
    boost = IMG_BOOST if boost is None else boost
    if boost <= 0:
        return
    for ev in events:
        if ev.get("image_url"):
            a = ev["analysis"]
            a["impact_score"] = min(100, a["impact_score"] + boost)
            if isinstance(a.get("score_breakdown"), dict):
                a["score_breakdown"]["img"] = boost
    events.sort(key=lambda e: e["analysis"]["impact_score"], reverse=True)

# ───────────────────────── dedup / cluster ─────────────────────────
def canonical(url):
    return re.sub(r"[?#].*$", "", url or "").rstrip("/").lower()

def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower())

def cluster(items):
    events = []
    for it in items:
        nt, cu = norm_title(it["title"]), canonical(it["link"])
        for ev in events:
            if cu == ev["cu"] or SequenceMatcher(None, nt, ev["nt"]).ratio() >= 0.62:
                ev["items"].append(it)
                break
        else:
            events.append({"nt": nt, "cu": cu, "items": [it]})
    return events

# ───────────────────────── relevancia / clasificación (heurística) ─────────────────────────
KW = {
    "rutas":       ["new route", "new service", "launches", "primer vuelo", "vuelo entre", "nueva ruta",
                    "frequency", "nonstop", "nueva frecuencia", "inaugura", "conecta", "begins flights",
                    "resumes service", "direct flight", "vuelo directo", "nuevo destino"],
    "meteo":       ["hurricane", "tropical storm", "tropical depression", "fog", "turbulence", "blizzard",
                    "tormenta tropical", "huracán", "ciclón", "depresión tropical", "onamet", "nhc",
                    "vaguada", "inundaci", "alerta meteorol", "alerta verde", "alerta amarilla",
                    "alerta roja", "marejada", "oleaje", "frente frío", "frente frio", "aguacero",
                    "granizo", "tornado", "flooding", "thunderstorm", "lluvia", "rain"],
    "seguridad":   ["crash", "accident", "incident", "emergency", "mayday", "evacuat", "fire", "smoke", "aog",
                    "close call", "near miss", "near-miss", "go-around", "go around", "loss of separation",
                    "tcas", "bird strike", "hard landing", "tail strike", "runway excursion", "rejected takeoff",
                    "accidente", "incidente", "emergencia", "cuasi accidente"],
    "operaciones": ["delay", "cancel", "strike", "ground stop", "divert", "notam", "retraso", "cancela", "huelga", "desvi", "paro"],
    "regulatorio": ["faa", "easa", "ntsb", "icao", "iata", "idac", "regulat", "directive", "certif", "ban", "suspend", "regul"],
    "industria":   ["order", "delivery", "airbus", "boeing", "embraer", "merger", "earnings", "financ", "pedido", "entrega", "fusión"],
    "tecnologia":  ["evtol", "saf", "sustainable aviation fuel", "sustainab", "electric aircraft", "hydrogen",
                    "drone", "uav", "tecnolog", "sostenib", "biocombustible"],
}
AVIATION = ["aviation", "airline", "airport", "flight", "aircraft", "aerol", "aeropuerto", "vuelo", "avión", "aviaci", "airbus", "boeing", "jet", "carrier"]
CRIT = ["crash", "accident", "emergency", "mayday", "evacuat", "hurricane", "ground stop", "huracán", "accidente", "emergencia"]
IMPORT = ["incident", "delay", "cancel", "strike", "storm", "tropical", "aog", "suspend", "incidente", "retraso", "cancela", "tormenta", "huelga"]
PUJ = ["punta cana", "puj", "dominican", "república dominicana", "republica dominicana", "caribbean", "caribe"]

def _kw_pat(k):
    """Patrón con LÍMITE DE PALABRA. Acrónimos/cortos (≤4) → palabra exacta (evita 'saf' en
    'USAF'/'safety', 'ban' en 'Cuban'). Términos largos → prefijo (permite plural/flexión)."""
    esc = re.escape(k)
    return r"\b" + esc + (r"\b" if len(k) <= 4 else "")

def word_re(words):
    return re.compile("|".join(_kw_pat(w) for w in words), re.I)

# Clasificación por categoría y severidad con límite de palabra (no subcadena).
KW_RE = {cat: word_re(kws) for cat, kws in KW.items()}
CRIT_RE = word_re(CRIT)
IMPORT_RE = word_re(IMPORT)

WHY = {
    "meteo":       "Condiciones meteorológicas con posible impacto en operaciones de PUJ y el Caribe; monitorear itinerarios y posibles ajustes (OVER/cancelaciones).",
    "seguridad":   "Evento de seguridad operacional; revisar si involucra aeronaves u operadores con presencia en PUJ.",
    "operaciones": "Posible afectación operativa (retrasos/cancelaciones/huelga); monitorear efecto en itinerarios de PUJ.",
    "regulatorio": "Cambio regulatorio que podría afectar requisitos operativos; evaluar aplicabilidad a PUJ/RD.",
    "rutas":       "Movimiento de red/conectividad relevante para la planificación de PUJ.",
    "industria":   "Relevante para el panorama de la industria; impacto operacional directo bajo.",
    "tecnologia":  "Tendencia tecnológica del sector; impacto operacional directo bajo por ahora.",
}

# ── Aerolíneas (matching robusto): SOLO nombres reales (≥4 chars, sin códigos de 2-3 letras
#    en mayúsculas) para evitar falsos positivos en texto libre (DO, NO, OR, AA, DE…). Word boundary.
_KEEP_UPPER = {"LATAM", "JETSMART", "TUI", "GOL"}
AIRLINE_NAMES = sorted(
    {a for a in AIRLINES if (len(a) >= 4 and not a.isupper()) or a in _KEEP_UPPER},
    key=len, reverse=True)
AIRLINE_RE = re.compile(r"\b(" + "|".join(re.escape(a) for a in AIRLINE_NAMES) + r")\b", re.I) if AIRLINE_NAMES else None

# Nombres de aerolínea que son palabras comunes → solo cuentan si van seguidos de un calificador
# de aviación. Evita "United States"→United, "Spirit of"→Spirit, "Mississippi Delta"→Delta, etc.
_AMBIG_AIRLINES = {"united", "delta", "spirit", "frontier", "american", "breeze", "avelo", "allegiant",
                   "discover", "sun country", "copa", "wingo", "flair", "condor", "neos", "wamos",
                   "plus ultra", "sky high", "freedom ii", "world atlantic", "air century", "eastern express"}
_AIR_QUAL = re.compile(r"^[\s,’'\-–]*(air\b|airline|airways|flight|jet|aircraft|plane|express|"
                       r"a3\d\d|a2\d\d|b7\d\d|7[2-8]7|cancel|delay|divert|route|fleet|nonstop|crew)", re.I)

def detect_airlines(text):
    if not AIRLINE_RE:
        return []
    text = text or ""
    seen, out = set(), []
    for m in AIRLINE_RE.finditer(text):
        k = m.group(0).lower()
        # Nombre ambiguo: exige un calificador de aviación inmediatamente después.
        if k in _AMBIG_AIRLINES and not _AIR_QUAL.match(text[m.end():m.end() + 28]):
            continue
        if k not in seen:
            seen.add(k); out.append(m.group(0))
    return out[:6]

# ── Relevancia y scoring: República Dominicana primero, en dos niveles. ──
# Núcleo RD: máxima prioridad operacional (aeropuertos, autoridades y aerolínea bandera del país).
DR_CORE = ["punta cana", "puj", "santo domingo", "sdq", " sti ", "santiago de los caballeros",
           "puerto plata", " pop ", "la romana", " lrm ", "samaná", "el catey", " azs ",
           "república dominicana", "republica dominicana", "dominican republic", "aerodom",
           "idac", "junta de aviación civil", "jac", "arajet", "sky high", "onamet"]
# Anillo regional/Caribe: relevante por proximidad y rutas, pero menor peso que el núcleo RD.
# "nws san juan" = firma de las alertas Atom del NWS de Puerto Rico (vecino inmediato de RD).
DR_REGIONAL = ["dominican", "caribbean", "caribe", "puerto rico", "san juan sju", "nws san juan",
               "cuba", "haiti", "haití", "jamaica", "bahamas", "antilles", "antillas", "hispaniola"]
DR_TERMS = DR_CORE + DR_REGIONAL  # compat: is_dr/is_relevant siguen viendo todo el universo RD/Caribe
WX_REGION = ["caribbean", "caribe", "atlantic", "atlántico", "dominican", "punta cana", "cuba",
             "jamaica", "bahamas", "hispaniola", "haiti", "haití", "puerto rico", "antilles", "antillas"]
WX_TROPICAL = ["tropical storm", "tropical depression", "hurricane", "huracán", "ciclón", "tormenta tropical"]
# Clima que afecta al PAÍS = afecta la operación de PUJ (aunque la nota no diga "avión"). Amplio
# pero acotado: solo cuenta junto a una ubicación RD/Caribe (ver is_relevant).
# OJO: nada de términos ambiguos como "temporal" (es. temporary) — generan falsos positivos.
WX_TERMS = WX_TROPICAL + [
    "lluvia", "aguacero", "vaguada", "inundaci", "onamet", "alerta meteorol", "alerta verde",
    "alerta amarilla", "alerta roja", "marejada", "oleaje", "frente frío", "frente frio",
    "ola de calor", "tormenta eléctrica", "tormenta electrica", "granizo", "tornado",
    "neblina", "niebla", "mal tiempo", "viento fuerte", "ráfaga", "rafaga", "rain", "rainfall",
    "storm", "thunderstorm", "flooding", "flood", "fog", "gale", "squall", "downpour"]
# Filtro de clima con LÍMITE DE PALABRA (no subcadena) → evita fugas tipo 'temporal' en 'temporalmente'.
WX_RE = word_re(WX_TERMS)
# Ciclones de la cuenca atlántica (NHC): un sistema tropical en el Atlántico es señal operativa
# para RD aunque el aviso aún no nombre al Caribe (la trayectoria puede alcanzarlo).
WX_TROPICAL_RE = word_re(WX_TROPICAL)
ATLANTIC_RE = re.compile(r"\batl[aá]ntic", re.I)
# Historia de RECUPERACIÓN tras un evento (rebote, reapertura, normalización): no es una alerta
# activa aunque nombre al huracán. Solo aplica si el texto NO trae señales de amenaza vigente.
WX_RECOVERY_RE = re.compile(
    r"\b(rebound|recover[sy]?|recovery|reabr\w*|recuper\w*|reanud\w*|aftermath|"
    r"after (?:hurricane|the storm|tropical storm)|tras (?:el|la) (?:hurac[aá]n|tormenta)|"
    r"se normaliz\w*|restablec\w*|back to normal)\b", re.I)
WX_ACTIVE_RE = re.compile(
    r"\b(warning|watch|alert[ae]?|approach\w*|se acerca|amenaza\w*|threatens?|forecast to|"
    r"expected to (?:strengthen|hit|impact)|en desarrollo|intensific\w*)\b", re.I)
# Ruido turístico/marketing sin valor operacional → se castiga fuerte y se filtra.
NOISE_RE = re.compile(
    r"\b(best time to (?:book|visit)|how to (?:book|find|score|visit|travel|get to)|"
    r"when to (?:book|visit|go)|cheapest|cheap flights to|deals?|things to do|guide to|top \d+|"
    r"\d+ (?:best|reasons|things|ways|places)|bucket list|all-inclusive|vacation package|review:|"
    r"travel tips|what to (?:do|pack|know before)|honeymoon|where to stay|nightlife|best beaches|"
    r"best [\w'’ -]{0,30}destinations?|destinations? to visit|to visit year[- ]?round|where to go|"
    r"during hurricane season|cruise|resort|getaway|staycation|romantic|family[- ]friendly)\b",
    re.I)

# Matching RD con LÍMITE DE PALABRA (no subcadena): evita falsos positivos de acrónimos cortos
# ('jac' dentro de 'hijack', 'puj' dentro de 'puja/Pujols'). Reusa _kw_pat: ≤4 chars = palabra exacta.
# Códigos IATA que además son palabras comunes (POP-up, STI…) solo cuentan EN MAYÚSCULAS.
_DR_AMBIG_CODES = {"pop", "sti", "azs", "lrm"}
_DR_CODES_CS_RE = re.compile(r"\b(" + "|".join(c.upper() for c in sorted(_DR_AMBIG_CODES)) + r")\b")
DR_CORE_RE = word_re([k.strip() for k in DR_CORE if k.strip() not in _DR_AMBIG_CODES])
DR_REGIONAL_RE = word_re([k.strip() for k in DR_REGIONAL])

def is_dr(text):
    t = text or ""
    return bool(DR_CORE_RE.search(t) or _DR_CODES_CS_RE.search(t) or DR_REGIONAL_RE.search(t))

# Señal aeronáutica: una nota DEBE tener contexto de aviación para entrar (no basta nombrar a RD).
# Evita ruido de feeds generales (economía/agro de Diario Libre). Se usa LÍMITE DE PALABRA: tokens
# cortos como "jet"/"faa"/"iata" NO deben matchear como subcadena (p. ej. "jet" dentro de "objetivo").
AVIATION_RE = re.compile(
    r"\baviation|\bairline|\bairport|\bflight|\baircraft|\baerol|\baeropuerto|\bvuelo\b|\bavi[oó]n|"
    r"\baviaci|\bairbus|\bboeing|\bjet\b|\bjetliner|\bcarrier\b|\baerodom|\bidac\b|\bnotam|\brunway|"
    r"\bpista\b|\bdespegu|\baterriz|\bslot\b|\bhandling|\brampa\b|terminal a[eé]re|aviaci[oó]n civil|"
    r"civil aviation|\bicao\b|\biata\b|\bfaa\b|\beasa\b|\btripulaci|\bcockpit|\bfuselaje|\bairspace|"
    r"espacio a[eé]reo", re.I)
DR_AIRPORT_CODES = [" puj", "puj ", " sdq", "sdq ", " sti ", " pop ", " azs ", " lrm "]

def is_relevant(text):
    t = text or ""
    tl = t.lower()
    if AVIATION_RE.search(t):
        return True
    if detect_airlines(t):
        return True
    if any(c in tl for c in DR_AIRPORT_CODES):       # códigos IATA de aeropuertos RD = contexto aviación
        return True
    # Clima que afecta a RD/Caribe entra como meteo (afecta operaciones de PUJ aunque no diga "avión").
    if WX_RE.search(t) and dr_tier(t) is not None:
        return True
    # Ciclón/tormenta tropical en la cuenca del Atlántico (NHC) — vigilancia aunque no nombre al Caribe.
    if WX_TROPICAL_RE.search(t) and ATLANTIC_RE.search(t):
        return True
    return False

# ── Extracción de contexto del título para ángulo editorial dinámico ──
_AIRPORTS_RE = re.compile(r'\b([A-Z]{3})\b')  # IATA codes
_FLIGHT_RE   = re.compile(r'\b([A-Z]{2}\d{1,4}|[A-Z]{3}\d{1,4}|Flight\s+\d+)\b', re.I)
_ACFT_RE     = re.compile(r'\b(A\d{3}(?:neo|XLR)?|B?7[2-8]7(?:-\d+)?|737\s*MAX|A220|E\d{3}|CRJ\d{3}|ATR\s*\d{2}|ERJ\s*\d{3})\b', re.I)
_PLACE_RE    = re.compile(r'\b(Punta Cana|PUJ|Santo Domingo|SDQ|Santiago|STI|La Romana|Miami|New York|JFK|Newark|Boston|'
                          r'Atlanta|Fort Lauderdale|Cancún|Cancun|San Juan|Bogotá|Bogota|Medellín|Lima|'
                          r'Panama|Toronto|Montreal|London|Madrid|Paris|Amsterdam|Chicago|Dallas|Houston|'
                          r'Los Angeles|LAX|Orlando|Caribbean|Caribe|Dominican Republic|República Dominicana)\b', re.I)
# Nombre propio del sistema tropical ("Hurricane Melissa") — se excluyen palabras institucionales
# ("Hurricane Center/Season/Warning") que no son nombres de ciclón.
_WX_EVENT_RE = re.compile(
    r'\b(Hurricane\s+(?!Center|Centre|Season|Watch|Warning|Hunters|Preparedness)\w+|'
    r'Tropical Storm\s+(?!Warning|Watch|Season)\w+|'
    r'huracán\s+\w+|tormenta tropical\s+(?!en|del|de)\w+|ciclón\s+(?!tropical)\w+)\b', re.I)

# Un lugar precedido por la OFICINA emisora (NWS Miami, National Hurricane Center Miami) no es
# la zona afectada de la noticia — es la firma del boletín. Se excluye del contexto.
_ISSUER_RE = re.compile(r"(nws|hurricane center|weather service|centro nacional)\s*$", re.I)

def _extract_context(text):
    """Extrae entidades relevantes del texto para construir un ángulo editorial contextual."""
    places = []
    for m in _PLACE_RE.finditer(text):
        if _ISSUER_RE.search(text[max(0, m.start() - 28):m.start()]):
            continue
        places.append(m.group())
    return {
        "airports":  list(dict.fromkeys(_AIRPORTS_RE.findall(text)))[:4],
        "flights":   list(dict.fromkeys(_FLIGHT_RE.findall(text)))[:2],
        "aircraft":  list(dict.fromkeys(_ACFT_RE.findall(text)))[:2],
        "places":    list(dict.fromkeys(places))[:4],
        "wx_events": list(dict.fromkeys(m.group() for m in _WX_EVENT_RE.finditer(text)))[:2],
    }

def _build_editorial(cat, sev, text, airlines, puj, ctx):
    """Genera un ángulo editorial dinámico basado en la categoría, severidad y contexto extraído."""
    tl = text.lower()
    places_str = ", ".join(ctx["places"][:3]) if ctx["places"] else ""
    air_str = ", ".join(airlines[:3]) if airlines else ""
    acft_str = ", ".join(ctx["aircraft"][:2]) if ctx["aircraft"] else ""
    flight_str = ctx["flights"][0] if ctx["flights"] else ""
    wx_str = ctx["wx_events"][0] if ctx["wx_events"] else ""

    if cat == "seguridad":
        # Detectar sub-tipo de evento
        if any(k in tl for k in ["crash", "accidente", "crashes"]):
            event = "accidente aéreo"
        elif any(k in tl for k in ["emergency landing", "aterrizaje de emergencia", "divert", "diverted"]):
            event = "aterrizaje de emergencia/desvío"
        elif any(k in tl for k in ["medical", "médic"]):
            event = "emergencia médica en cabina"
        elif any(k in tl for k in ["fire", "smoke", "incendio", "humo"]):
            event = "incidente por fuego/humo"
        elif any(k in tl for k in ["lightning", "rayo"]):
            event = "impacto de rayo en aeronave"
        elif any(k in tl for k in ["evacuat"]):
            event = "evacuación"
        elif any(k in tl for k in ["engine", "motor"]):
            event = "fallo de motor"
        elif any(k in tl for k in ["runway", "pista", "gear", "tren de aterrizaje"]):
            event = "incidente en pista"
        else:
            event = "incidente de seguridad"

        parts = [f"Reportan {event}"]
        if flight_str:
            parts[0] += f" ({flight_str})"
        if air_str:
            parts.append(f"involucra a {air_str}")
        if acft_str:
            parts.append(f"aeronave tipo {acft_str}")
        if places_str:
            parts.append(f"ruta asociada: {places_str}")
        # Honestidad del vínculo con PUJ: solo se afirma impacto directo si la nota MENCIONA el
        # hub; una aerolínea que también opera en PUJ es vínculo indirecto y se dice como tal.
        if PUJ_DIRECT_RE.search(text):
            parts.append("involucra directamente a PUJ — monitorear afectación a itinerarios")
        elif airlines:
            parts.append("aerolínea(s) con operación también en PUJ; monitorear posibles efectos en itinerarios")
        elif puj:
            parts.append("ocurre en el entorno RD — relevante para la operación nacional")
        else:
            parts.append("sin impacto directo en PUJ, pero relevante para el sector")
        return "; ".join(parts) + "."

    elif cat == "meteo":
        parts = []
        recovery = WX_RECOVERY_RE.search(tl) and not WX_ACTIVE_RE.search(tl)
        if recovery:
            parts.append(f"Recuperación/normalización tras {wx_str}" if wx_str
                         else "Recuperación tras el evento meteorológico")
            if places_str:
                parts.append(f"zona en recuperación: {places_str}")
            parts.append("sin alerta activa señalada en el texto; valor informativo para la planificación regional")
            return "; ".join(parts) + "."
        if wx_str:
            parts.append(f"Alerta por {wx_str}")
        else:
            parts.append("Evento meteorológico en desarrollo")
        if places_str:
            parts.append(f"zona afectada: {places_str}")
        if puj:
            parts.append("potencial impacto en operaciones de PUJ/Caribe — revisar NOTAM y planes de contingencia (OVER, re-ruteo, cancelaciones preventivas)")
        if air_str:
            parts.append(f"aerolíneas expuestas: {air_str}")
        return "; ".join(parts) + "."

    elif cat == "operaciones":
        # OJO: "strike" solo es huelga si NO es 'bird/lightning/tail strike' (esos son seguridad);
        # y el desvío/ground stop se evalúa ANTES para no confundir el evento principal.
        labor_strike = bool(re.search(r"\bhuelga\b|\bparo\b|(?<!bird )(?<!lightning )(?<!tail )(?<!wildlife )strike\b", tl))
        if any(k in tl for k in ["cancel", "cancela"]):
            event = "cancelaciones"
        elif any(k in tl for k in ["delay", "retraso"]):
            event = "retrasos operativos"
        elif any(k in tl for k in ["ground stop"]):
            event = "ground stop"
        elif any(k in tl for k in ["divert", "desvi"]):
            event = "desvíos de vuelos"
        elif labor_strike:
            event = "huelga/paro laboral"
        else:
            event = "disrupción operativa"

        parts = [f"Reportan {event}"]
        if places_str:
            parts.append(f"en {places_str}")
        if air_str:
            parts.append(f"afecta a {air_str}")
        n_delays = re.search(r'(\d+)\s+(?:flight\s+)?delay', tl)
        n_cancels = re.search(r'(\d+)\s+(?:flight\s+)?cancel', tl)
        nums = []
        if n_delays:
            nums.append(f"{n_delays.group(1)} retrasos")
        if n_cancels:
            nums.append(f"{n_cancels.group(1)} cancelaciones")
        if nums:
            parts.append(f"cifras reportadas: {', '.join(nums)}")
        if puj:
            parts.append("posible efecto cascada en conexiones hacia PUJ")
        return "; ".join(parts) + "."

    elif cat == "rutas":
        if any(k in tl for k in ["new route", "nueva ruta", "launches", "inaugura", "begins flights"]):
            event = "Nueva ruta anunciada"
        elif any(k in tl for k in ["nonstop", "directo", "direct"]):
            event = "Nuevo servicio directo"
        elif any(k in tl for k in ["resumes", "reanuda"]):
            event = "Reanudación de servicio"
        elif any(k in tl for k in ["frequency", "frecuencia"]):
            event = "Cambio de frecuencia"
        else:
            event = "Movimiento de red"
        parts = [event]
        if air_str:
            parts.append(f"operado por {air_str}")
        if places_str:
            parts.append(f"conecta {places_str}")
        if acft_str:
            parts.append(f"con {acft_str}")
        if puj:
            parts.append("impacta conectividad/competencia en PUJ")
        else:
            parts.append("relevante para el mapa de rutas regional")
        return "; ".join(parts) + "."

    elif cat == "regulatorio":
        if any(k in tl for k in ["faa"]):
            body_name = "FAA"
        elif any(k in tl for k in ["easa"]):
            body_name = "EASA"
        elif any(k in tl for k in ["ntsb"]):
            body_name = "NTSB"
        elif any(k in tl for k in ["idac"]):
            body_name = "IDAC"
        elif any(k in tl for k in ["icao"]):
            body_name = "ICAO"
        else:
            body_name = ""
        parts = []
        if body_name:
            parts.append(f"Acción regulatoria de {body_name}")
        else:
            parts.append("Desarrollo regulatorio")
        if any(k in tl for k in ["certif"]):
            parts.append("relacionada con certificación")
        elif any(k in tl for k in ["directive", "directiva"]):
            parts.append("directiva de aeronavegabilidad")
        elif any(k in tl for k in ["ban", "suspend", "prohib"]):
            parts.append("restricción o suspensión")
        if air_str:
            parts.append(f"afecta a {air_str}")
        if puj:
            parts.append("evaluar aplicabilidad a operaciones en PUJ/RD")
        else:
            parts.append("sin efecto directo en PUJ pero sienta precedente para el sector")
        return "; ".join(parts) + "."

    elif cat == "industria":
        # "Entrega" solo es entrega de AERONAVE si hay contexto de aeronave (evita confundir
        # 'entrega de reconocimientos/premios' con un delivery de Airbus/Boeing).
        acft_ctx = bool(ctx["aircraft"]) or bool(re.search(r"aeronave|aircraft|avi[oó]n|airbus|boeing|embraer|jet\b", tl))
        if any(k in tl for k in ["visitantes", "turistas", "pasajeros", "passengers", "tourists"]) \
                and re.search(r"\d[\d.,]*\s*(millones|million|mil\b|%)", tl):
            parts = ["Cifra de tráfico/demanda del mercado"]
        elif any(k in tl for k in ["order", "pedido"]) and acft_ctx:
            parts = ["Pedido de aeronaves"]
        elif any(k in tl for k in ["delivery", "entrega", "receives"]) and acft_ctx:
            parts = ["Entrega de aeronave"]
        elif any(k in tl for k in ["merger", "fusión", "acquisition", "adquisición"]):
            parts = ["Movimiento corporativo (fusión/adquisición)"]
        elif any(k in tl for k in ["earnings", "financ", "revenue", "profit"]):
            parts = ["Resultado financiero del sector"]
        else:
            parts = ["Desarrollo de la industria aeronáutica"]
        if air_str:
            parts.append(f"involucra a {air_str}")
        if acft_str:
            parts.append(f"aeronave: {acft_str}")
        if places_str:
            parts.append(f"mercado: {places_str}")
        if puj:
            parts.append("operador(es) con vuelos a PUJ")
        else:
            parts.append("sin impacto operacional directo en PUJ")
        return "; ".join(parts) + "."

    elif cat == "tecnologia":
        parts = ["Avance tecnológico en aviación"]
        if any(k in tl for k in ["saf", "sustainab", "sostenib"]):
            parts = ["Desarrollo en combustibles sostenibles (SAF)"]
        elif any(k in tl for k in ["evtol", "electric", "eléctric"]):
            parts = ["Avance en aviación eléctrica/eVTOL"]
        elif any(k in tl for k in ["drone"]):
            parts = ["Desarrollo en tecnología de drones"]
        if air_str:
            parts.append(f"involucra a {air_str}")
        parts.append("impacto operacional a mediano plazo")
        return "; ".join(parts) + "."

    return WHY.get(cat, "Noticia del sector aeronáutico.")

def dr_tier(text):
    """'core' = aeropuerto/autoridad/aerolínea RD · 'regional' = Caribe/proximidad · None = fuera de zona."""
    t = text or ""
    if DR_CORE_RE.search(t) or _DR_CODES_CS_RE.search(t):
        return "core"
    if DR_REGIONAL_RE.search(t):
        return "regional"
    return None

# Mención DIRECTA del hub (Punta Cana / PUJ / MDPC / Grupo Puntacana). Distingue la vista
# "Hub PUJ" del dashboard (solo el aeropuerto) de "República Dominicana" (todo el país):
# affects_puj sigue siendo el criterio AMPLIO (aerolíneas con operación en PUJ) para alertas/API.
PUJ_DIRECT_RE = re.compile(r"(?i:punta\s?cana)|\bPUJ\b|\bMDPC\b")

# Pesos del modelo de relevancia (transparentes y testeables). El eje dominante es la geografía:
# una noticia de RD parte con un piso alto; una global sin relación operacional se queda abajo.
W_GEO  = {"core": 42, "regional": 18, None: 0}
W_SEV  = {"crítico": 30, "importante": 18, "info": 6}
W_CAT  = {"operaciones": 14, "seguridad": 14, "meteo": 12, "rutas": 12,
          "regulatorio": 8, "industria": 4, "tecnologia": 2}

def score_event(text, n_sources, cat, sev, airlines, tier, ctx):
    """Modelo de ranking ponderado y explicable. Devuelve (score 0-100, desglose)."""
    geo = W_GEO[tier]
    # Una aerolínea que opera en PUJ aporta relevancia aunque la nota no nombre a RD explícitamente.
    if geo == 0 and airlines:
        geo = 12
    sev_w = W_SEV[sev]
    cat_w = W_CAT.get(cat, 4)
    # Riqueza de entidades = concreción operacional (vuelo/aeronave/ruta/aerolínea identificados).
    ent = min(10, 3 * bool(airlines) + 3 * bool(ctx["flights"]) +
                  2 * bool(ctx["aircraft"]) + 2 * bool(ctx["places"]))
    corro = min(8, 3 * (n_sources - 1))               # corroboración entre fuentes
    score = geo + sev_w + cat_w + ent + corro        # la recencia/ruido se aplican en el post-paso unificado
    breakdown = {"geo": geo, "sev": sev_w, "cat": cat_w, "ent": ent, "corro": corro}
    return max(0, min(100, score)), breakdown

def analyze_heuristic(text, n_sources, dt=None):
    tl = text.lower()
    cat = next((c for c, rx in KW_RE.items() if rx.search(text)), "industria")
    sev = "crítico" if CRIT_RE.search(text) else "importante" if IMPORT_RE.search(text) else "info"
    airlines = detect_airlines(text)
    tier = dr_tier(text)
    puj = tier == "core" or bool(airlines)
    ctx = _extract_context(text)
    score, breakdown = score_event(text, n_sources, cat, sev, airlines, tier, ctx)
    why = _build_editorial(cat, sev, text, airlines, puj, ctx)
    return {"relevante": True, "categoria": cat, "severidad": sev, "impact_score": score,
            "affects_puj": puj, "aerolineas": airlines, "angulo_editorial": why,
            "entidades": {"aerolineas": airlines, "aeropuertos": ctx["places"][:3],
                          "aeronaves": ctx["aircraft"], "vuelos": ctx["flights"]},
            "score_breakdown": breakdown, "dr_tier": tier,
            "confianza": round(0.5 + 0.1 * min(3, n_sources), 2)}

RECAP_RE = re.compile(r"\b(recap|roundup|round-up|explained|timeline|what we know|in photos|"
                      r"cronolog|resumen del|a look back|year in review)\b", re.I)

# Contenido RUTINARIO / NO OPERACIONAL: pronóstico diario (temperatura/probabilidad de lluvia)
# y noticias de asistencia social post-evento (bonos, subsidios). Se hunden bajo el umbral;
# las alertas reales (ONAMET, ciclones, vaguadas) usan otro vocabulario y no caen aquí.
ROUTINE_WX_RE = re.compile(
    r"(temperatura y probabilidad de lluvia|probabilidad de lluvia para|pron[oó]stico del tiempo|"
    r"el tiempo (?:para )?hoy|clima (?:de |en )[^:]{2,40}: temperatura|temperaturas? para hoy|"
    r"weather forecast for (?:today|this week)|daily (?:weather )?forecast|"
    r"bonos? de emergencia|bono (?:social|navideño)|subsidios?\b|asistencia social|tarjeta (?:de )?solidaridad)", re.I)

def apply_ranking_adjustments(ev):
    """Correcciones deterministas sobre CUALQUIER score (heurístico o LLM): recencia, ruido,
    recaps y un piso para el núcleo RD. Garantiza prioridad RD y supresión de ruido turístico."""
    a = ev["analysis"]
    txt = ev.get("_txt") or ev["items"][0]["title"]
    # Titular de respaldo limpio si el LLM no reescribió (quita " - Publicación" y tracking).
    if not a.get("titular"):
        a["titular"] = clean_title(ev["items"][0]["title"])
    # dr_tier SIEMPRE determinista desde el texto (el LLM no lo devuelve): alimenta la sección
    # República Dominicana del dashboard, la BD y la API.
    if not a.get("dr_tier"):
        a["dr_tier"] = dr_tier(txt)
    # Mención directa del hub: alimenta la vista "Hub PUJ" y el badge PUJ del dashboard.
    a["puj_direct"] = bool(PUJ_DIRECT_RE.search(txt))
    age = age_hours(ev.get("dt"))
    is_noise = bool(NOISE_RE.search(txt))
    # Recencia con peso FUERTE: la portada debe sentirse viva. Lo muy reciente sube; lo viejo
    # (>2 días) se hunde aunque sea importante, para que un evento de hace 4 días no lidere.
    if age is not None:
        a["impact_score"] += (16 if age < 3 else 11 if age < 12 else 6 if age < 24 else
                              0 if age < 48 else -12 if age < 96 else -22 if age < 168 else -34)
    # Ruido turístico/marketing: tope duro. Aunque mencione "hurricane"/"Caribbean" (lo que le daría
    # severidad alta), un artículo de reservas/turismo nunca debe superar el umbral de publicación.
    if is_noise:
        a["impact_score"] = min(a["impact_score"], 10)
        a["severidad"] = "info"
    # Pronóstico rutinario del día: informativo pero no inteligencia operacional → bajo el umbral.
    if ROUTINE_WX_RE.search(txt):
        a["impact_score"] = min(a["impact_score"], 24)
        a["severidad"] = "info"
        is_noise = True                              # tampoco aplica el piso RD
    # Recap/cronología de evento ya ocurrido: no es breaking
    if RECAP_RE.search(ev["items"][0]["title"]):
        a["impact_score"] -= 20
        if a["severidad"] == "crítico":
            a["severidad"] = "importante"
    # Historia de recuperación (rebote/reapertura tras un evento): nombrar al huracán no la hace
    # crítica. Sin señales de amenaza vigente → severidad informativa y sin ticker de última hora.
    if WX_RECOVERY_RE.search(txt) and not WX_ACTIVE_RE.search(txt) and a["severidad"] == "crítico":
        a["severidad"] = "info"
        a["impact_score"] -= 10
    # Piso para el núcleo RD: una noticia OPERATIVA de RD nunca debe quedar sepultada.
    # No aplica a ruido turístico/marketing aunque mencione "Punta Cana".
    if not is_noise and a["dr_tier"] == "core" and a["severidad"] != "info":
        a["impact_score"] = max(a["impact_score"], 55)
        a["affects_puj"] = True
    a["impact_score"] = max(0, min(100, a["impact_score"]))
    return ev

# ───────────────────────── análisis con LLM (opcional; listo para enchufar) ─────────────────────────
# Prompt editorial compartido por todos los proveedores.
SYSTEM_PROMPT = (
    "Actúas como analista senior de operaciones aeronáuticas que redacta para un boletín de inteligencia "
    "leído por jefes de operaciones de aeropuerto, despachadores y planificadores de red. Hub principal: "
    "Aeropuerto de Punta Cana (PUJ), República Dominicana; también cubres SDQ, STI, POP y "
    "aviación global cuando impacta a RD/Caribe.\n"
    "ESTÁNDAR EDITORIAL (estilo analista, no titular de prensa amarilla):\n"
    "- Analiza SOLO lo que dice el texto. NUNCA inventes cifras, aerolíneas, rutas ni causas. Si un dato no "
    "está, no lo afirmes.\n"
    "- titular: reescríbelo claro, específico y FIEL, máx ~12 palabras, español. Nombra la entidad concreta "
    "(aerolínea/aeropuerto/aeronave) si el texto la da. Prohibido el relleno ('importante noticia', 'esto es "
    "lo que sabemos').\n"
    "- resumen: 2-3 frases de prosa profesional. Qué pasó, dónde, a quién afecta. Lenguaje del sector "
    "(itinerario, rotación, slot, NOTAM, conectividad, OVER), sin tecnicismos gratuitos.\n"
    "- angulo_editorial: la lectura OPERACIONAL. Consecuencia de segundo orden concreta: ¿afecta rotaciones "
    "o conexiones en PUJ? ¿aerolíneas que operan a RD? ¿requiere contingencia (re-ruteo, OVER, cancelación "
    "preventiva)? Si NO toca a RD/PUJ, escribe 'Sin impacto directo en PUJ' y explica el valor para el sector "
    "(precedente regulatorio, tendencia de flota/red).\n"
    "- Evita SIEMPRE frases genéricas y repetitivas. Cada análisis debe sonar escrito por una persona experta.\n"
    "- Recap/cronología/resumen de un evento YA ocurrido (no en desarrollo): severidad=info y baja impact_score.\n"
    "- impact_score (0-100): sube por severidad, cercanía a RD/PUJ/Caribe, aerolíneas que operan en PUJ y que "
    "el hecho esté EN CURSO; baja si es viejo, recap, turismo/marketing o sin relación operacional.\n"
    "Responde ÚNICAMENTE un objeto JSON con: titular, relevante(bool), "
    "categoria(meteo|seguridad|operaciones|regulatorio|rutas|industria|tecnologia), "
    "severidad(info|importante|crítico), aerolineas(array de strings), "
    "entidades(objeto: aeropuertos[], aeronaves[], rutas[]), affects_puj(bool), impact_score(0-100), "
    "resumen(string 2-3 frases), angulo_editorial(string), confianza(0-1).\n"
    "EJEMPLO de calidad (formato y registro, NO copies su contenido):\n"
    '{"titular":"Arajet suma frecuencia diaria Punta Cana–Bogotá con A220",'
    '"categoria":"rutas","severidad":"importante","aerolineas":["Arajet"],'
    '"entidades":{"aeropuertos":["PUJ","BOG"],"aeronaves":["A220"],"rutas":["PUJ-BOG"]},'
    '"affects_puj":true,"impact_score":71,'
    '"resumen":"Arajet incrementa a frecuencia diaria su servicio entre Punta Cana y Bogotá operado con Airbus A220. '
    'El ajuste eleva la oferta de asientos en un corredor de alta demanda de conexiones suramericanas.",'
    '"angulo_editorial":"Mayor presión sobre slots y handling en PUJ en la ventana matinal; refuerza la posición de '
    'Arajet como feeder andino y exige revisar rotaciones de rampa y mostradores.","confianza":0.8}')

# Proveedores compatibles con la API de OpenAI (chat/completions). Free tier real:
#   groq = Llama 3.3 70B (recomendado) · openrouter · cerebras.
OPENAI_PROVIDERS = {
    "groq":       ("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "meta-llama/llama-3.3-70b-instruct:free", "OPENROUTER_API_KEY"),
    "cerebras":   ("https://api.cerebras.ai/v1/chat/completions", "llama3.1-8b", "CEREBRAS_API_KEY"),
}

# ── Resiliencia del LLM: reintentos con backoff. En free tier los 429 (rate limit) son normales;
#    reintentar con espera recupera el análisis en vez de degradar a heurística. ──
LLM_RETRIES = int(os.environ.get("AEROINTEL_LLM_RETRIES", "3"))
_LLM_STATS = {"fallbacks": 0, "retries": 0}    # contadores de la corrida (salud → consola/Mattermost)

def _ra_seconds(retry_after):
    """Parsea Retry-After (segundos) a float; None si no viene o no es numérico."""
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return None

def _retry_delay(attempt, retry_after=None):
    """Espera antes del reintento N (desde 0). Respeta el Retry-After del proveedor (tope 30 s
    para no agotar el presupuesto del cron); si no viene, backoff exponencial 2s → 6s → 18s."""
    ra = _ra_seconds(retry_after)
    if ra is not None:
        return min(30.0, max(1.0, ra))
    return 2.0 * (3 ** attempt)

def _llm_post(url, payload, headers, timeout=60, tries=None):
    """POST JSON al proveedor LLM con reintentos. Reintenta SOLO lo transitorio (429/5xx/red);
    un 4xx real (clave inválida, payload malo) se lanza de inmediato — reintentarlo es inútil.
    Un 429 con Retry-After largo (cuota por minutos/día agotada) también falla directo: esperar
    dentro de la corrida no lo va a resolver."""
    tries = LLM_RETRIES if tries is None else tries
    last = None
    for attempt in range(tries):
        delay = None
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            ra = _ra_seconds(e.headers.get("Retry-After"))
            if e.code == 429 and ra is not None and ra > 120:
                raise                                # cuota larga: no quemar tiempo del cron
            if e.code == 429 or e.code >= 500:
                last, delay = e, _retry_delay(attempt, e.headers.get("Retry-After"))
            else:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last, delay = e, _retry_delay(attempt)
        if attempt < tries - 1:
            _LLM_STATS["retries"] += 1
            time.sleep(delay)
    raise last

def _parse_llm_json(txt):
    r = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    r.setdefault("impact_score", 50); r.setdefault("affects_puj", False); r.setdefault("aerolineas", [])
    r.setdefault("categoria", "industria"); r.setdefault("severidad", "info"); r.setdefault("confianza", 0.7)
    r.setdefault("titular", ""); r.setdefault("resumen", "")
    r.setdefault("entidades", {"aeropuertos": [], "aeronaves": [], "rutas": []})
    if not r.get("angulo_editorial"):
        r["angulo_editorial"] = r.get("resumen", "")
    return r

def analyze_anthropic(text):
    key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("AEROINTEL_MODEL", "claude-haiku-4-5-20251001")
    payload = {"model": model, "max_tokens": 500, "system": SYSTEM_PROMPT,
               "messages": [{"role": "user", "content": text[:4000]}]}
    raw = _llm_post("https://api.anthropic.com/v1/messages", payload,
        {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json", "User-Agent": UA})
    return _parse_llm_json(raw["content"][0]["text"])

def analyze_openai_compatible(text, prov):
    base, default_model, key_env = OPENAI_PROVIDERS[prov]
    key = os.environ.get(key_env) or os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("AEROINTEL_MODEL", default_model)
    payload = {"model": model, "max_tokens": 500, "temperature": 0.2,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text[:4000]}]}
    raw = _llm_post(base, payload,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA})
    return _parse_llm_json(raw["choices"][0]["message"]["content"])

def analyze(text, n_sources):
    prov = os.environ.get("AEROINTEL_LLM", "").lower()
    if not prov and os.environ.get("ANTHROPIC_API_KEY"):
        prov = "anthropic"
    if prov:
        try:
            if prov == "anthropic":
                return analyze_anthropic(text)
            if prov in OPENAI_PROVIDERS:
                return analyze_openai_compatible(text, prov)
            print(f"   (proveedor LLM '{prov}' no reconocido — uso heurística)")
        except Exception as e:
            _LLM_STATS["fallbacks"] += 1
            print(f"   (LLM falló tras {LLM_RETRIES} intentos, uso heurística: {e})")
    return analyze_heuristic(text, n_sources)

# Cortacircuito: si N eventos SEGUIDOS agotan sus reintentos (cuota/rate limit persistente),
# el resto de la corrida va directo a heurística — sin quemar minutos en reintentos condenados.
LLM_BREAKER = int(os.environ.get("AEROINTEL_LLM_BREAKER", "3"))

def apply_llm(events, top, pause):
    """Analiza con LLM los top-N eventos. Devuelve cuántos analizó de verdad (sin fallback)."""
    done = consec = 0
    for ev in events[:top]:
        before = _LLM_STATS["fallbacks"]
        ev["analysis"] = analyze(ev["_txt"], len(ev["items"]))
        if _LLM_STATS["fallbacks"] > before:
            consec += 1
            if consec >= LLM_BREAKER:
                print(f"  LLM: rate limit persistente ({consec} eventos seguidos) — "
                      "el resto de la corrida usa heurística.")
                break
        else:
            done += 1
            consec = 0
        time.sleep(pause)
    return done

# ── Interpretación de NOTAMs con IA (texto llano). Reusa el proveedor LLM; respaldo heurístico. ──
NOTAM_SYS = (
    "Eres especialista en operaciones airside del Aeropuerto de Punta Cana (PUJ/MDPC). "
    "Explica el NOTAM en español claro y OPERATIVO en UNA sola frase breve (máx ~30 palabras): qué "
    "significa y su implicación concreta para la operación. NUNCA inventes datos que no estén en el texto. "
    "Directo y profesional, sin emojis, sin repetir el código crudo, sin frases de relleno.")

def llm_complete(system, user, prov, max_tokens=220):
    try:
        if prov == "anthropic":
            key = os.environ["ANTHROPIC_API_KEY"]
            model = os.environ.get("AEROINTEL_MODEL", "claude-haiku-4-5-20251001")
            payload = {"model": model, "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user[:1500]}]}
            raw = _llm_post("https://api.anthropic.com/v1/messages", payload,
                {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json", "User-Agent": UA},
                timeout=40)
            return raw["content"][0]["text"].strip()
        if prov in OPENAI_PROVIDERS:
            base, default_model, key_env = OPENAI_PROVIDERS[prov]
            key = os.environ.get(key_env) or os.environ.get("LLM_API_KEY", "")
            model = os.environ.get("AEROINTEL_MODEL", default_model)
            payload = {"model": model, "max_tokens": max_tokens, "temperature": 0.2,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user[:1500]}]}
            raw = _llm_post(base, payload,
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA},
                timeout=40)
            return raw["choices"][0]["message"]["content"].strip()
    except Exception:
        return None

def interpret_notams_llm(notam_list, prov, cap=14, pause=0.8):
    done = 0
    for n in notam_list[:cap]:
        txt = llm_complete(NOTAM_SYS, n.get("raw") or n.get("body") or "", prov, max_tokens=110)
        if not txt:
            break                       # rate-limit/fallo → conservar la lectura heurística para el resto
        # tope duro por si el modelo se extiende: ~1-2 frases, evita tarjetas disparejas
        if len(txt) > 260:
            cut = txt[:260].rsplit(" ", 1)[0]
            txt = cut.rstrip(" ,;:.") + "…"
        n["lectura"], n["lectura_ia"] = txt, True
        done += 1
        time.sleep(pause)
    return done

# ───────────────────────── salida (Mattermost / briefing) ─────────────────────────
COLOR = {"crítico": "#c00000", "importante": "#e69100", "info": "#1F3864"}
EMOJI = {"crítico": "🔴", "importante": "🟠", "info": "🔵"}
LEVEL = {"crítico": "BREAKING", "importante": "UPDATE", "info": "INFO"}

def to_mattermost(ev):
    a, first = ev["analysis"], ev["items"][0]
    sev = a["severidad"]
    sources = ", ".join(sorted({it["source"] for it in ev["items"]}))
    text = (f"**{a.get('titular') or first['title']}**\n\n*Por qué importa:* {a['angulo_editorial']}\n\n"
            f"**Impacto:** {a['impact_score']}/100 · **Confianza:** {a.get('confianza', '-')} · "
            f"**Categoría:** {a['categoria']}")
    return {"username": "AeroIntel",
            "attachments": [{"color": COLOR.get(sev, "#1F3864"),
                             "title": f"{LEVEL.get(sev, 'INFO')} · {a['categoria'].upper()}",
                             "text": text,
                             "footer": f"Fuentes: {sources} · {len(ev['items'])} fuente(s) · {datetime.now():%d %b %Y %H:%M}",
                             "actions": [{"type": "button", "name": "Ver fuente", "url": first["link"]}]}]}

# ── METAR de la estación, traído SERVER-SIDE. aviationweather.gov dejó de permitir CORS en el
#    navegador; el pipeline lo obtiene cada corrida (el METAR se emite cada hora → cron de 30 min
#    lo mantiene fresco) y lo publica en /api/weather.json para lectura mismo-origen. ──
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

# ── Monitor de salud: aviso a Mattermost el DÍA que algo falle (fuente caída, NOTAM, LLM
#    degradado), no semanas después. Solo se envía si hay algo que reportar. ──
def health_payload(fails, total_sources, notam_err=None, llm_fallbacks=0):
    lines = [f"- **{f['name']}**: {f['error']}" for f in fails]
    if notam_err:
        lines.append(f"- **NOTAM (SkyLink)**: {notam_err}")
    if llm_fallbacks:
        lines.append(f"- **LLM**: {llm_fallbacks} evento(s) cayeron a heurística (rate limit/errores persistentes)")
    text = (f"**Monitor de salud de la corrida** · {len(fails)}/{total_sources} fuentes con fallo\n\n"
            + "\n".join(lines)
            + "\n\n_Degradación elegante: el resto del pipeline corrió normal. Revisar si el fallo persiste en corridas siguientes._")
    return {"username": "AeroIntel",
            "attachments": [{"color": "#e69100", "title": "⚠ Salud de fuentes", "text": text}]}

def write_briefing(events):
    lines = ["# Daily Aviation Briefing · AeroIntel", f"_{datetime.now():%d %b %Y %H:%M} · Hub: PUJ_", ""]
    by = {}
    for ev in events:
        by.setdefault(ev["analysis"]["categoria"], []).append(ev)
    for cat, evs in by.items():
        lines.append(f"\n## {cat.upper()}")
        for ev in evs[:6]:
            a, it = ev["analysis"], ev["items"][0]
            lines.append(f"- **[{LEVEL.get(a['severidad'], 'INFO')} · {a['impact_score']}]** {a.get('titular') or it['title']}  ")
            lines.append(f"  {a['angulo_editorial']}  ")
            lines.append(f"  _{it['source']} · {it['link']}_")
    with open(os.path.join(OUT, "briefing.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def entity_chips(analysis):
    """Entidades concretas de la nota (aerolínea/aeropuerto/aeronave/ruta) para badges y el
    'entity plate' de respaldo. Devuelve hasta 3 etiquetas cortas, deduplicadas y en orden de valor."""
    ent = analysis.get("entidades") or {}
    chips = []
    for a in (analysis.get("aerolineas") or [])[:2]:
        chips.append(a.upper())
    for r in (ent.get("rutas") or [])[:1]:
        chips.append(r.upper())
    for ap in (ent.get("aeropuertos") or [])[:2]:
        ap = str(ap).upper()
        if ap not in chips and not any(ap in c for c in chips):
            chips.append(ap)
    for ac in (ent.get("aeronaves") or [])[:1]:
        chips.append(str(ac).upper())
    out = []
    for c in chips:
        c = c.strip()
        if c and c not in out:
            out.append(c)
    return out[:3]

def write_dashboard(events, notam_list=None):
    tpl_path = os.path.join(HERE, "dashboard_template.html")
    if not os.path.exists(tpl_path):
        return
    data = [{"title": ev["analysis"].get("titular") or ev["items"][0]["title"], "link": ev["items"][0]["link"],
             "source": ev["items"][0]["source"], "n": len({i["source"] for i in ev["items"]}),
             "cat": ev["analysis"]["categoria"], "sev": ev["analysis"]["severidad"],
             # puj = mención DIRECTA del hub (badge y vista "Hub PUJ"); el criterio amplio
             # affects_puj (aerolíneas con operación en PUJ) queda para alertas/BD/API.
             "impact": ev["analysis"]["impact_score"], "puj": bool(ev["analysis"].get("puj_direct")),
             "dr": ev["analysis"].get("dr_tier"),   # 'core' = RD · 'regional' = Caribe · null = global
             "why": ev["analysis"]["angulo_editorial"], "resumen": ev["analysis"].get("resumen", ""),
             "fecha": human_age(ev.get("dt")), "img": ev.get("image_url"),
             # iso = publicación en UTC marcada con 'Z' → el navegador calcula la antigüedad EN VIVO
             "iso": (ev["dt"].isoformat() + "Z") if ev.get("dt") else None,
             "ent": entity_chips(ev["analysis"])} for ev in events]
    mm_url = os.environ.get("MATTERMOST_WEB_URL", "https://chatroom.grupopuntacana.com/")
    build_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"   # hora de generación (UTC)
    out = (open(tpl_path, encoding="utf-8").read()
           .replace("/*DATA*/", json.dumps(data, ensure_ascii=False))
           .replace("/*NOTAMS*/", json.dumps(notam_list or [], ensure_ascii=False))
           .replace("__UPDATED__", f"{datetime.now():%d %b %Y %H:%M}")
           .replace("__BUILD_ISO__", build_iso)
           .replace("__MM_URL__", mm_url))
    with open(os.path.join(OUT, "dashboard.html"), "w", encoding="utf-8") as f:
        f.write(out)

def post(hook, payload):
    req = urllib.request.Request(hook, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()

# ───────────────────────── pipeline ─────────────────────────
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

    # 2) LLM (si hay proveedor) SOLO en los top N → respeta el free tier (rate limits)
    prov = os.environ.get("AEROINTEL_LLM") or ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "")
    if prov:
        top = int(os.environ.get("AEROINTEL_LLM_MAX", "20"))
        pause = float(os.environ.get("AEROINTEL_LLM_SLEEP", "2"))
        done = apply_llm(events, top, pause)
        print(f"  LLM ({prov}): {done}/{min(top, len(events))} eventos analizados por IA (resto heurística).")

    # 3) ajustes finales unificados: recencia + ruido + recaps + piso del núcleo RD
    for ev in events:
        apply_ranking_adjustments(ev)
    events.sort(key=lambda e: e["analysis"]["impact_score"], reverse=True)

    # filtro de relevancia: descarta lo de bajo valor (turismo/marketing/sin impacto operacional)
    MIN_SCORE = int(os.environ.get("AEROINTEL_MIN_SCORE", "30"))
    n_before = len(events)
    events = [e for e in events if e["analysis"]["impact_score"] >= MIN_SCORE]
    print(f"  Relevancia (score ≥ {MIN_SCORE}): {n_before} → {len(events)} eventos publicables.")

    # 4) extracción de imágenes en paralelo (solo las notas visibles / top-N)
    n_img = int(os.environ.get("AEROINTEL_IMG_N", "48"))
    if os.environ.get("AEROINTEL_NO_IMG", "").lower() not in ("1", "true", "yes"):
        fetch_images_parallel(events, n=n_img)
        apply_image_boost(events)          # nota con foto real sube un poco (solo reordena)

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
    write_briefing(events)
    write_dashboard(events, notam_list)

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
    if src_fails or notam_alert or _LLM_STATS["fallbacks"]:
        resumen = (f"⚠ salud: {len(src_fails)} fuente(s) caída(s)"
                   + (f" · NOTAM: {notam_alert}" if notam_alert else "")
                   + (f" · LLM→heurística: {_LLM_STATS['fallbacks']}" if _LLM_STATS["fallbacks"] else "")
                   + (f" · reintentos LLM: {_LLM_STATS['retries']}" if _LLM_STATS["retries"] else ""))
        print(f"  {resumen}")
        if hook:
            try:
                post(hook, health_payload(src_fails, len(SOURCES), notam_alert, _LLM_STATS["fallbacks"]))
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
