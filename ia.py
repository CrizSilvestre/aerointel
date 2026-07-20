#!/usr/bin/env python3
# ia.py — Todo lo relacionado al LLM: prompts editoriales, proveedores (Groq/OpenRouter/
# Cerebras/Anthropic), reintentos con backoff, cortacircuito ante rate limit persistente y la
# interpretación de NOTAMs. QUÉ TOCAR AQUÍ: el prompt del analista (SYSTEM_PROMPT), el de
# NOTAMs (NOTAM_SYS), los modelos por proveedor y la política de reintentos.
import os, re, json, time, urllib.request, urllib.error

from config import UA
from analisis import analyze_heuristic

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
    # OpenRouter retiró el Llama 3.3 70B gratis (jul 2026). gpt-oss/nemotron son de RAZONAMIENTO
    # (dejan 'content' vacío y JSON sucio). Gemma 4 (instruction-tuned) devuelve JSON limpio y gratis.
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "google/gemma-4-31b-it:free", "OPENROUTER_API_KEY"),
    "cerebras":   ("https://api.cerebras.ai/v1/chat/completions", "llama3.1-8b", "CEREBRAS_API_KEY"),
}

# ── Resiliencia del LLM: reintentos con backoff. En free tier los 429 (rate limit) son normales;
#    reintentar con espera recupera el análisis en vez de degradar a heurística. ──
LLM_RETRIES = int(os.environ.get("AEROINTEL_LLM_RETRIES", "3"))
_LLM_STATS = {"fallbacks": 0, "retries": 0}    # contadores de la corrida (salud → consola/Mattermost)

# Nombres alternativos aceptados para cada clave (comodidad: p. ej. el secret OPEN_ROUTER).
_KEY_ALIASES = {"OPENROUTER_API_KEY": ("OPEN_ROUTER", "OPENROUTER_KEY")}


def _http_reason(e):
    """Mensaje corto del cuerpo de error del proveedor (para diagnosticar 401/402/etc.)."""
    try:
        body = json.loads(e.read())
        return (body.get("error") or {}).get("message") or body.get("message") or e.reason
    except Exception:
        return getattr(e, "reason", "")


def _get_key(key_env):
    """Devuelve la clave del env probando el nombre estándar y sus alias; '' si ninguna existe."""
    v = os.environ.get(key_env)
    if v:
        return v
    for alt in _KEY_ALIASES.get(key_env, ()):
        v = os.environ.get(alt)
        if v:
            return v
    return os.environ.get("LLM_API_KEY", "")


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


def _msg_text(raw):
    """Texto del mensaje del proveedor, tolerante a variantes: algunos modelos dejan 'content'
    vacío y ponen la salida en 'reasoning'/'reasoning_content'."""
    msg = raw["choices"][0]["message"]
    return (msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or "").strip()


def _parse_llm_json(txt):
    txt = (txt or "").strip()
    if txt.startswith("```"):                        # quita cercas markdown ```json … ```
        txt = re.sub(r"^```[a-z]*\s*|\s*```$", "", txt, flags=re.I | re.S).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError("respuesta LLM sin objeto JSON")
    r = json.loads(m.group(0))
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
    key = _get_key(key_env)
    model = os.environ.get("AEROINTEL_MODEL", default_model)
    payload = {"model": model, "max_tokens": 500, "temperature": 0.2,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text[:4000]}]}
    raw = _llm_post(base, payload,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA})
    return _parse_llm_json(_msg_text(raw))


# Proveedores con cuota agotada EN ESTA CORRIDA → no volver a intentarlos (evita quemar tiempo).
_DEAD_PROVIDERS = set()


def _provider_chain():
    """Orden de proveedores a intentar: el primario (AEROINTEL_LLM) y como RESPALDO los demás
    que tengan clave configurada. Así, si el primario agota su cuota diaria (429), la IA sigue
    con otro gratis (mismo modelo Llama 3.3 70B en groq/openrouter) — el carrusel y las noticias
    top nunca quedan en heurística por un tope de cuota. Con una sola clave, se comporta igual."""
    primary = os.environ.get("AEROINTEL_LLM", "").lower()
    chain = []
    if primary in OPENAI_PROVIDERS or primary == "anthropic":
        chain.append(primary)
    if os.environ.get("ANTHROPIC_API_KEY") and "anthropic" not in chain:
        chain.append("anthropic")
    for prov, (_, _, key_env) in OPENAI_PROVIDERS.items():
        if prov not in chain and _get_key(key_env):
            chain.append(prov)
    return chain


