# tests/test_pipeline.py — pruebas del núcleo (sin red). Uso: python3 tests/test_pipeline.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aerointel as A

fails = 0
def ok(name, cond):
    global fails
    print(f"{'✓' if cond else '✗'} {name}")
    if not cond:
        fails += 1

# ── Matching de aerolíneas: sin falsos positivos de códigos de 2-3 letras ──
ok("airlines · nombre real detectado", "American Airlines" in A.detect_airlines("American Airlines flight AA100 to Miami"))
ok("airlines · 'do/no/or/de' NO son falsos positivos", A.detect_airlines("I do not know if there is no problem or what to do") == [])
ok("airlines · Air Canada", "Air Canada" in A.detect_airlines("Air Canada launches new route to Punta Cana"))
ok("airlines · JetBlue", "JetBlue" in A.detect_airlines("JetBlue Airbus A320 incident"))

# ── República Dominicana primero + relevancia ──
ok("dr · Punta Cana = DR", A.is_dr("Punta Cana airport sees record traffic"))
ok("dr · Londres NO es DR", not A.is_dr("London Heathrow expansion plan"))
ok("relevante · término de aviación", A.is_relevant("Airline cancels flights"))
ok("relevante · clima local de EE.UU. NO entra", not A.is_relevant("Showers and storms forecast across Montana today"))
ok("relevante · ciclón en el Caribe sí", A.is_relevant("Hurricane approaches the Caribbean islands"))
ok("relevante · RD sí", A.is_relevant("El aeropuerto de Punta Cana amplía operaciones"))
ok("relevante · 'jet' en 'objetivo' NO es aviación", not A.is_relevant("El Gobierno tiene como objetivo elevar la inversión en fertilizantes"))
ok("relevante · 'faa' en 'rafaela' NO es aviación", not A.is_relevant("La empresa Rafaela anunció nuevos empleos"))
# Flexibilidad meteo: clima que afecta a RD entra (afecta ops de PUJ); ambiguos/EEUU no.
ok("relevante · vaguada en RD entra", A.is_relevant("Vaguada provoca aguaceros en República Dominicana"))
ok("relevante · ONAMET alerta entra", A.is_relevant("ONAMET emite alerta amarilla para varias provincias"))
ok("relevante · 'temporal' ambiguo NO cuela economía", not A.is_relevant("Serie temporal de exportaciones de la República Dominicana"))
ok("relevante · clima EEUU sin RD NO entra", not A.is_relevant("Heavy rain and storm forecast across Montana"))
# Fuentes nuevas: NHC (cuenca atlántica) y NWS San Juan (vecino de RD) entran; lo lejano no.
ok("relevante · NHC outlook atlántico entra", A.is_relevant("Tropical Storm Emily forms in the central Atlantic, NHC monitoring"))
ok("relevante · tormenta en Pacífico NO entra", not A.is_relevant("Tropical storm forms in the eastern Pacific near Mexico"))
ok("tier · alerta NWS San Juan = regional", A.dr_tier("Flood Advisory issued by NWS San Juan PR") == "regional")
# Nombre de ciclón: 'Hurricane Melissa' sí; 'Hurricane Center' (institución) no.
ok("wx-evento · nombre real detectado", A._extract_context("Hurricane Melissa approaches Jamaica")["wx_events"] == ["Hurricane Melissa"])
ok("wx-evento · 'Hurricane Center' NO es ciclón", A._extract_context("National Hurricane Center Miami issues outlook")["wx_events"] == [])
ok("ruido · 'how to book' turismo", bool(A.NOISE_RE.search("How To Book The Caribbean During Hurricane Season")))

# ── Limpieza de titulares crudos de Google News ──
ct = A.clean_title("Private Jet Crashes At Dominican Republic Airport (JU9j10x0U0) - Mshale")
ok("titulo · quita sufijo de publicación", "Mshale" not in ct)
ok("titulo · quita blob de tracking", "JU9j10x0U0" not in ct)
ok("titulo · conserva el contenido", "Private Jet Crashes" in ct)

