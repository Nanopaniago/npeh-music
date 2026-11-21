"""Organização da biblioteca: género (em grandes famílias) e data de lançamento de cada música.

1. As etiquetas do ficheiro (o spotDL grava os géneros do artista no Spotify e a data do álbum).
2. O que faltar é completado em segundo plano com a API pública do Deezer: género do álbum e data
   de lançamento. Cada faixa é consultada uma vez só (coluna `enriched`).
"""
import logging
import re
import threading
import time

from . import dedupe, discover, load
from .db import get_conn

log = logging.getLogger("catalog")

# (palavras-chave, nome da família). A ordem conta: o mais específico primeiro.
FAMILIES = [
    (("k-pop", "kpop", "k pop", "j-pop", "asian", "korean", "japanese", "c-pop", "anime"), "K-Pop & Ásia"),
    (("sertanejo", "mpb", "samba", "pagode", "forró", "forro", "axé", "axe", "funk brasileiro", "funk carioca",
      "brazil", "brasil", "bossa", "piseiro", "arrocha", "funk mandelão", "baile funk", "brega"), "Música Brasileira"),
    (("kizomba", "kuduro", "afro", "african", "amapiano", "afrobeat", "semba", "zouk"), "Afro"),
    (("reggaeton", "latin", "salsa", "bachata", "cumbia", "urbano latino", "dembow", "merengue", "mexican", "corrido"), "Latina"),
    (("drill", "trap", "rap", "hip hop", "hip-hop", "grime"), "Hip-Hop & Rap"),
    (("drum and bass", "dnb", "drum & bass", "house", "techno", "trance", "edm", "electro", "dance", "dubstep",
      "electronic", "eletrónica", "eletronica", "big room", "garage", "hardstyle", "disco"), "Eletrónica & Dance"),
    (("metal", "metalcore", "hardcore"), "Metal"),
    (("indie", "alternative", "alternativa", "shoegaze", "dream pop"), "Indie & Alternativa"),
    (("rock", "punk", "grunge", "emo"), "Rock"),
    (("r&b", "rnb", "soul", "funk", "neo soul", "motown"), "R&B & Soul"),
    (("reggae", "dancehall", "ska", "dub"), "Reggae"),
    (("jazz", "blues", "swing"), "Jazz & Blues"),
    (("classical", "clássica", "classica", "orchestra", "piano", "opera", "baroque"), "Clássica"),
    (("soundtrack", "films", "games", "movie", "score", "musical", "banda sonora"), "Bandas sonoras"),
    (("gospel", "christian", "worship", "cristã", "cristao", "louvor"), "Gospel"),
    (("country", "folk", "americana", "bluegrass", "fado"), "Folk, Country & Fado"),
    (("chill", "lounge", "ambient", "lo-fi", "lofi", "sleep", "relax", "new age"), "Chill"),
    (("kids", "children", "infantil"), "Infantil"),
    (("pop",), "Pop"),
]
OTHER = "Outros"
FAMILY_NAMES = [n for _, n in FAMILIES] + [OTHER]


def family(genre_text):
    """'dance pop; pop' -> 'Eletrónica & Dance' (o primeiro género que encaixa numa família)."""
    if not genre_text:
        return None
    parts = [p.strip().lower() for p in re.split(r"[;,/|]", str(genre_text)) if p.strip()]
    for part in parts:
        for keys, name in FAMILIES:
            if any(k in part for k in keys):
                return name
    return None


def norm_date(value):
    """'2024-05-10T00:00:00' / '2024' / '2024-05' -> '2024-05-10' / '2024' / '2024-05'."""
    m = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", str(value or "").strip())
    if not m or not (1900 <= int(m.group(1)) <= 2100):
        return None
    return "-".join(g for g in m.groups() if g)


# ---------------------------------------------------------------- Deezer
_album_cache = {}


def _best_match(artist, title, duration):
    main_artist = re.split(r",|;| feat\.? | ft\.? | & ", artist, flags=re.I)[0].strip()
    clean_title = re.sub(r"\s*[\(\[].*?(feat|ft\.|with ).*?[\)\]]", "", title, flags=re.I).strip()
    data = discover._get("/search", {"q": f'artist:"{main_artist}" track:"{clean_title}"', "limit": 10}, ttl=3600).get("data") or []
    if not data:
        data = discover._get("/search", {"q": f"{main_artist} {clean_title}", "limit": 10}, ttl=3600).get("data") or []
    want = dedupe.key_for(main_artist, clean_title)
    best = None
    for t in data:
        if not t.get("album"):
            continue
        same = dedupe.key_for((t.get("artist") or {}).get("name", ""), t.get("title", "")) == want
        close = not duration or not t.get("duration") or abs(t["duration"] - duration) <= 12
        score = (same, close)
        if best is None or score > best[0]:
            best = (score, t)
    if not best or not (best[0][0] or best[0][1]):
        return None
    return best[1]


