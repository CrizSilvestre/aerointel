#!/usr/bin/env python3
# relevancia.py — Qué entra y cómo se clasifica: keywords por categoría, severidad, detección
# de aerolíneas, niveles geográficos RD (core/regional), filtros de ruido y señal de aviación.
# QUÉ TOCAR AQUÍ: agregar/quitar keywords de una categoría, términos RD, patrones de ruido.
# (Las aerolíneas de PUJ se editan en airlines_puj.json, no aquí.)
import re

from config import load_json

# ───────────────────────── keywords por categoría ─────────────────────────
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

# ── Aerolíneas (matching robusto): SOLO nombres reales (≥4 chars, sin códigos de 2-3 letras
#    en mayúsculas) para evitar falsos positivos en texto libre (DO, NO, OR, AA, DE…). Word boundary.
AIRLINES = load_json("airlines_puj.json", {"airlines": []})["airlines"]
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