# ── Scoring: DR pesa más; ruido turístico se castiga ──
dr = A.analyze_heuristic("Arajet announces new route from Punta Cana to Bogota", 1)
nodr = A.analyze_heuristic("Carrier announces new route from Frankfurt to Tokyo", 1)
ok("score · DR > no-DR", dr["impact_score"] > nodr["impact_score"])
ok("score · DR activa affects_puj", dr["affects_puj"] is True)
ok("ruido · '10 best beaches' detectado", bool(A.NOISE_RE.search("10 best beaches in Punta Cana for your vacation")))
ok("ruido · 'best destinations' turismo", bool(A.NOISE_RE.search("Where The Storms Don't Follow: The Best Caribbean Destinations To Visit Year-Round")))
ok("ruido · noticia operacional NO es ruido", not A.NOISE_RE.search("JetBlue cancels 15 flights at JFK due to weather"))

# ── Severidad / categoría ──
sa = A.analyze_heuristic("Plane crashes during emergency landing at airport", 1)
ok("severidad · crash = crítico", sa["severidad"] == "crítico")
ok("categoria · accidente = seguridad", sa["categoria"] == "seguridad")
ru = A.analyze_heuristic("Delta launches new nonstop service to Santo Domingo", 1)
ok("categoria · nueva ruta = rutas", ru["categoria"] == "rutas")
# Regresión: 'saf' NO debe matchear dentro de 'USAF'/'safety' (límite de palabra)
ok("categoria · 'saf' en 'USAF' NO es tecnología", A.analyze_heuristic("USAF deploys B-2 bombers for exercise", 1)["categoria"] != "tecnologia")
ok("categoria · SAF real sí es tecnología", A.analyze_heuristic("Airline expands sustainable aviation fuel SAF use", 1)["categoria"] == "tecnologia")
ok("categoria · near-miss = seguridad", A.analyze_heuristic("Delta Air Lines flight avoids close call near Boston", 1)["categoria"] == "seguridad")
# Regresión: aerolíneas ambiguas no deben confundir nombres propios
ok("airlines · 'United States' NO es United", A.detect_airlines("United States and Canada sign aviation deal") == [])
ok("airlines · 'Mississippi Delta' NO es Delta", A.detect_airlines("Flooding hits the Mississippi Delta") == [])
ok("airlines · 'Delta Air Lines' SÍ", "Delta" in A.detect_airlines("Delta Air Lines flight diverted"))

# ── Extracción de imagen (regex, sin red) ──
ok("img · og:image directo", bool(A._OG_RE.search('<meta property="og:image" content="https://x.com/a.jpg">')))
ok("img · og:image atributos invertidos", bool(A._OG_RE.search('<meta content="https://x.com/b.jpg" property="og:image">')))

# ── Scoring v2: niveles geográficos ──
ok("tier · núcleo RD = core", A.dr_tier("Aerodom amplía el aeropuerto de Punta Cana") == "core")
ok("tier · Caribe = regional", A.dr_tier("Jamaica reopens its main airport") == "regional")
ok("tier · global = None", A.dr_tier("Lufthansa adds Frankfurt to Tokyo route") is None)
# Regresión: acrónimos RD cortos NO deben matchear como subcadena ni como palabra común
ok("tier · 'jac' en 'hijack' NO es core", A.dr_tier("Erroneous hijack alert on a flight to Tel Aviv") is None)
ok("tier · 'puj' en 'puja' NO es core", A.dr_tier("La puja por el mercado aéreo europeo se intensifica") is None)
ok("tier · 'pop' en 'pop-up storms' NO es core", A.dr_tier("Pop-up storms may cause delays in Florida") is None)
ok("tier · código POP en mayúsculas SÍ es core", A.dr_tier("Airline resumes flights to POP airport") == "core")
ok("tier · JAC como sigla SÍ es core", A.dr_tier("La JAC aprueba nuevas frecuencias aéreas") == "core")
core = A.analyze_heuristic("Arajet cancela vuelos en Punta Cana por mantenimiento", 1)
glob = A.analyze_heuristic("Boeing reports quarterly earnings in Seattle", 1)
ok("score · núcleo RD operacional > industria global", core["impact_score"] > glob["impact_score"])
ok("score · entidades pobladas en RD", core["entidades"]["aerolineas"] == ["Arajet"])
ok("score · breakdown expone geografía", core["score_breakdown"]["geo"] >= 42)

# ── Ajustes de ranking deterministas (recencia/ruido/recap/piso RD) ──
def mkev(title, sev="importante", cat="operaciones", tier="core", impact=40, dt=None):
    return {"items": [{"title": title, "link": "https://e.com/" + title[:8], "source": "Test", "desc": ""}],
            "dt": dt, "_txt": title,
            "analysis": {"titular": title, "categoria": cat, "severidad": sev, "impact_score": impact,
                         "affects_puj": False, "dr_tier": tier, "angulo_editorial": "x", "resumen": "",
                         "aerolineas": [], "entidades": {}}}