def lookup_duration(artist, title):
    """Duração da música no Deezer (para confirmar que um download não ficou cortado)."""
    t = _best_match(artist, title, None)
    if t and dedupe.key_for(re.split(r",|;| feat\.? ", artist, flags=re.I)[0].strip(), title) == dedupe.key_for(
            (t.get("artist") or {}).get("name", ""), t.get("title", "")):
        return t.get("duration") or None
    return None


def _deezer_lookup(artist, title, duration):
    """-> (deezer_album_id, genre_names, release_date) para a faixa mais parecida."""
    t = _best_match(artist, title, duration)
    if not t:
        return None, [], None
    alb_id = t["album"]["id"]
    if alb_id not in _album_cache:
        a = discover._get(f"/album/{int(alb_id)}", ttl=86400)
        genres = [g.get("name", "") for g in ((a.get("genres") or {}).get("data") or []) if g.get("name")]
        _album_cache[alb_id] = (genres, norm_date(a.get("release_date")))
        if len(_album_cache) > 2000:
            _album_cache.clear()
    genres, date = _album_cache.get(alb_id, ([], None))
    return alb_id, genres, date


_lock = threading.Lock()
state = {"running": False, "done": 0, "pending": 0}


def refresh_albums(conn=None):
    """Data de lançamento de cada álbum = a mais recente das suas faixas."""
    conn = conn or get_conn()
    conn.execute("""UPDATE albums SET release_date = (SELECT MAX(release_date) FROM tracks t WHERE t.album_id=albums.id)
                    WHERE EXISTS (SELECT 1 FROM tracks t WHERE t.album_id=albums.id AND t.release_date IS NOT NULL)""")
    conn.execute("""UPDATE albums SET release_date = year
                    WHERE release_date IS NULL AND year IS NOT NULL""")
    conn.commit()


def enrich(limit=400, pause=0.15):
    """Completa género e data das faixas ainda não consultadas. Corre numa thread em segundo plano."""
    if not _lock.acquire(blocking=False):
        return 0
    conn = get_conn()
    n = 0
    try:
        todo = conn.execute("""SELECT id, title, artist, duration, genre, genre_group, release_date
                               FROM tracks WHERE COALESCE(enriched,0)=0 ORDER BY id LIMIT ?""", (limit,)).fetchall()
        state.update(running=True, done=0, pending=len(todo))
        for t in todo:
            try:
                _, genres, date = _deezer_lookup(t["artist"], t["title"], t["duration"])
            except Exception as e:  # noqa — sem rede / limite do Deezer: tenta de novo mais tarde
                log.info("Deezer indisponível (%s); a enriquecer mais tarde", e)
                break
            group = t["genre_group"] or family("; ".join(genres)) or (OTHER if genres or t["genre"] else None)
            rel = t["release_date"]
            if date and (not rel or (len(rel) < 10 and date.startswith(rel[:4]))):
                rel = date              # a etiqueta só tinha o ano (ou nada): usa a data completa do Deezer
            conn.execute("UPDATE tracks SET genre_group=?, release_date=?, enriched=1, "
                         "genre=CASE WHEN COALESCE(genre,'')='' THEN ? ELSE genre END WHERE id=?",
                         (group or OTHER, rel, "; ".join(genres)[:200], t["id"]))
            n += 1
            state["done"] = n
            if n % 20 == 0:
                conn.commit()
            time.sleep(pause)
            if load.busy():                # prioridade 3: grava o que tem e espera que os downloads acabem
                conn.commit()
                load.wait_idle()
        conn.commit()
        refresh_albums(conn)
    finally:
        state["running"] = False
        _lock.release()
    if n:
        log.info("Catálogo: %s faixas organizadas", n)
    return n


def enrich_all():
    while enrich() >= 400:
        pass


def start_background():
    threading.Thread(target=enrich_all, daemon=True, name="catalog").start()


# ---------------------------------------------------------------- consultas para a interface
def genres_summary(conn=None):
    conn = conn or get_conn()
    out = []
    for r in conn.execute("""SELECT COALESCE(genre_group, ?) AS g, COUNT(*) AS n, COUNT(DISTINCT artist_id) AS artists
                             FROM tracks GROUP BY g ORDER BY n DESC""", (OTHER,)).fetchall():
        covers = [x[0] for x in conn.execute(
            """SELECT al.id FROM tracks t JOIN albums al ON al.id=t.album_id
               WHERE COALESCE(t.genre_group, ?)=? AND al.cover IS NOT NULL
               GROUP BY al.id ORDER BY MAX(COALESCE(t.release_date,'')) DESC LIMIT 4""", (OTHER, r["g"])).fetchall()]
        out.append({"name": r["g"], "tracks": r["n"], "artists": r["artists"], "covers": covers})
    # "Outros" sempre no fim
    out.sort(key=lambda x: (x["name"] == OTHER, -x["tracks"]))
    return out
