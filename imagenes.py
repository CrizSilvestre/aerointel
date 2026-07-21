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


def _feed_image_for(ev):
    """Imagen que YA declara el feed para este evento (cluster): prefiere un ítem de FUENTE
    DIRECTA (no Google News, cuyo RSS no trae la foto real). Devuelve '' si ningún ítem la trae."""
    items = sorted(ev["items"], key=lambda it: "news.google.com" in (it.get("link") or ""))
    for it in items:
        img = it.get("image") or ""
        if img and img.startswith("http") and not GENERIC_IMG_RE.search(img):
            return img
    return ""


def fetch_images_parallel(events, n=20, max_workers=8):
    """Enriquece los eventos con 'image_url'. La imagen del feed RSS es GRATIS (viene en el XML),
    así que se asigna a TODOS los eventos; el og:image (que abre el artículo) solo se intenta en
    los top-N que aún no tienen foto (típicamente los de Google News, cuyo RSS no la trae)."""
    if not events:
        return
    # 1) imagen del feed para TODOS los eventos (sin red): la mayoría de fuentes directas la incluyen
    from_feed = 0
    for ev in events:
        img = _feed_image_for(ev)
        ev["image_url"] = img or None
        if img:
            from_feed += 1
    # 2) og:image solo para los top-N que siguen sin imagen (abre el artículo → costoso)
    need_fetch = [ev for ev in events[:n] if not ev.get("image_url")]
    print(f"  Imágenes: {from_feed} del feed · extrayendo {len(need_fetch)} restantes ({max_workers} workers)…",
          end="", flush=True)
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch_og_image, ev["items"][0]["link"]): ev for ev in need_fetch}
        for fut in as_completed(futs):
            try:
                results[id(futs[fut])] = fut.result()
            except Exception:
                pass
    # Filtro de calidad SOLO sobre las extraídas por og:image: una foto real es única; el
    # placeholder/tarjeta genérica de Google News se repite entre muchos artículos → se rechaza.
    fetched = [results.get(id(ev)) for ev in need_fetch]
    counts = Counter(u for u in fetched if u)
    for ev in need_fetch:
        u = results.get(id(ev))
        if u and (counts[u] > 1 or GENERIC_IMG_RE.search(u)):
            u = None
        ev["image_url"] = u
    kept = sum(1 for ev in events if ev.get("image_url"))
    print(f" {kept}/{len(events)} con imagen real y única (resto → ficha de inteligencia).")

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
