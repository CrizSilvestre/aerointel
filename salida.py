#!/usr/bin/env python3
# salida.py — Todo lo que el pipeline PUBLICA: el periódico web (dashboard), los avisos a
# Mattermost (breaking + monitor de salud) y el briefing en Markdown.
# QUÉ TOCAR AQUÍ: el formato de los mensajes de Mattermost, qué datos se inyectan al
# dashboard y el texto del aviso de salud. (El HTML/CSS/JS vive en dashboard_template.html.)
import os, json, urllib.request
from datetime import datetime

from config import HERE, OUT
from ingesta import human_age
from analisis import entity_chips

COLOR = {"crítico": "#c00000", "importante": "#e69100", "info": "#1F3864"}
EMOJI = {"crítico": "🔴", "importante": "🟠", "info": "🔵"}
LEVEL = {"crítico": "BREAKING", "importante": "UPDATE", "info": "INFO"}


def to_mattermost(ev):
    a, first = ev["analysis"], ev["items"][0]
    sev = a["severidad"]
    sources = ", ".join(sorted({it["source"] for it in ev["items"]}))
    text = (f"**{a.get('titular') or first['title']}**\n\n*Por qué importa:* {a['angulo_editorial']}\n\n"
            f"**Impacto:** {a['impact_score']}/100 · **Confianza:** {a.get('confianza', '-')} · "
            f"**Categoría:** {a['categoria']}")
    return {"username": "AeroIntel",
            "attachments": [{"color": COLOR.get(sev, "#1F3864"),
                             "title": f"{LEVEL.get(sev, 'INFO')} · {a['categoria'].upper()}",
                             "text": text,
                             "footer": f"Fuentes: {sources} · {len(ev['items'])} fuente(s) · {datetime.now():%d %b %Y %H:%M}",
                             "actions": [{"type": "button", "name": "Ver fuente", "url": first["link"]}]}]}

# ── Monitor de salud: aviso a Mattermost el DÍA que algo falle (fuente caída, NOTAM, LLM
#    degradado), no semanas después. Solo se envía si hay algo que reportar. ──
def health_payload(fails, total_sources, notam_err=None, llm_fallbacks=0, nas_err=None):
    lines = [f"- **{f['name']}**: {f['error']}" for f in fails]
    if notam_err:
        lines.append(f"- **NOTAM (SkyLink)**: {notam_err}")
    if nas_err:
        lines.append(f"- **NAS (FAA)**: {nas_err}")
    if llm_fallbacks:
        lines.append(f"- **LLM**: {llm_fallbacks} evento(s) cayeron a heurística (rate limit/errores persistentes)")
    text = (f"**Monitor de salud de la corrida** · {len(fails)}/{total_sources} fuentes con fallo\n\n"
            + "\n".join(lines)
            + "\n\n_Degradación elegante: el resto del pipeline corrió normal. Revisar si el fallo persiste en corridas siguientes._")
    return {"username": "AeroIntel",
            "attachments": [{"color": "#e69100", "title": "⚠ Salud de fuentes", "text": text}]}


def write_briefing(events):
    lines = ["# Daily Aviation Briefing · AeroIntel", f"_{datetime.now():%d %b %Y %H:%M} · Hub: PUJ_", ""]
    by = {}
    for ev in events:
        by.setdefault(ev["analysis"]["categoria"], []).append(ev)
    for cat, evs in by.items():
        lines.append(f"\n## {cat.upper()}")
        for ev in evs[:6]:
            a, it = ev["analysis"], ev["items"][0]
            lines.append(f"- **[{LEVEL.get(a['severidad'], 'INFO')} · {a['impact_score']}]** {a.get('titular') or it['title']}  ")
            lines.append(f"  {a['angulo_editorial']}  ")
            lines.append(f"  _{it['source']} · {it['link']}_")
    with open(os.path.join(OUT, "briefing.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_dashboard(events, notam_list=None, nas_data=None):
    tpl_path = os.path.join(HERE, "dashboard_template.html")
    if not os.path.exists(tpl_path):
        return
    data = [{"title": ev["analysis"].get("titular") or ev["items"][0]["title"], "link": ev["items"][0]["link"],
             "source": ev["items"][0]["source"], "n": len({i["source"] for i in ev["items"]}),
             "cat": ev["analysis"]["categoria"], "sev": ev["analysis"]["severidad"],
             # puj = mención DIRECTA del hub (badge y vista "Hub PUJ"); el criterio amplio
             # affects_puj (aerolíneas con operación en PUJ) queda para alertas/BD/API.
             "impact": ev["analysis"]["impact_score"], "puj": bool(ev["analysis"].get("puj_direct")),
             "dr": ev["analysis"].get("dr_tier"),   # 'core' = RD · 'regional' = Caribe · null = global
             "why": ev["analysis"]["angulo_editorial"], "resumen": ev["analysis"].get("resumen", ""),
             "fecha": human_age(ev.get("dt")), "img": ev.get("image_url"),
             # iso = publicación en UTC marcada con 'Z' → el navegador calcula la antigüedad EN VIVO
             "iso": (ev["dt"].isoformat() + "Z") if ev.get("dt") else None,
             "ent": entity_chips(ev["analysis"])} for ev in events]
    mm_url = os.environ.get("MATTERMOST_WEB_URL", "https://chatroom.grupopuntacana.com/")
    build_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"   # hora de generación (UTC)
    out = (open(tpl_path, encoding="utf-8").read()
           .replace("/*DATA*/", json.dumps(data, ensure_ascii=False))
           .replace("/*NOTAMS*/", json.dumps(notam_list or [], ensure_ascii=False))
           .replace("/*NAS*/", json.dumps(nas_data or {"updated": None, "events": []}, ensure_ascii=False))
           .replace("__UPDATED__", f"{datetime.now():%d %b %Y %H:%M}")
           .replace("__BUILD_ISO__", build_iso)
           .replace("__MM_URL__", mm_url))
    with open(os.path.join(OUT, "dashboard.html"), "w", encoding="utf-8") as f:
        f.write(out)


def post(hook, payload):
    req = urllib.request.Request(hook, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()
