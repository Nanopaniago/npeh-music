"""Pesquisa de músicas, álbuns e artistas na API pública do Deezer (grátis, sem chave).

Dá-nos capas e uma prévia de 30 s por faixa. O download em si continua a ser feito pelo spotDL
a partir do texto "Artista - Título".
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import dedupe
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
    # o DNS do router falha de vez em quando: 3 tentativas antes de desistir
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(0.6 * (attempt + 1))
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise RuntimeError(err.get("message", "erro do Deezer") if isinstance(err, dict) else str(err))
    if len(_cache) > 300:
        _cache.clear()
    _cache[url] = (time.time(), data)
    return data


def itunes_match(artist, title, duration=None):
    """Plano B para capa + prévia de 30 s: a pesquisa pública do iTunes (quando o Deezer não tem a faixa)."""
    term = urllib.parse.urlencode({"term": f"{artist} {title}", "entity": "song", "limit": 8, "country": "PT"})
    req = urllib.request.Request(f"https://itunes.apple.com/search?{term}", headers={"User-Agent": "Pobrefy/1.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        res = json.load(r).get("results") or []
    want, who = dedupe.key_for(artist, title), dedupe.primary_artist(artist)
    best = None
    for x in res:
        if not x.get("previewUrl"):
            continue
        dur = round((x.get("trackTimeMillis") or 0) / 1000) or None
        if dedupe.key_for(x.get("artistName", ""), x.get("trackName", "")) == want and dedupe._close(duration, dur):
            best = x
            break
        if best is None and who and dedupe.primary_artist(x.get("artistName", "")) == who \
                and dedupe.norm(title)[:12] in dedupe.norm(x.get("trackName", "")):
            best = x
    if not best:
        return None
    art = (best.get("artworkUrl100") or "").replace("100x100bb", "300x300bb")
    return {"id": None, "title": best.get("trackName") or title, "artist": best.get("artistName") or artist,
            "album": best.get("collectionName") or "", "cover": art or None,
            "duration": round((best.get("trackTimeMillis") or 0) / 1000) or duration, "preview": best["previewUrl"],
            "explicit": best.get("trackExplicitness") == "explicit", "source": "itunes"}


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


# ---------------------------------------------------------------- "já está na biblioteca / na fila?"
def mark_library(tracks):
    lib, pend = dedupe.library_index(), dedupe.pending_keys()
    for t in tracks:
        st = dedupe.status(t["title"], t["artist"], t.get("duration"), lib, pend)
        t["in_library"] = st == "library"
        t["pending"] = st == "pending"
    return tracks


def _playlist(p):
    return {
        "id": p.get("id"),
        "title": p.get("title", ""),
        "cover": p.get("picture_big") or p.get("picture_medium") or p.get("picture"),
        "owner": (p.get("user") or p.get("creator") or {}).get("name", ""),
        "tracks": p.get("nb_tracks"),
        "url": f"https://www.deezer.com/playlist/{p.get('id')}",
    }


# ---------------------------------------------------------------- API usada pelo servidor
def search(q, kind="track", limit=24):
    data = _get(f"/search/{kind}", {"q": q, "limit": limit})["data"]
    if kind == "track":
        return mark_library([_track(t) for t in data if t.get("id")])
    if kind == "album":
        return [_album(a) for a in data if a.get("id")]
    if kind == "playlist":
        return [_playlist(p) for p in data if p.get("id")]
    return [_artist(a) for a in data if a.get("id")]


def playlist_detail(pid):
    p = _get(f"/playlist/{int(pid)}")
    out = _playlist(p)
    items = (p.get("tracks") or {}).get("data") or []
    out["description"] = p.get("description") or ""
    out["tracks_list"] = mark_library([_track(t) for t in items if t.get("id") and t.get("readable", True)])
    return out


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
        key = dedupe.norm(al.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(_album(al))
    return {**_artist(a), "top": mark_library([_track(t) for t in top if t.get("id")]), "albums": uniq}


def latest_releases(name, limit=10):
    """Procura o artista pelo nome e devolve os lançamentos dele, do mais recente para o mais antigo."""
    found = _get("/search/artist", {"q": name, "limit": 5}, ttl=3600)["data"]
    want = dedupe.norm(name)
    art = next((x for x in found if dedupe.norm(x.get("name", "")) == want), None)
    if not art:
        return {"artist": None, "releases": []}
    albums = _get(f"/artist/{int(art['id'])}/albums", {"limit": 100}, ttl=3600)["data"]
    albums.sort(key=lambda al: al.get("release_date") or "", reverse=True)
    lib = {dedupe.norm(r[0]) for r in get_conn().execute(
        "SELECT al.title FROM albums al JOIN artists ar ON ar.id=al.artist_id WHERE ar.name=? COLLATE NOCASE", (name,))}
    seen, out = set(), []
    for al in albums:
        key = dedupe.norm(al.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        item = _album(al)
        item["release_date"] = al.get("release_date")
        item["in_library"] = key in lib
        out.append(item)
        if len(out) >= limit:
            break
    return {"artist": _artist(art), "releases": out}
