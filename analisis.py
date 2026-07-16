#!/usr/bin/env python3
# analisis.py — El "porqué" y el ranking: extracción de entidades del texto, ángulo editorial
# heurístico por categoría, modelo de scoring explicable y ajustes deterministas finales.
# QUÉ TOCAR AQUÍ: la redacción del editorial por categoría (_build_editorial), los pesos del
# score (W_GEO/W_SEV/W_CAT) y los ajustes de ranking (recencia, ruido, piso RD).
import re

from ingesta import clean_title, age_hours
from relevancia import (KW_RE, CRIT_RE, IMPORT_RE, detect_airlines, dr_tier, PUJ_DIRECT_RE,
                        NOISE_RE, RECAP_RE, ROUTINE_WX_RE, WX_RECOVERY_RE, WX_ACTIVE_RE)

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

# Respaldo genérico por categoría cuando el editorial dinámico no aplica.
WHY = {
    "meteo":       "Condiciones meteorológicas con posible impacto en operaciones de PUJ y el Caribe; monitorear itinerarios y posibles ajustes (OVER/cancelaciones).",
    "seguridad":   "Evento de seguridad operacional; revisar si involucra aeronaves u operadores con presencia en PUJ.",
    "operaciones": "Posible afectación operativa (retrasos/cancelaciones/huelga); monitorear efecto en itinerarios de PUJ.",
    "regulatorio": "Cambio regulatorio que podría afectar requisitos operativos; evaluar aplicabilidad a PUJ/RD.",
    "rutas":       "Movimiento de red/conectividad relevante para la planificación de PUJ.",
    "industria":   "Relevante para el panorama de la industria; impacto operacional directo bajo.",
    "tecnologia":  "Tendencia tecnológica del sector; impacto operacional directo bajo por ahora.",
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
        # 'strike' solo es huelga si no es bird/lightning/tail strike NI vocabulario militar
        # ('strike fighter/range/group…': el F/A-18 no está en huelga).
        labor_strike = bool(re.search(
            r"\bhuelga\b|\bparo\b|"
            r"(?<!bird )(?<!lightning )(?<!tail )(?<!wildlife )(?<!air)strike\b"
            r"(?!\s+(?:fighter|group|aircraft|carrier|force|range|capabilit|mission|wing|eagle))", tl))
        if any(k in tl for k in ["cancel", "cancela"]):
            event = "cancelaciones"
        elif any(k in tl for k in ["delay", "retraso"]):
            event = "retrasos operativos"
        elif any(k in tl for k in ["ground stop"]):
            event = "ground stop"
        elif any(k in tl for k in ["divert", "desvi", "desví"]):
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
        # Siglas con LÍMITE DE PALABRA: 'faa' como subcadena matchea 'raFAAela', etc.
        if re.search(r"\bfaa\b", tl):
            body_name = "FAA"
        elif re.search(r"\beasa\b", tl):
            body_name = "EASA"
        elif re.search(r"\bntsb\b", tl):
            body_name = "NTSB"
        elif re.search(r"\bidac\b", tl):
            body_name = "IDAC"
        elif re.search(r"\bicao\b|\boaci\b", tl):
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
        # Sistemas TI aeroportuarios (facturación/check-in caídos o restablecidos): es un evento
        # OPERATIVO inmediato — filas, mostradores, tiempos de proceso — no una tendencia.
        if re.search(r"factur|check-?in|outage|ca[ií]da de[l]? sistema|aver[ií]a|restablec|reanuda|"
                     r"system (failure|glitch)|it system", tl):
            parts = ["Incidencia/restablecimiento de sistemas TI aeroportuarios"]
            if places_str:
                parts.append(f"en {places_str}")
            if air_str:
                parts.append(f"aerolíneas afectadas: {air_str}")
            parts.append("impacto directo en el procesamiento de pasajeros — monitorear mostradores, filas y tiempos de proceso")
            return "; ".join(parts) + "."
        # OJO: límite de palabra para 'saf' — como subcadena matchea 'deSAFío'/'SAFety' y generaba
        # lecturas absurdas de "combustibles sostenibles" en notas que no hablaban de eso.
        parts = ["Avance tecnológico en aviación"]
        if re.search(r"\bsaf\b|sustainab|sostenib", tl):
            parts = ["Desarrollo en combustibles sostenibles (SAF)"]
        elif re.search(r"\bevtol\b|electric|eléctric", tl):
            parts = ["Avance en aviación eléctrica/eVTOL"]
        elif "drone" in tl:
            parts = ["Desarrollo en tecnología de drones"]
        if air_str:
            parts.append(f"involucra a {air_str}")
        parts.append("impacto operacional a mediano plazo")
        return "; ".join(parts) + "."

    return WHY.get(cat, "Noticia del sector aeronáutico.")

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
    # Recap/cronología/reseña de evento ya ocurrido: no es breaking — y tampoco merece el
    # piso RD (un libro sobre un aeropuerto de RD sigue siendo contenido editorial, no operativo).
    if RECAP_RE.search(ev["items"][0]["title"]):
        a["impact_score"] -= 20
        if a["severidad"] == "crítico":
            a["severidad"] = "importante"
        is_noise = True
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
