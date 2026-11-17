"""Top 100 Global: junta os três rankings mundiais mais conhecidos e públicos.

  • Spotify — playlist oficial "Top 50 - Global" (página pública de incorporação)
  • Apple Music — playlist oficial "Top 100: Global" (página pública da Apple Music)
  • Deezer — "Top Mundial" (API pública /chart/0/tracks)

Cada música ganha pontos pela posição em cada ranking (1.º lugar = 1 ponto, último ≈ 0) e um bónus por
aparecer em mais de uma plataforma. O resultado é ordenado e cortado nas 100 primeiras. Cada entrada é
ligada a uma faixa do Deezer (capa, prévia de 30 s, download) e marcada se já está na biblioteca.
Atualiza a cada 6 horas; se uma fonte falhar, as outras continuam.
"""
import json
import logging
import re
import threading
import time
import urllib.request

from . import dedupe, discover
from .config import DATA_DIR
from .db import get_conn

log = logging.getLogger("charts")
CACHE = DATA_DIR / "charts-global.json"
TTL = 6 * 3600
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.8"}
SOURCES = {
    "spotify": {"name": "Spotify · Top 50 Global", "url": "https://open.spotify.com/playlist/37i9dQZEVXbMDoHDwVN2tF"},
    "apple": {"name": "Apple Music · Top 100: Global", "url": "https://music.apple.com/us/playlist/top-100-global/pl.d25f5d1181894928af76c85c967f8f31"},
    "deezer": {"name": "Deezer · Top Mundial", "url": "https://www.deezer.com/playlist/3155776842"},
}
_lock = threading.Lock()
_mem = {"data": None}


def _html(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def fetch_spotify():
    h = _html("https://open.spotify.com/embed/playlist/37i9dQZEVXbMDoHDwVN2tF")
    ent = json.loads(re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', h, re.S).group(1))
    ent = ent["props"]["pageProps"]["state"]["data"]["entity"]
    return [{"title": t["title"], "artist": t.get("subtitle", ""), "duration": round((t.get("duration") or 0) / 1000) or None}
            for t in ent.get("trackList") or [] if t.get("title")]


def fetch_apple():
    h = _html(SOURCES["apple"]["url"])
    data = json.loads(re.search(r'<script type="application/json" id="serialized-server-data">(.*?)</script>', h, re.S).group(1))
    out = []

    def walk(x):
        if isinstance(x, dict):
            if (x.get("contentDescriptor") or {}).get("kind") == "song" and x.get("title"):
                out.append({"title": x["title"], "artist": x.get("artistName") or "",
                            "duration": round((x.get("duration") or 0) / 1000) or None})
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return out[:100]


def fetch_deezer():
    data = discover._get("/chart/0/tracks", {"limit": 100}, ttl=1800).get("data") or []
    return [{"title": t.get("title", ""), "artist": (t.get("artist") or {}).get("name", ""),
             "duration": t.get("duration"), "_dz": t} for t in data]


def _key(artist, title):
    # "Tate McRae" vs "Tate McRae & X"; "Song (feat. Y)" vs "Song"
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "")
    t = re.sub(r"\s+-\s+.*$", "", t)
    return dedupe.key_for(artist, t)


def build():
    lists, errors = {}, {}
    for name, fn in (("spotify", fetch_spotify), ("apple", fetch_apple), ("deezer", fetch_deezer)):
        try:
            lists[name] = fn()
        except Exception as e:  # noqa
            errors[name] = str(e)[:200]
            log.warning("Top global: %s falhou (%s)", name, e)
    if not lists:
        raise RuntimeError("nenhuma fonte respondeu")
    merged = {}
    for src, items in lists.items():
        n = len(items)
        for pos, it in enumerate(items, 1):
            k = _key(it["artist"], it["title"])
            if k.startswith("|") or k.endswith("|"):
                continue
            e = merged.setdefault(k, {"title": it["title"], "artist": it["artist"], "duration": it["duration"],
                                      "sources": {}, "score": 0.0, "_dz": None})
            if src in e["sources"]:
                continue
            e["sources"][src] = pos
            e["score"] += 1 - (pos - 1) / max(n, 1)
            if it.get("_dz"):
                e["_dz"] = it["_dz"]
            if src == "apple":              # títulos da Apple vêm mais limpos
                e["title"], e["artist"] = it["title"], it["artist"]
    for e in merged.values():
        e["score"] += 0.35 * (len(e["sources"]) - 1)        # consenso entre plataformas pesa
    top = sorted(merged.values(), key=lambda e: (-e["score"], min(e["sources"].values())))[:100]

    # ligar cada entrada a uma faixa do Deezer (capa, prévia, download)
    out = []
    for rank, e in enumerate(top, 1):
        dz = e.pop("_dz")
        if not dz:
            try:
                who = dedupe.primary_artist(e["artist"]) or e["artist"]
                what = re.sub(r"[\(\[].*", "", e["title"]).strip()
                q = f'artist:"{who}" track:"{what}"'
                res = discover._get("/search", {"q": q, "limit": 5}, ttl=86400).get("data") or []
                want = _key(e["artist"], e["title"])
                dz = next((t for t in res if _key((t.get("artist") or {}).get("name", ""), t.get("title", "")) == want), res[0] if res else None)
                time.sleep(0.12)
            except Exception:  # noqa
                dz = None
        t = discover._track(dz) if dz else {"id": None, "title": e["title"], "artist": e["artist"], "album": "",
                                            "cover": None, "duration": e["duration"], "preview": None, "explicit": False}
        t.update(rank=rank, sources=e["sources"], score=round(e["score"], 3), chart_title=e["title"], chart_artist=e["artist"])
        out.append(t)
    data = {"updated": int(time.time()), "sources": {k: {**SOURCES[k], "count": len(v)} for k, v in lists.items()},
            "errors": errors, "tracks": out}
    CACHE.write_text(json.dumps(data, ensure_ascii=False))
    _mem["data"] = data
    log.info("Top 100 Global atualizado (%s)", ", ".join(f"{k}: {len(v)}" for k, v in lists.items()))
    return data


def _lib_ids(tracks):
    """Liga cada entrada à faixa da biblioteca (para tocar já)."""
    idx = {}
    for r in get_conn().execute("SELECT id, title, artist, duration FROM tracks"):
        idx.setdefault(_key(r["artist"], r["title"]), []).append((r["id"], r["duration"]))
    pend = dedupe.pending_keys()
    for t in tracks:
        hits = idx.get(_key(t["artist"], t["title"])) or idx.get(_key(t.get("chart_artist", ""), t.get("chart_title", ""))) or []
        lib = next((i for i, d in hits if dedupe._close(t.get("duration"), d)), None)
        t["library_id"] = lib
        t["in_library"] = lib is not None
        t["pending"] = not t["in_library"] and dedupe.key_for(t["artist"], t["title"]) in pend
    return tracks


def get(force=False):
    data = _mem["data"]
    if data is None and CACHE.exists():
        try:
            data = _mem["data"] = json.loads(CACHE.read_text())
        except Exception:  # noqa
            data = None
    stale = not data or time.time() - data.get("updated", 0) > TTL
    if force or not data:
        with _lock:
            data = build()
    elif stale and _lock.acquire(blocking=False):
        def bg():
            try:
                build()
            except Exception:  # noqa
                log.exception("top global")
            finally:
                _lock.release()
        threading.Thread(target=bg, daemon=True).start()
    data = json.loads(json.dumps(data))
    _lib_ids(data["tracks"])
    return data