noisy = A.apply_ranking_adjustments(mkev("10 best beaches in Punta Cana", tier=None, impact=50))
ok("ajuste · ruido turístico se hunde", noisy["analysis"]["impact_score"] <= 20)
floor = A.apply_ranking_adjustments(mkev("Arajet suspende ruta desde Punta Cana", impact=30))
ok("ajuste · piso RD eleva nota operacional", floor["analysis"]["impact_score"] >= 55)
ok("ajuste · piso RD marca affects_puj", floor["analysis"]["affects_puj"] is True)
recap = A.apply_ranking_adjustments(mkev("Timeline: what we know about the crash", sev="crítico", cat="seguridad", tier=None, impact=70))
ok("ajuste · recap baja severidad", recap["analysis"]["severidad"] != "crítico")
# dr_tier faltante (ruta LLM: el modelo no lo devuelve) → se fija determinista desde el texto
llmev = mkev("Arajet suspende ruta desde Punta Cana", impact=60)
del llmev["analysis"]["dr_tier"]
ok("ajuste · dr_tier se fija si el LLM no lo trae", A.apply_ranking_adjustments(llmev)["analysis"]["dr_tier"] == "core")
# Pronóstico rutinario del día: se hunde bajo el umbral; la alerta real de ONAMET no cae aquí.
rout = A.apply_ranking_adjustments(mkev("Clima en República Dominicana: temperatura y probabilidad de lluvia para Santo Domingo este 2 de julio", impact=70))
ok("ajuste · pronóstico rutinario se hunde", rout["analysis"]["impact_score"] <= 24)
alert = A.apply_ranking_adjustments(mkev("ONAMET emite alerta amarilla por vaguada en República Dominicana", cat="meteo", impact=50))
ok("ajuste · alerta ONAMET real NO se hunde", alert["analysis"]["impact_score"] >= 50)
aid = A.apply_ranking_adjustments(mkev("República Dominicana entrega Bono de Emergencia a más de 1,700 familias", cat="meteo", sev="crítico", impact=74))
ok("ajuste · asistencia social se hunde", aid["analysis"]["impact_score"] <= 24)

# ── Hub PUJ ≠ República Dominicana: puj_direct exige mención del aeropuerto ──
d1 = A.apply_ranking_adjustments(mkev("Arajet añade frecuencias desde Punta Cana a Lima", impact=50))
ok("puj-directo · 'Punta Cana' = directo", d1["analysis"]["puj_direct"] is True)
d2 = A.apply_ranking_adjustments(mkev("IDAC fija nuevas tarifas aeroportuarias en el país", impact=50))
ok("puj-directo · noticia RD sin PUJ NO es directa", d2["analysis"]["puj_direct"] is False)
d3 = A.apply_ranking_adjustments(mkev("JetBlue cancels dozens of flights at JFK", tier=None, impact=50))
ok("puj-directo · aerolínea sin mención PUJ NO es directa", d3["analysis"]["puj_direct"] is False)
d4 = A.apply_ranking_adjustments(mkev("New ILS procedure published for MDPC", tier=None, impact=50))
ok("puj-directo · MDPC = directo", d4["analysis"]["puj_direct"] is True)

# ── Boost de imagen: la nota con foto sube un poco y el ranking se reordena ──
imgA, imgB = mkev("Nota sin foto", tier=None, impact=50), mkev("Nota con foto", tier=None, impact=48)
imgB["image_url"] = "https://x.com/foto.jpg"
boosted = [imgA, imgB]
A.apply_image_boost(boosted, boost=4)
ok("img-boost · con foto supera a sin foto", boosted[0] is imgB and imgB["analysis"]["impact_score"] == 52)
ok("img-boost · sin foto no cambia", imgA["analysis"]["impact_score"] == 50)
cap = mkev("Nota tope", impact=99); cap["image_url"] = "https://x.com/c.jpg"
capped = [cap]; A.apply_image_boost(capped, boost=4)
ok("img-boost · tope en 100", cap["analysis"]["impact_score"] == 100)

