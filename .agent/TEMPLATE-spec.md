# SPEC NNN — <título corto>

**Rama:** `feat/<nombre>`
**Escrito por:** Claude Code · <fecha>
**Ejecuta:** Antigravity

## Objetivo

Una o dos frases. Qué debe ser cierto cuando esto termine.

## Contexto

Por qué se hace, y qué comportamiento actual se está cambiando. Incluye el dato concreto
que motivó el cambio (un bug observado, una petición, una métrica) — no generalidades.

## Archivos a tocar

| Archivo | Qué cambia |
|---|---|
| `x.py` | ... |

Si necesitas tocar un archivo que no está en esta lista, **detente y repórtalo**.

## Cambios

### 1. `<archivo>` — `<función>`

Descripción precisa. Incluye firma exacta si es función nueva, y el comportamiento
esperado en los casos límite.

```python
# forma esperada, si aplica
```

### 2. ...

## Criterios de aceptación

- [ ] `python3 tests/test_pipeline.py` pasa completo
- [ ] <condición observable y verificable>
- [ ] <condición observable y verificable>

## Fuera de alcance

Lo que explícitamente NO se toca en esta tarea. Sé específico: esta sección es la que evita
que el diff se infle.

## Cómo verificar

Comandos exactos a correr y qué debe salir.

```bash
python3 tests/test_pipeline.py
python3 aerointel.py   # observar: ...
```
