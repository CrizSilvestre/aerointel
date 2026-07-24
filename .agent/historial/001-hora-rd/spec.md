# SPEC 001 — Horas locales de RD en las salidas

**Rama:** `fix/hora-rd`
**Escrito por:** Claude Code · 24 jul 2026
**Ejecuta:** Antigravity

## Objetivo

Que todas las horas que AeroIntel muestra a un humano estén en hora de República Dominicana
(UTC-4), sin importar dónde corra el pipeline.

## Contexto

En el commit `dd70003` se añadió la etiqueta `(hora RD)` al footer de las alertas de
Mattermost, pero la hora se sigue generando con `datetime.now()`, que devuelve la hora
**local de la máquina**. En el Mac de desarrollo eso da la hora correcta; los runners de
GitHub Actions corren en **UTC** y el workflow `.github/workflows/update.yml` no fija la
variable `TZ`. En producción, entonces, la alerta muestra una hora 4 horas adelantada
mientras afirma que es hora dominicana.

El mismo defecto afecta a otras dos salidas visibles que ya existían: la cabecera de
`briefing.md` y el sello `__UPDATED__` del dashboard.

República Dominicana usa UTC-4 todo el año — **no aplica horario de verano**, así que un
offset fijo es correcto y no necesita `zoneinfo` ni base de datos de zonas horarias.

## Archivos a tocar

| Archivo | Qué cambia |
|---|---|
| `salida.py` | Import de `datetime`, constante `RD_TZ` nueva, 3 llamadas a `datetime.now()` |
| `tests/test_pipeline.py` | Asserts nuevos |

Si necesitas tocar cualquier otro archivo, **detente y repórtalo**.

## Cambios

### 1. `salida.py` — import y constante

En la línea 7, `from datetime import datetime` pasa a incluir `timezone` y `timedelta`.

Justo debajo del bloque de constantes existente (`COLOR`, `EMOJI`, `LEVEL`, `CAT_ES`),
añade:

```python
RD_TZ = timezone(timedelta(hours=-4))   # RD no aplica horario de verano
```

### 2. `salida.py` — las tres horas visibles

Sustituye `datetime.now()` por `datetime.now(RD_TZ)` en estos tres puntos:

- **línea ~33** — footer de `to_mattermost()`, el que dice `(hora RD)`
- **línea ~54** — cabecera de `write_briefing()`
- **línea ~91** — el `.replace("__UPDATED__", ...)` de `write_dashboard()`

El formato de salida (`%d %b %Y %H:%M`) no cambia en ninguno de los tres.

### 3. `tests/test_pipeline.py` — cobertura

Añade asserts que verifiquen que las horas son de RD y no de la máquina. La forma
recomendada es comparar contra el valor esperado calculado desde UTC, para que el test
pase igual en tu Mac que en un runner UTC:

```python
esperado = f"{datetime.now(timezone.utc).astimezone(SAL.RD_TZ):%H}"
```

Cubre al menos el footer de `to_mattermost()`. Los tests siguen siendo **offline**: no
toques la red ni uses `freezegun` ni ninguna dependencia externa.

## Criterios de aceptación

- [ ] `python3 tests/test_pipeline.py` pasa completo
- [ ] `TZ=UTC python3 -c "..."` y `TZ=America/Santo_Domingo python3 -c "..."` producen la
      **misma** hora en el footer de `to_mattermost()` (ver "Cómo verificar")
- [ ] No aparece ningún `datetime.now()` sin argumento en `salida.py`, salvo el excluido abajo
- [ ] No se añadió ninguna dependencia externa ni ningún import de `zoneinfo`

## Fuera de alcance

No toques nada de esto:

- **`salida.py` línea 86, `build_iso`** — usa `datetime.utcnow()` y **debe seguir en UTC con
  el sufijo `Z`**. El JavaScript del dashboard lo parsea para calcular "hace X minutos".
  Cambiarlo rompe esa cuenta.
- **`aerointel.py` línea 43** — el banner de consola. Es cosmético y solo lo ve quien corre
  el pipeline a mano.
- Los `datetime` de `ingesta.py`, `store.py`, `apiexport.py`, `notams.py`, `nas.py` y
  `clima.py` — son timestamps internos en UTC y así deben quedarse.
- No cambies `.github/workflows/update.yml`. La corrección va en el código, no en la
  configuración del entorno: así funciona igual corra donde corra.
- Nada de refactors, renombres ni reordenar imports fuera de lo pedido.

## Cómo verificar

```bash
python3 tests/test_pipeline.py
```

Y la prueba que demuestra el arreglo — las dos líneas deben imprimir la misma hora:

```bash
TZ=UTC python3 -c "import salida; print(salida.to_mattermost(__import__('json').load(open('.agent/fixture.json')))['attachments'][0]['footer'])" 2>/dev/null || echo "usa el evento de prueba que ya exista en tests/"
```

Si no hay un fixture a mano, construye el evento mínimo en un snippet temporal y corre el
mismo snippet con `TZ=UTC` y con `TZ=America/Santo_Domingo`. **Ambas salidas deben coincidir.**
No dejes ese snippet en el repositorio.

Al terminar, escribe `.agent/reporte-activo.md` siguiendo `.agent/TEMPLATE-reporte.md`.
Commitea en `fix/hora-rd`. No hagas push ni merge.