# ── entity_chips: entidades concretas para la ficha/badges ──
chips = A.entity_chips({"aerolineas": ["JetBlue"], "entidades": {"aeropuertos": ["PUJ"], "rutas": ["PUJ-JFK"], "aeronaves": []}})
ok("chips · incluye aerolínea", "JETBLUE" in chips)
ok("chips · máximo 3", len(chips) <= 3)

# ── SQLite + API estática (sin red, BD temporal) ──
import tempfile, json as _json
tmp = tempfile.mkdtemp()
import store, apiexport
conn = store.connect(os.path.join(tmp, "t.db"))
evs = [mkev("Arajet abre ruta Punta Cana-Lima", impact=72), mkev("Delay en SDQ", impact=58)]
store.upsert_events(conn, evs, A.canonical)
store.record_run(conn, 10, 2, 2, 1, True)
an = store.analytics(conn)
ok("db · persiste artículos", an["articles_total"] == 2)
ok("db · registra corrida", len(an["recent_runs"]) == 1)
store.upsert_events(conn, evs, A.canonical)  # misma nota otra vez → dedup por URL, no duplica
ok("db · dedup entre corridas por URL", store.analytics(conn)["articles_total"] == 2)
apiexport.write_api(evs, tmp, [{"name": "Test", "type": "rss"}], an, A.human_age)
ok("api · escribe latest.json", os.path.exists(os.path.join(tmp, "api/news/latest.json")))
latest = _json.load(open(os.path.join(tmp, "api/news/latest.json")))
ok("api · latest tiene items", latest["count"] == 2 and bool(latest["items"][0]["titular"]))
ok("api · analytics.json existe", os.path.exists(os.path.join(tmp, "api/analytics.json")))
conn.close()

# ── Resiliencia LLM: backoff + reintentos (sin red: urlopen simulado) ──
ok("backoff · exponencial 2/6/18", (A._retry_delay(0), A._retry_delay(1), A._retry_delay(2)) == (2.0, 6.0, 18.0))
ok("backoff · respeta Retry-After", A._retry_delay(0, "7") == 7.0)
ok("backoff · Retry-After con tope 30s", A._retry_delay(0, "300") == 30.0)
ok("backoff · Retry-After inválido → exponencial", A._retry_delay(1, "soon") == 6.0)

import urllib.error, io as _io
_calls = {"n": 0}
def _fake_urlopen_429(req, timeout=None, **kw):
    _calls["n"] += 1
    if _calls["n"] < 3:   # dos 429 y a la tercera responde
        raise urllib.error.HTTPError("http://fake", 429, "Too Many Requests", {"Retry-After": "0"}, _io.BytesIO(b""))
    class R:
        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'
    return R()
_real_urlopen, _real_sleep = A.urllib.request.urlopen, A.time.sleep
A.urllib.request.urlopen, A.time.sleep = _fake_urlopen_429, lambda s: None
try:
    r = A._llm_post("http://fake", {}, {})
    ok("retry · recupera tras dos 429", r["choices"][0]["message"]["content"] == "ok" and _calls["n"] == 3)
    _calls["n"] = 0
    def _fake_urlopen_401(req, timeout=None, **kw):
        _calls["n"] += 1
        raise urllib.error.HTTPError("http://fake", 401, "Unauthorized", {}, _io.BytesIO(b""))
    A.urllib.request.urlopen = _fake_urlopen_401
    try:
        A._llm_post("http://fake", {}, {})
        ok("retry · 401 NO se reintenta", False)
    except urllib.error.HTTPError:
        ok("retry · 401 NO se reintenta (falla directo)", _calls["n"] == 1)
finally:
    A.urllib.request.urlopen, A.time.sleep = _real_urlopen, _real_sleep

# ── Fail-fast: 429 con Retry-After largo (cuota agotada) NO se reintenta ──
_calls["n"] = 0
def _fake_urlopen_429_long(req, timeout=None, **kw):
    _calls["n"] += 1
    raise urllib.error.HTTPError("http://fake", 429, "Too Many Requests", {"Retry-After": "600"}, _io.BytesIO(b""))
A.urllib.request.urlopen, A.time.sleep = _fake_urlopen_429_long, lambda s: None
try:
    try:
        A._llm_post("http://fake", {}, {})
        ok("retry · Retry-After largo falla directo", False)
    except urllib.error.HTTPError:
        ok("retry · Retry-After largo falla directo (1 llamada)", _calls["n"] == 1)
finally:
    A.urllib.request.urlopen, A.time.sleep = _real_urlopen, _real_sleep

