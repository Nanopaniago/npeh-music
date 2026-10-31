"""Pesquisa de músicas, álbuns e artistas na API pública do Deezer (grátis, sem chave).

Dá-nos capas e uma prévia de 30 s por faixa. O download em si continua a ser feito pelo spotDL
a partir do texto "Artista - Título".
"""
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request

from .db import get_conn

API = "https://api.deezer.com"
_cache = {}


def _get(path, params=None, ttl=300):
    qs = urllib.parse.urlencode(params or {})
    url = f"{API}{path}" + (f"?{qs}" if qs else "")
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "Pobrefy/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise RuntimeError(err.get("message", "erro do Deezer") if isinstance(err, dict) else str(err))
    if len(_cache) > 300:
        _cache.clear()
    _cache[url] = (time.time(), data)
    return data


# ---------------------------------------------------------------- normalização
def _track(t, album=None):
    alb = t.get("album") or album or {}
    art = t.get("artist") or {}
    return {
        "id": t.get("id"),
        "title": t.get("title") or t.get("title_short") or "",
        "artist": art.get("name", ""),
        "artist_id": art.get("id"),
        "album": alb.get("title", ""),
        "album_id": alb.get("id"),
        "cover": alb.get("cover_medium") or alb.get("cover_big") or alb.get("cover"),
        "duration": t.get("duration"),
        "preview": t.get("preview") or None,
        "explicit": bool(t.get("explicit_lyrics")),
    }


def _album(a):
    art = a.get("artist") or {}
    return {
        "id": a.get("id"),
        "title": a.get("title", ""),
        "artist": art.get("name", ""),
        "artist_id": art.get("id"),
        "cover": a.get("cover_big") or a.get("cover_medium") or a.get("cover"),
        "year": (a.get("release_date") or "")[:4],
        "tracks": a.get("nb_tracks"),
        "kind": a.get("record_type"),
        "explicit": bool(a.get("explicit_lyrics")),
    }


def _artist(a):
    return {
        "id": a.get("id"),
        "name": a.get("name", ""),
        "picture": a.get("picture_big") or a.get("picture_medium") or a.get("picture"),
        "fans": a.get("nb_fan"),
    }


# ---------------------------------------------------------------- "já está na biblioteca?"
def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
    s = re.split(r"\s-\s", s)[0]
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def _library_index():
    idx = {}
    for r in get_conn().execute("SELECT title, artist FROM tracks"):
        nt = _norm(r["title"])
        if nt:
            idx.setdefault(nt, []).append(_norm(r["artist"]))
    return idx


def mark_library(tracks):
    idx = _library_index()
    for t in tracks:
        nt, na = _norm(t["title"]), _norm(t["artist"])
        t["in_library"] = bool(nt and na and any(na in a or a in na for a in idx.get(nt, [])))
    return tracks


# ---------------------------------------------------------------- API usada pelo servidor
def search(q, kind="track", limit=24):
    data = _get(f"/search/{kind}", {"q": q, "limit": limit})["data"]
    if kind == "track":
        return mark_library([_track(t) for t in data if t.get("id")])
    if kind == "album":
        return [_album(a) for a in data if a.get("id")]
    return [_artist(a) for a in data if a.get("id")]


def album_detail(aid):
    a = _get(f"/album/{int(aid)}")
    out = _album(a)
    items = _get(f"/album/{int(aid)}/tracks", {"limit": 100})["data"]
    out["tracks_list"] = mark_library([_track(t, album=a) for t in items if t.get("id")])
    return out


def artist_detail(aid):
    a = _get(f"/artist/{int(aid)}")
    top = _get(f"/artist/{int(aid)}/top", {"limit": 20})["data"]
    albums = _get(f"/artist/{int(aid)}/albums", {"limit": 60})["data"]
    seen, uniq = set(), []
    for al in albums:
        key = _norm(al.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(_album(al))
    return {**_artist(a), "top": mark_library([_track(t) for t in top if t.get("id")]), "albums": uniq}