def _analyze_with(prov, text):
    r = analyze_anthropic(text) if prov == "anthropic" else analyze_openai_compatible(text, prov)
    r["_llm"] = True          # marca: análisis real de IA (no heurística de respaldo)
    r["_prov"] = prov
    return r


def analyze(text, n_sources):
    for prov in _provider_chain():
        if prov in _DEAD_PROVIDERS:
            continue
        try:
            return _analyze_with(prov, text)
        except urllib.error.HTTPError as e:
            if e.code == 429:                 # cuota agotada → no reintentar este proveedor hoy
                _DEAD_PROVIDERS.add(prov)
                print(f"   ({prov}: cuota agotada (429) — se salta el resto de la corrida)")
            else:                             # 401 clave inválida, 402 requiere créditos, etc.
                _DEAD_PROVIDERS.add(prov)     # error de config → no insistir en cada evento
                print(f"   ({prov}: HTTP {e.code} — {_http_reason(e)}; se salta el resto de la corrida)")
            continue                          # probar el siguiente proveedor del respaldo
        except Exception as e:
            print(f"   ({prov} falló: {type(e).__name__}: {e} — probando siguiente proveedor)")
            continue
    _LLM_STATS["fallbacks"] += 1
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

def upgrade_carousel_llm(carousel_events, pause, adjust):
    """Pase de IA en ORDEN DE PRIORIDAD (main() manda el carrusel primero, luego el top):
    analiza con LLM cada evento que siga en heurística (los que ya tienen IA se saltan),
    reaplica `adjust` (apply_ranking_adjustments) UNA vez sobre el análisis nuevo, y corta
    con el cortacircuito si la cuota muere. Devuelve cuántos quedaron con IA real."""
    done = consec = 0
    for ev in carousel_events:
        if ev["analysis"].get("_llm"):
            continue                                   # ya tiene IA → no gastar otra llamada
        before = _LLM_STATS["fallbacks"]
        fresh = analyze(ev["_txt"], len(ev["items"]))
        if fresh.get("_llm"):
            ev["analysis"] = fresh
            adjust(ev)                                 # una sola pasada de ajustes sobre lo nuevo
            done += 1
            consec = 0
        elif _LLM_STATS["fallbacks"] > before:
            consec += 1
            if consec >= LLM_BREAKER:
                break                                  # cuota agotada → dejar el resto heurístico
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
            key = _get_key(key_env)
            model = os.environ.get("AEROINTEL_MODEL", default_model)
            payload = {"model": model, "max_tokens": max_tokens, "temperature": 0.2,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user[:1500]}]}
            raw = _llm_post(base, payload,
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA},
                timeout=40)
            return _msg_text(raw)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _DEAD_PROVIDERS.add(prov)    # cuota agotada → saltar este proveedor el resto de la corrida
        return None
    except Exception:
        return None


def interpret_notams_llm(notam_list, prov=None, cap=14, pause=0.8):
    chain = _provider_chain()
    if not chain:
        return 0
    done = 0
    for n in notam_list[:cap]:
        txt = None
        for p in chain:                 # respaldo entre proveedores, como en las noticias
            if p in _DEAD_PROVIDERS:
                continue
            txt = llm_complete(NOTAM_SYS, n.get("raw") or n.get("body") or "", p, max_tokens=110)
            if txt:
                break
        if not txt:
            break                       # todos los proveedores fallaron → conservar la heurística
        # tope duro por si el modelo se extiende: ~1-2 frases, evita tarjetas disparejas
        if len(txt) > 260:
            cut = txt[:260].rsplit(" ", 1)[0]
            txt = cut.rstrip(" ,;:.") + "…"
        n["lectura"], n["lectura_ia"] = txt, True
        done += 1
        time.sleep(pause)
    return done
