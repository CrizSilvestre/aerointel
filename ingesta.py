#!/usr/bin/env python3
# ingesta.py — Entrada de noticias: fuentes (sources.json), descarga de feeds RSS/Atom y
# Google News, parseo, fechas/antigüedad, limpieza de titulares y dedup/cluster.
# QUÉ TOCAR AQUÍ: cómo se leen los feeds, la ventana de recencia y la limpieza de titulares.
# (Las fuentes en sí se editan en sources.json, no aquí.)
import re, os, ssl, html, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from difflib import SequenceMatcher
import xml.etree.ElementTree as ET

from config import FEED_UA, load_json

MAX_PER_SOURCE = 25
WHEN = os.environ.get("AEROINTEL_WHEN", "7d")                    # ventana de Google News (recencia)
MAX_AGE_H = float(os.environ.get("AEROINTEL_MAX_AGE_H", "168"))  # descarta noticias más viejas (168h = 7 días)

SOURCES = load_json("sources.json", {"sources": []})["sources"]


def gnews_url(s):
    q = urllib.parse.quote(f"{s['query']} when:{WHEN}")   # restringe a noticias recientes
    lang, gl = s.get("lang", "en-US"), s.get("gl", "US")
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={gl}&ceid={gl}:{lang.split('-')[0]}"


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": FEED_UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    try:
        return urllib.request.urlopen(req, timeout=timeout).read()
    except (ssl.SSLError, urllib.error.URLError):
        # respaldo solo-lectura para feeds públicos (certs de macOS); es un demo local
        ctx = ssl._create_unverified_context()
        return urllib.request.urlopen(req, timeout=timeout, context=ctx).read()


def localname(tag):
    return tag.split('}')[-1].lower()


def clean(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()

# Limpieza de titulares crudos de Google News: quita el sufijo " - Publicación" y blobs de
# tracking "(AbC123…)". Se aplica cuando el LLM no reescribió (fallback heurístico/429).
_SRC_SUFFIX_RE = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,45}$")
_TRACK_RE = re.compile(r"\s*\((?:[A-Za-z0-9_]{6,}|[A-Za-z0-9]{1,3}\d[A-Za-z0-9]{4,})\)\s*")


def clean_title(t):
    t = (t or "").strip()
    t = _TRACK_RE.sub(" ", t)
    t = _SRC_SUFFIX_RE.sub("", t).strip()
    t = re.sub(r"\s+", " ", t)
    return (t[:117] + "…") if len(t) > 120 else t


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt); break
            except Exception:
                continue
    if dt and dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def age_hours(dt):
    return None if dt is None else (datetime.utcnow() - dt).total_seconds() / 3600


def human_age(dt):
    a = age_hours(dt)
    if a is None:
        return "fecha s/d"
    if a < 1:
        return f"hace {max(1, int(a * 60))} min"
    if a < 24:
        return f"hace {int(a)} h"
    return dt.strftime("%d %b %H:%M")


def parse_feed(xml_bytes, source_name):
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return out
    for el in root.iter():
        if localname(el.tag) not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "desc": "", "pub": ""}
        for ch in el:
            lt = localname(ch.tag)
            if lt == "title":
                d["title"] = clean(ch.text)
            elif lt == "link" and not d["link"]:
                d["link"] = (ch.get("href") or ch.text or "").strip()
            elif lt in ("description", "summary", "content") and not d["desc"]:
                d["desc"] = clean(ch.text)
            elif lt in ("pubdate", "published", "updated", "date") and not d["pub"]:
                d["pub"] = (ch.text or "").strip()
        if d["title"] and d["link"]:
            d["source"] = source_name
            d["dt"] = parse_date(d["pub"])
            out.append(d)
    return out[:MAX_PER_SOURCE]


# ───────────────────────── dedup / cluster ─────────────────────────
def canonical(url):
    return re.sub(r"[?#].*$", "", url or "").rstrip("/").lower()


def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower())


def cluster(items):
    events = []
    for it in items:
        nt, cu = norm_title(it["title"]), canonical(it["link"])
        for ev in events:
            if cu == ev["cu"] or SequenceMatcher(None, nt, ev["nt"]).ratio() >= 0.62:
                ev["items"].append(it)
                break
        else:
            events.append({"nt": nt, "cu": cu, "items": [it]})
    return events
