# .agent/ — canal de handoff Claude Code ↔ Antigravity

No existe puente API entre los dos agentes. El canal es esta carpeta más git.
Ambos corren en el mismo Mac sobre el mismo repositorio, así que el traspaso es inmediato.

## Ciclo

```
Claude Code                          Antigravity
-----------                          -----------
escribe .agent/spec-activo.md   ──▶  lee el spec
                                     rama nueva, implementa, tests
revisa git diff   ◀──                escribe .agent/reporte-activo.md
                                     commit en la rama (sin push)
aprueba o devuelve correcciones
merge a main
```

## Archivos

| Archivo | Lo escribe | Contenido |
|---|---|---|
| `spec-activo.md` | Claude Code | La tarea a implementar. Uno a la vez. |
| `reporte-activo.md` | Antigravity | Qué hizo, qué falló, qué objeta. |
| `TEMPLATE-spec.md` | — | Plantilla del spec. |
| `TEMPLATE-reporte.md` | — | Plantilla del reporte. |
| `historial/` | Claude Code | Specs y reportes cerrados, por si hay que auditar. |

## Reglas del canal

- **Un spec activo a la vez.** Al cerrarse, ambos archivos se mueven a `historial/NNN-nombre/`.
- **Antigravity nunca edita el spec.** Si algo está mal, lo dice en el reporte.
- **Claude Code nunca lee los archivos completos que tocó Antigravity**, solo `git diff`.
  Ahí está el ahorro de tokens: revisar 80 líneas de diff en vez de 900 de fuente.
- **Nada llega a `main` sin revisión.** Antigravity commitea en su rama y para.

## Por qué este reparto ahorra tokens

Planificar es barato. Lo caro es ejecutar: leer archivos, iterar, correr tests, releer,
corregir. Ese ciclo lo absorbe Antigravity con su propia cuota.

El ahorro se evapora si el spec es vago — entonces Antigravity improvisa y corregirlo cuesta
más que haberlo hecho directo. Un spec sirve cuando alguien que no estuvo en la conversación
puede ejecutarlo sin preguntar nada.
