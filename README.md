# AeroIntel — Aviation Intelligence (Hub PUJ · República Dominicana)

Plataforma autónoma de **inteligencia aeronáutica**: vigila noticias 24/7, las analiza con criterio
editorial de analista de operaciones, deduplica, **puntúa por relevancia operacional** (República
Dominicana primero), selecciona imagen y publica un **periódico web** + una **API JSON** + (opcional)
avisos a Mattermost. No es un blog de noticias: el objetivo es responder *"¿por qué esto importa para
la operación de PUJ?"*.

> Estado: **beta funcional**. Hub coordinado: **PUJ** (Aeropuerto de Punta Cana).
> También cubre SDQ / STI / POP y aviación global cuando impacta a RD/Caribe.
>
> **En vivo:** https://crizsilvestre.github.io/aerointel/ · API: `/api/news/latest.json`

---

## Arquitectura (y por qué)

Decisión deliberada: un **motor de generación estática** (Python de librería estándar) + **SQLite** +
**API JSON estática** servida por **GitHub Pages**. Cero servidor que mantener, cero costo, no se cae.

```
 Fuentes RSS/Google News
        │  ingesta (urllib, stdlib)
        ▼
 recencia ──► relevancia (señal de aviación) ──► dedup/cluster (difflib)
        │
        ▼
 pre-score heurístico (TODO) ──► LLM analista (top-N, free tier) ──► ajustes deterministas
        │                                                              (recencia/ruido/recap/piso RD)
        ▼
 imágenes (og:image de fuentes confiables + filtro anti-placeholder)
        │
        ├──► output/dashboard.html      periódico web (sin build, sin dependencias)
        ├──► output/api/news/*.json      API estática (latest/categories/sources)
        ├──► output/api/analytics.json   métricas históricas
        ├──► aerointel.db (SQLite)        historial + dedup entre corridas
        └──► Mattermost (opcional)        breaking/update
```

### Componentes

Código dividido por responsabilidad — **para editar algo, este es el mapa**:

| Archivo | Qué tocar ahí |
|---|---|
| `aerointel.py` | Solo el **orquestador** (`main()`): el orden del pipeline. |
| `config.py` | Rutas, user-agents, carga de JSON. |
| `ingesta.py` | Descarga/parseo de feeds, fechas, limpieza de titulares, dedup/cluster. |
| `relevancia.py` | **Keywords por categoría**, severidad, aerolíneas, niveles RD, filtros de ruido. |
| `analisis.py` | El **"porqué"** (editorial heurístico), pesos del score, ajustes de ranking. |
| `ia.py` | **Prompts del LLM**, proveedores, reintentos/cortacircuito, lectura de NOTAMs. |
| `imagenes.py` | og:image, filtros anti-placeholder, boost de foto. |
| `clima.py` | METAR server-side (`/api/weather.json`). |
| `notams.py` | NOTAMs de MDPC (SkyLink): clasificación y lectura operativa. |
| `nas.py` | Estado del NAS de la FAA (ground stops EE.UU., marca "Ruta PUJ"). |
| `salida.py` | Dashboard, Mattermost (breaking + salud), briefing. |
| `store.py` | Persistencia SQLite: artículos, historial y analítica. |
| `apiexport.py` | API estática JSON (`/api/news/*`, `/api/analytics`). |
| `dashboard_template.html` | La web en sí (HTML/CSS/JS del periódico). |
| `api_server/` | API REST en vivo OPCIONAL (FastAPI). La producción no la necesita. |

Config editable **sin tocar código**: `sources.json` (fuentes), `airlines_puj.json` (aerolíneas
PUJ), `nas_puj_airports.json` (aeropuertos EE.UU. con ruta a PUJ).
Suite: `tests/test_pipeline.py` (130+ asserts sin red) · Cron: `.github/workflows/update.yml`.

¿Por qué **no** Postgres + React + servidor FastAPI como stack principal? Porque añadirían hosting,
una BD gestionada y mantenimiento de uptime — fragilidad y costo contra las prioridades
(*confiabilidad, mantenibilidad*). SQLite cubre la BD sin ops; la API estática cubre `/api/news/*`
sin servidor; el FastAPI queda como módulo aditivo para quien quiera consultas en vivo.

---

## Las cuatro mejoras clave de esta beta

