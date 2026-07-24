# AGENTS.md — AeroIntel

Instrucciones para agentes de codificación que trabajen en este repositorio
(Antigravity / Gemini, Codex, Cursor). Claude Code lee además `CLAUDE.md`.

---

## Tu rol

En este proyecto trabajan dos agentes con roles fijos:

- **Claude Code = arquitecto.** Diseña, decide, escribe el spec y revisa el diff.
- **Tú (Antigravity) = implementador.** Ejecutas el spec al pie de la letra.

No rediseñes. Si el spec te parece equivocado, **detente y escribe la objeción en el
reporte** en vez de improvisar una solución distinta. Una desviación silenciosa
cuesta más que una pregunta.

## Protocolo de trabajo

1. Lee tu tarea en `.agent/spec-activo.md`. Si no existe, no hay trabajo asignado: pregunta.
2. Trabaja **siempre en una rama**: `git checkout -b <rama-indicada-en-el-spec>`. Nunca en `main`.
3. Implementa solo lo que el spec pide. Nada de refactors de oportunidad, renombres,
   reordenar imports ni "mejoras" no solicitadas — ensucian el diff que se va a revisar.
4. Corre `python3 tests/test_pipeline.py`. **Debe pasar al 100%**. Si tu cambio altera
   comportamiento cubierto por un assert, añade o ajusta el assert y explícalo en el reporte.
5. Escribe `.agent/reporte-activo.md` con la plantilla de `.agent/TEMPLATE-reporte.md`.
6. Commitea en la rama. **No hagas push, no abras PR, no mergees a `main`.** El trabajo se
   revisa en local.

---

## Reglas duras del proyecto — no negociables

Romper cualquiera de estas invalida el trabajo completo.

1. **Solo librería estándar de Python.** El pipeline no tiene dependencias externas y así
   se queda. No `requests`, no `feedparser`, no `beautifulsoup`, no `pandas`. No crees
   `requirements.txt` ni `pyproject.toml`. Se usa `urllib`, `xml.etree`, `sqlite3`, `re`, `json`.
2. **Sin servidor.** La salida es estática: HTML + JSON servidos por GitHub Pages, generados
   por un cron de GitHub Actions. No introduzcas FastAPI, Flask, Node en runtime ni base de
   datos gestionada. (`serve.mjs` es solo para previsualizar en local.)
3. **Sin paso de build.** `dashboard_template.html` es HTML+CSS+JS a mano, sin bundler,
   sin framework, sin npm install. Se edita directo.
4. **Los tests son offline.** Nada en `tests/` puede tocar la red. Si necesitas simular
   HTTP, monkeypatchea `urllib.request.urlopen` como ya se hace en el suite.
5. **Nunca commitees secretos.** Las claves van por variables de entorno / GitHub Secrets.
   `.env` no se sube. Si el spec menciona una clave, usa `os.environ.get(...)`.
6. **Degradación elegante.** Toda llamada externa (feeds, LLM, NOTAM, clima) debe fallar
   sin tumbar el run. Si la IA no responde, la heurística cubre. Si una fuente cae, se
   registra en el health monitor y el pipeline sigue.

## Mapa: ¿dónde toco qué?

`aerointel.py` es **solo el orquestador** (`main()`). La lógica vive en módulos con nombre
en español. Pon el código donde corresponde:

| Archivo | Responsabilidad |
|---|---|
| `config.py` | Rutas (`HERE`/`OUT`), user-agents, `load_json` |
| `ingesta.py` | Descarga y parseo de RSS, fechas, limpieza de títulos, clustering, `SOURCES` |
| `relevancia.py` | Palabras clave, detección de aerolíneas, tiers geográficos RD, filtro de ruido |
| `analisis.py` | Scoring, análisis heurístico, ángulo editorial, ajustes de ranking, entidades |
| `ia.py` | Proveedores LLM, prompt de sistema, reintentos, circuit breaker, cadena de respaldo |
| `imagenes.py` | Extracción de imágenes (feed, og:image), filtro de calidad, boost |
| `clima.py` | METAR / clima |
| `notams.py` | NOTAMs: FAA (primario), SkyLink (respaldo), clasificación, interpretación |
| `nas.py` | Estado del NAS de la FAA (ground stops, cierres) para rutas PUJ |
| `salida.py` | Render del dashboard, briefing, payload de salud, Mattermost |
| `store.py` | Persistencia SQLite |
| `apiexport.py` | Export de JSON estáticos a `/api/` |

El grafo de importaciones es **acíclico** y debe seguir siéndolo:
`analisis ← (relevancia, ingesta)` · `ia ← (config, analisis)` · `clima ← ingesta` ·
`salida ← (config, ingesta, analisis)`

## Contexto del dominio

AeroIntel es un sistema de inteligencia aeronáutica que vigila noticias 24/7 y las publica
priorizadas para jefes de operaciones de aeropuerto. La prioridad geográfica es
**República Dominicana, con foco en Punta Cana (PUJ/MDPC)**.

Dos conceptos que se confunden y **no son lo mismo** — no los unifiques:
- `dr_tier`: `core` (RD), `regional` (Caribe) o global.
- `puj_direct`: mención directa a Punta Cana / PUJ / MDPC.
- `affects_puj`: más amplio — incluye aerolíneas que operan PUJ aunque no se nombre el aeropuerto.

Los códigos IATA ambiguos (`PUJ`, `POP`, `STI`, `AZS`, `LRM`) se detectan **solo en mayúsculas
y con límite de palabra**. Ya hubo bugs por esto: "hijack" contenía "jac", "Mario Pujols"
contenía "puj". No relajes esas regex.

Estilo del código: nombres en español para el dominio, comentarios escasos, funciones cortas,
sin abstracciones prematuras. Escribe código que se parezca al que ya está alrededor.
