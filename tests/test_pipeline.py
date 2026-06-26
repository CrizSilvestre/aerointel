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
ok("ruido · noticia operacional NO es ruido", not A.NOISE_RE.search("JetBlue cancels 15 flights at JFK due to weather"))

# ── Severidad / categoría ──
sa = A.analyze_heuristic("Plane crashes during emergency landing at airport", 1)
ok("severidad · crash = crítico", sa["severidad"] == "crítico")
ok("categoria · accidente = seguridad", sa["categoria"] == "seguridad")
ru = A.analyze_heuristic("Delta launches new nonstop service to Santo Domingo", 1)
ok("categoria · nueva ruta = rutas", ru["categoria"] == "rutas")

# ── Extracción de imagen (regex, sin red) ──
ok("img · og:image directo", bool(A._OG_RE.search('<meta property="og:image" content="https://x.com/a.jpg">')))
ok("img · og:image atributos invertidos", bool(A._OG_RE.search('<meta content="https://x.com/b.jpg" property="og:image">')))

# ── Scoring v2: niveles geográficos ──
ok("tier · núcleo RD = core", A.dr_tier("Aerodom amplía el aeropuerto de Punta Cana") == "core")
ok("tier · Caribe = regional", A.dr_tier("Jamaica reopens its main airport") == "regional")
ok("tier · global = None", A.dr_tier("Lufthansa adds Frankfurt to Tokyo route") is None)
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

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