1. **Imágenes correctas (no genéricas).** Solo se usa la `og:image` real del artículo de **fuentes
   directas confiables** (Simple Flying, AeroTime, Aviation Week, Diario Libre). Se rechazan
   placeholders y logos (p. ej. la tarjeta genérica de Google News) por **frecuencia** (una foto real
   es única; un logo se repite) y por **blocklist** (`gstatic`, `/logo`, `not.exist`, tamaños mini).
   Si no hay foto fiable → **ficha de inteligencia**: categoría + entidades reales (aerolínea/
   aeropuerto/aeronave/ruta). Nunca un avión de stock que no corresponde a la nota.

2. **Texto de analista (no bot).** El prompt actúa como *analista senior de operaciones*: exige
   especificidad, entidades nombradas, consecuencia operacional de segundo orden y prohíbe relleno;
   incluye un ejemplo few-shot. El fallback heurístico también construye un ángulo editorial
   contextual a partir de las entidades extraídas.

3. **Scoring inteligente (RD primero).** Modelo ponderado y **explicable** (`score_breakdown`):
   geografía (núcleo RD 42 / regional 18) + severidad + valor operacional de la categoría + riqueza
   de entidades + corroboración. Ajustes deterministas: recencia, **tope duro al ruido turístico**,
   castigo a recaps/pronósticos rutinarios y **piso para el núcleo RD**. Corrige falsos positivos
   por subcadena (p. ej. `"jet"` dentro de `"objetivo"`, `"jac"` dentro de `"hijack"`) con límites
   de palabra. El dashboard tiene **sección propia "República Dominicana"** (nav + portada, país
   primero) alimentada por el nivel geográfico (`dr_tier`), y **"Hub PUJ"** con las noticias de
   **mención directa** del aeropuerto (`puj_direct`: Punta Cana / PUJ / MDPC) — el criterio amplio
   `affects_puj` (aerolíneas con operación en PUJ) se mantiene para alertas y API.

4. **Actualización automática.** GitHub Actions cron **cada 30 minutos**: corre los tests, ejecuta
   el pipeline y **despliega a GitHub Pages**. El usuario abre la página y ya está al día. Ver más abajo.

---

## Uso local

```bash
# 1) Heurística pura (sin API key, dry-run) — útil para probar rápido
python3 aerointel.py

# 2) Con LLM analista (recomendado: Groq, free tier real) y publicación a Mattermost
AEROINTEL_LLM=groq GROQ_API_KEY=xxx \
MATTERMOST_WEBHOOK_URL=https://.../hooks/xxx \
python3 aerointel.py

# 3) Ver el periódico (servidor estático local)
node serve.mjs          # http://localhost:8200/dashboard.html

# 4) API REST en vivo OPCIONAL
pip install -r api_server/requirements.txt
uvicorn api_server.app:app --port 8000     # http://localhost:8000/docs

# 5) Tests (sin red)
python3 tests/test_pipeline.py
```

### Variables de entorno

Ver `.env.example`. Las más relevantes:

| Variable | Default | Qué hace |
|---|---|---|
| `AEROINTEL_LLM` | (vacío) | Proveedor: `groq` \| `openrouter` \| `cerebras` \| `anthropic`. Vacío = heurística. |
| `GROQ_API_KEY` | — | Clave del proveedor (Groq es gratis: console.groq.com). |
| `AEROINTEL_LLM_MAX` | 18 | Cuántos eventos top analiza el LLM (el resto, heurística). |
| `AEROINTEL_LLM_SLEEP` | 2.0 | Pausa entre llamadas (respeta rate limits del free tier). |
| `AEROINTEL_LLM_RETRIES` | 3 | Reintentos ante 429/5xx con backoff (respeta `Retry-After`; 4xx real no se reintenta). |
| `AEROINTEL_LLM_BREAKER` | 3 | Cortacircuito: tras N eventos seguidos con LLM agotado, el resto de la corrida usa heurística. |
| `AEROINTEL_MIN_SCORE` | 30 | Umbral de publicación. |
| `AEROINTEL_IMG_N` | 48 | Cuántas notas top enriquecen con imagen. `AEROINTEL_NO_IMG=1` desactiva. |
| `AEROINTEL_IMG_BOOST` | 4 | Empuje de score a notas con foto real (solo reordena la portada; 0 = off). |
| `AEROINTEL_WHEN` / `AEROINTEL_MAX_AGE_H` | 7d / 168 | Ventana de recencia. |
| `MATTERMOST_WEBHOOK_URL` | — | Si está, publica de verdad; si no, dry-run. |