# ── Cortacircuito: fallos LLM consecutivos → resto de la corrida en heurística ──
def _mkev_llm(i):
    return {"items": [{"title": f"Evento {i}", "link": f"https://e.com/{i}", "source": "T", "desc": ""}],
            "dt": None, "_txt": f"Evento {i}", "analysis": {}}
_an_calls = {"n": 0}
_real_analyze = A.analyze
def _fake_analyze_fail(text, n_sources):
    _an_calls["n"] += 1
    A._LLM_STATS["fallbacks"] += 1          # simula: agotó reintentos y cayó a heurística
    return A.analyze_heuristic(text, n_sources)
A.analyze = _fake_analyze_fail
try:
    evs5 = [_mkev_llm(i) for i in range(5)]
    done = A.apply_llm(evs5, top=5, pause=0)
    ok("breaker · corta tras 3 fallos seguidos", _an_calls["n"] == 3 and done == 0)
    _an_calls["n"] = 0
    def _fake_analyze_ok(text, n_sources):
        _an_calls["n"] += 1
        return A.analyze_heuristic(text, n_sources)
    A.analyze = _fake_analyze_ok
    done = A.apply_llm([_mkev_llm(i) for i in range(4)], top=4, pause=0)
    ok("breaker · sin fallos analiza todos", _an_calls["n"] == 4 and done == 4)
finally:
    A.analyze = _real_analyze

# ── Monitor de salud: payload para Mattermost ──
hp = A.health_payload([{"name": "Fuente X", "ok": False, "items": 0, "error": "HTTPError: 503"}],
                      21, notam_err="HTTP 429 rate limit", llm_fallbacks=2)
htxt = hp["attachments"][0]["text"]
ok("salud · nombra la fuente caída", "Fuente X" in htxt and "503" in htxt)
ok("salud · incluye NOTAM y LLM", "NOTAM" in htxt and "heurística" in htxt)
ok("salud · conteo de fuentes", "1/21" in htxt)

# ── NOTAMs: clasificación de sujeto/importancia/estado (sin red) ──
import notams as NT
nd = NT.normalize({"notam_id": "A1/26", "type": "N", "location": "MDPC",
                   "effective": "2026-06-01T00:00:00Z", "expiration": "2027-01-01T00:00:00Z",
                   "body": "RWY 08/26 CLSD", "raw": "RWY 08/26 CLSD", "source": "AIS"})
ok("notam · RWY CLSD = Pista", nd["subject"] == "Pista")
ok("notam · RWY CLSD = alta importancia", nd["importance"] == "alta")
ok("notam · vigente (fechas)", nd["status"] == "vigente")
ok("notam · ILS GP U/S = navegación/alta", NT.classify("ILS RWY 08 GP U/S") == ("Ayuda a navegación", "alta"))
ok("notam · crane iluminado = Obstáculo (no Iluminación)", NT.classify("OBST CRANE 145FT MARKED AND LGT")[0] == "Obstáculo")
ok("notam · TWY WIP = Calle de rodaje/media", NT.classify("TWY C WIP") == ("Calle de rodaje", "media"))
ok("notam · expirado se detecta", NT.normalize({"expiration": "2020-01-01T00:00:00Z", "raw": "x"})["status"] == "expirado")
ok("notam · lectura operativa no vacía", len(nd["lectura"]) > 20)
ok("notam · lectura de RWY CLSD menciona pista", "pista" in NT.interpret_heuristic({"subject": "Pista", "body": "RWY 08/26 CLSD"}).lower())
ok("notam · lectura ILS U/S menciona servicio", "servicio" in NT.interpret_heuristic({"subject": "Ayuda a navegación", "body": "ILS RWY 08 GP U/S"}).lower())
# Fuente con enlace en cada NOTAM (como las noticias); el scope ya no se disfraza de fuente.
ok("notam · fuente explícita se respeta", nd["source"] == "AIS")
ok("notam · fuente por defecto = AIS/IDAC", NT.normalize({"raw": "TWY C WIP", "scope": "A"})["source"] == NT.SOURCE_NAME)
ok("notam · source_url presente", NT.normalize({"raw": "TWY C WIP"})["source_url"].startswith("https://"))
ok("notam · scope separado de la fuente", NT.normalize({"raw": "TWY C WIP", "scope": "A"})["scope"] == "Aeródromo")

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
