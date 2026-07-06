#!/usr/bin/env python3
# imagenes.py — Imágenes de las notas: extracción de og:image / thumbnail de Google News,
# filtros anti-placeholder/logo y el boost de score para notas con foto real.
# QUÉ TOCAR AQUÍ: la blocklist de imágenes genéricas y el tamaño del boost.
import re, os, ssl, html, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

# Regex para extraer thumbnail de previsualización de Google News (lh3.googleusercontent.com).
# Estas imágenes son del artículo original, cacheadas/escaladas por Google.
_GNEWS_IMG_RE = re.compile(
    r'(https://lh3\.googleusercontent\.com/[A-Za-z0-9_-]+=[^\s"\'<>]+)',
    re.I
)
# Captura og:image y twitter:image en cualquier orden de atributos (para URLs directas).
_OG_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:image["\']|name=["\']twitter:image["\'])[^>]+content=["\']([^"\' <>]+)["\']'
    r'|<meta[^>]+content=["\']([^"\' <>]+)["\'][^>]+(?:property=["\']og:image["\']|name=["\']twitter:image["\'])',
    re.I | re.S
)
# Imágenes a rechazar siempre: logos/branding de plataformas y thumbnails diminutos (no son
# la foto del artículo). El logo-tarjeta de Google News cae aquí o por el filtro de frecuencia.
GENERIC_IMG_RE = re.compile(
    r'(gstatic\.com|/logo|_logo|sprite|favicon|placeholder|default[-_]?(?:image|thumb)|'
    r'=w(?:16|24|32|48|64)\b|googlelogo|news[-_]?google|if\.not\.exist|not[-_.]?exist|'
    r'no[-_]?image|sin[-_]?imagen)', re.I)


def _fetch_html(url, timeout=10, max_bytes=65536):
    """Descarga hasta max_bytes de una URL y retorna el texto."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read(max_bytes).decode("utf-8", errors="ignore")


def fetch_og_image(url, timeout=10):
    """Extrae imagen de previsualización para un artículo.
    - Google News: extrae el thumbnail cacheado de lh3.googleusercontent.com (escalado a 1200px).
    - URLs directas: extrae og:image / twitter:image del <head> del artículo.
    Retorna la URL de la imagen (str) o None."""
    if not url or not url.startswith("http"):
        return None
    try:
        is_gnews = "news.google.com/" in url
        # Google News embebe la imagen de artículo casi al final del HTML (~600 KB)
        body = _fetch_html(url, timeout=timeout, max_bytes=720000 if is_gnews else 65536)
        if is_gnews:
            # Extraer thumbnail de previsualización de Google News
            imgs = _GNEWS_IMG_RE.findall(body)
            # Filtrar favicons (w16, w24, w32, w48) y quedarnos con la imagen de artículo
            article_imgs = [i for i in imgs if re.search(r'=s\d+-w\d+-rw', i)]
            if article_imgs:
                # Escalar a 1200px de ancho
                return re.sub(r'=s\d+-w\d+-rw', '=s0-w1200-rw', article_imgs[0])
            # Fallback: cualquier imagen grande de googleusercontent
            large = [i for i in imgs if '=w16' not in i and '=w24' not in i and '=w32' not in i and '=w48' not in i]
            if large:
                return large[0]
        # Para URLs directas (o fallback de GNews): buscar og:image
        m = _OG_RE.search(body)
        if m:
            img = html.unescape((m.group(1) or m.group(2) or "").strip())
            if img.startswith("http"):
                return img
    except Exception:
        pass
    return None


def fetch_images_parallel(events, n=20, max_workers=8):
    """Enriquece los top-N eventos con 'image_url' en paralelo.
    Los que no tienen imagen quedan con image_url=None (el dashboard usa fallback de color)."""
    top = events[:n]
    if not top:
        return
    print(f"  Extrayendo imágenes para {len(top)} eventos ({max_workers} workers paralelos)…", end="", flush=True)
    urls = [ev["items"][0]["link"] for ev in top]
    results = {i: None for i in range(len(top))}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch_og_image, url): i for i, url in enumerate(urls)}
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                results[idx] = fut.result()
            except Exception:
                pass
    # Filtro de calidad de imagen: una foto de artículo REAL es única. Un placeholder/logo
    # (p. ej. la tarjeta genérica de Google News) se repite entre muchos artículos → se rechaza.
    counts = Counter(u for u in results.values() if u)
    kept = 0
    for i, ev in enumerate(top):
        u = results.get(i)
        if u and (counts[u] > 1 or GENERIC_IMG_RE.search(u)):
            u = None                                  # placeholder/logo compartido → ficha de categoría
        if u:
            kept += 1
        ev["image_url"] = u
    print(f" {kept}/{len(top)} con imagen real y única (resto → ficha de inteligencia).")

# Boost visual: una nota con foto real sube un poco en el ranking (portada más atractiva) sin
# alterar el fondo del modelo. Se aplica DESPUÉS del umbral de publicación: solo reordena.
IMG_BOOST = int(os.environ.get("AEROINTEL_IMG_BOOST", "4"))


def apply_image_boost(events, boost=None):
    boost = IMG_BOOST if boost is None else boost
    if boost <= 0:
        return
    for ev in events:
        if ev.get("image_url"):
            a = ev["analysis"]
            a["impact_score"] = min(100, a["impact_score"] + boost)
            if isinstance(a.get("score_breakdown"), dict):
                a["score_breakdown"]["img"] = boost
    events.sort(key=lambda e: e["analysis"]["impact_score"], reverse=True)