---

## API

**Estática (producción, vía Pages):**
`/api/news/latest.json` · `/api/news/categories.json` · `/api/news/sources.json` · `/api/analytics.json`

**REST en vivo (opcional, FastAPI):**
`GET /api/news/latest?limit=&category=&puj=&min_impact=` · `/api/news/categories` ·
`/api/news/sources` · `/api/analytics` · `/docs` (OpenAPI).

`/api/notams.json` — NOTAMs activos de la estación (si está configurado).
`/api/nas.json` — Estado del NAS (FAA): ground stops/demoras/cierres activos en EE.UU.
`/api/weather.json` — METAR de MDPC traído server-side (el navegador lo lee mismo-origen).

---

## NOTAMs de la estación (categoría operativa)

Sección **NOTAM** con los avisos a la navegación aérea **vigentes y programados** de la estación
(**MDPC** = Punta Cana). `notams.py` los clasifica por **sujeto operativo** (Pista, Calle de
rodaje, Plataforma, Ayuda a navegación, Iluminación, Obstáculo, Actividad UAS/drones, Fauna…),
marca **importancia alta** (cierres de pista/aeródromo, ayudas críticas U/S) y **estado**
(vigente/programado), con la vigencia en **hora local RD**.

- **Fuente primaria: FAA NOTAM Search** (gratis, sin clave, distribución oficial). Auditoría
  jul 2026: SkyLink servía 16 NOTAMs cuando la FAA distribuía 8 — retenía avisos ya
  incorporados al AIP (hasta 177 días viejos) y omitía nuevos.
- **Respaldo: SkyLink API** (RapidAPI) si la FAA no responde. La clave (`RAPIDAPI_KEY`) es
  **server-side** y nunca llega al navegador. Sin ninguna fuente, la sección no aparece.
- `AEROINTEL_NOTAM_DEMO=1` muestra NOTAMs de ejemplo para previsualizar la categoría.
- Cada tarjeta cita su **fuente con enlace** (como las noticias): AIS/IDAC por defecto
  (`AEROINTEL_NOTAM_SOURCE` / `AEROINTEL_NOTAM_SOURCE_URL` lo personalizan).
- Referencial/operativo — la **fuente oficial es AIS/IDAC**.

> Endpoint real usado: `GET https://skylink-api.p.rapidapi.com/notams/{ICAO}` (fechas en formato
> NOTAM `YYYYMMDDHHMM` / `PERM`; el código las normaliza).

---

## Estado del NAS · EE.UU. (categoría operativa)

Sección **NAS EE.UU.** con los **ground stops, programas de demora y cierres activos** del
sistema estadounidense, vía el API oficial de la FAA (`nasstatus.faa.gov`). Un ground stop en
MIA/JFK/FLL cascadea directo a los itinerarios de PUJ: los eventos en aeropuertos con servicio
a Punta Cana se marcan **Ruta PUJ** y encabezan la lista. La lista de estaciones es **editable
por operaciones** en `nas_puj_airports.json` (las rutas cambian por temporada; LGA/DCA nunca —
regla de perímetro).
Causas traducidas al español; cierres solo-aviación general se etiquetan aparte (no afectan
la operación comercial). Sin clave ni registro; si el API falla, la sección no aparece.
`AEROINTEL_NAS_DEMO=1` para previsualizar con datos de ejemplo.

---

## Botón "Actualizar" (refresh bajo demanda)

El dashboard tiene un botón **⟳ Actualizar** que dispara una corrida del pipeline sin entrar a
GitHub. Arquitectura: un **Cloudflare Worker** (`worker/`) guarda el token de GitHub como secret
(el navegador jamás lo ve) y aplica el **límite de 1 disparo cada 30 min del lado del servidor**
(consulta la última corrida real — cron o manual — en GitHub; nadie lo brinca ni con curl).
El botón muestra el estado honesto: disponible, "En N min", o confirmación de disparo.

- Deploy del Worker: `cd worker && npx wrangler@4 deploy` · secret: `npx wrangler@4 secret put GITHUB_TOKEN`
  (PAT fine-grained: solo este repo, permiso Actions Read/Write).
- El dashboard recibe la URL vía `AEROINTEL_REFRESH_URL` (workflow); vacía → botón oculto.

---

## Automatización / despliegue (24/7)

1. Sube el repo a GitHub.
2. **Settings → Secrets and variables → Actions** → `GROQ_API_KEY` (y opcional `MATTERMOST_WEBHOOK_URL`).
3. **Settings → Pages → Source: GitHub Actions.**
4. El workflow `update.yml` corre **cada 30 minutos** (y a mano con *Run workflow*): tests →
   pipeline → deploy. La portada queda en la URL de Pages.

> La BD SQLite en Actions es efímera (se regenera cada corrida); el historial de largo plazo vive
> donde se persista la BD (ejecución local o un runner con almacenamiento).

---

## Fuentes y redes sociales (decisión de ingeniería)

Se prioriza **calidad sobre cantidad** — 20 fuentes, todas verificadas vivas antes de agregarse:

- **7 consultas Google News** acotadas: PUJ/RD, Aviation/Caribbean, Seguridad, Meteo Caribe,
  Meteo RD, **Regulatorio** (FAA/EASA/NTSB/ICAO) y **Rutas Caribe/Latam**.
- **13 RSS directos**: Simple Flying, AeroTime, Aviation Week, Diario Libre, **NHC Atlántico**
  (ciclones — fuente oficial de meteo tropical), **NWS San Juan** (alertas Atom del vecino
  inmediato; vacío sin eventos activos), **Arecoa** (aviación RD), **Dominican Today**,
  Flightradar24, Leeham News (industria), A21, Aviación al Día (Latam, ES), **Airbus Press
  (oficial)**.

FAA/EASA/NTSB/Boeing no publican RSS estable (verificado: 404/timeout) — se cubren con la
consulta Regulatorio de Google News. Lo evaluado y descartado queda documentado en
`sources.json` → `_fuentes_evaluadas_sin_feed`.

### Resiliencia (diseñada para depender de servicios gratis)

- **LLM con reintentos + backoff**: los 429 del free tier se reintentan (2s → 6s → 18s,
  respetando `Retry-After`, tope 30 s); un 4xx real no se reintenta y un `Retry-After` largo
  (cuota por minutos/día) falla directo. Si todo falla, la nota cae a la heurística — la
  corrida nunca se rompe. **Cortacircuito**: 3 eventos seguidos sin LLM → el resto de la
  corrida va directo a heurística (no quema minutos del cron en reintentos condenados).
- **Monitor de salud por corrida**: si una fuente cae, el NOTAM falla (con clave puesta) o el
  LLM degrada, se publica un **aviso a Mattermost** con el detalle — te enteras el día que
  pasa, no semanas después. Si todo está bien, no envía nada.
- **Degradación elegante en todo**: fuente caída → se omite; sin imagen → ficha de
  inteligencia; sin LLM → heurística; sin clave NOTAM → sección oculta.

**Instagram / redes sociales:** no existe API pública estable para leer perfiles/reels de terceros;
el scraping de Instagram es frágil y viola sus términos. La vía profesional es **RSS/feeds oficiales
y APIs autorizadas**, no scraping inestable. Por eso AeroIntel **no** depende de Instagram; cuando una
cuenta tenga feed/sitio oficial, se agrega como fuente RSS.

---

## ¿Integrar en AeroSuite?

Evaluado: AeroIntel encaja como **módulo de inteligencia** dentro de AeroSuite (otra herramienta de
PUJ), igual que AeroWeather/AIPC. Recomendación: mantener AeroIntel como **servicio independiente**
(su propio cron + Pages) y exponer su **API JSON** para que el shell de AeroSuite lo embeba (iframe o
fetch del `latest.json`). Así se respeta la separación limpia sin acoplar despliegues.

---

## Pruebas

`python3 tests/test_pipeline.py` — 100+ asserts sin red: matching de aerolíneas, relevancia (incl.
regresiones `jet`/`objetivo` y `jac`/`hijack`), niveles geográficos, ajustes de ranking (ruido/
recap/piso RD/pronóstico rutinario), boost de imagen, resiliencia LLM (retry/backoff/cortacircuito
con red simulada), monitor de salud, METAR server-side, NOTAMs, y el ciclo SQLite + API estática.
