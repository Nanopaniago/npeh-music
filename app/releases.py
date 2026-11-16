"""Novos lançamentos dos artistas que já estão na biblioteca.

De X em X horas o servidor pergunta ao Deezer pelos últimos álbuns/singles de cada artista da biblioteca.
Os lançamentos recentes (últimos 45 dias) que ainda não estão na biblioteca aparecem no início do nPlay
("Novo lançamento de Supercombo — Ouvir agora"). Opcionalmente, são descarregados sozinhos.
"""
import datetime as dt
import logging
import threading
import time

from . import dedupe, discover, downloader
from . import settings as app_settings
from .db import get_conn

log = logging.getLogger("releases")

RECENT_DAYS = 45
SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,               -- id do álbum no Deezer
    artist_id INTEGER,                    -- artista na biblioteca
    artist TEXT, title TEXT, cover TEXT, release_date TEXT, kind TEXT, nb_tracks INTEGER,
    found_at INTEGER, dismissed INTEGER DEFAULT 0, queued INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS release_artists (
    artist_id INTEGER PRIMARY KEY, dz_id INTEGER, checked_at INTEGER
);
"""
state = {"running": False, "last_run": None, "checked": 0, "found": 0}
_lock = threading.Lock()


def init():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


def _dz_artist(conn, artist_id, name):
    r = conn.execute("SELECT dz_id FROM release_artists WHERE artist_id=?", (artist_id,)).fetchone()
    if r and r[0]:
        return r[0]
    found = discover._get("/search/artist", {"q": name, "limit": 5}, ttl=86400).get("data") or []
    want = dedupe.norm(name)
    art = next((x for x in found if dedupe.norm(x.get("name", "")) == want), None)
    dz = art["id"] if art else 0
    conn.execute("INSERT INTO release_artists(artist_id, dz_id, checked_at) VALUES (?,?,?) "
                 "ON CONFLICT(artist_id) DO UPDATE SET dz_id=excluded.dz_id", (artist_id, dz, int(time.time())))
    return dz


def _library_titles(conn, artist_id):
    return {dedupe.norm(r[0]) for r in conn.execute(
        """SELECT al.title FROM albums al WHERE al.artist_id=?
           UNION SELECT t.title FROM tracks t WHERE t.artist_id=?""", (artist_id, artist_id))}


def check(max_artists=200, pause=0.35):
    """Procura lançamentos novos. Devolve quantos encontrou."""
    if not _lock.acquire(blocking=False):
        return 0
    conn = get_conn()
    found = 0
    try:
        state.update(running=True, checked=0)
        since = (dt.date.today() - dt.timedelta(days=RECENT_DAYS)).isoformat()
        artists = conn.execute("""SELECT a.id, a.name, COUNT(t.id) n FROM artists a JOIN tracks t ON t.artist_id=a.id
                                  GROUP BY a.id ORDER BY n DESC LIMIT ?""", (max_artists,)).fetchall()
        auto = app_settings.get_all().get("rel_auto")
        for a in artists:
            try:
                dz = _dz_artist(conn, a["id"], a["name"])
                if not dz:
                    continue
                albums = discover._get(f"/artist/{int(dz)}/albums", {"limit": 15}, ttl=3600).get("data") or []
            except Exception as e:  # noqa — sem rede: tenta na próxima ronda
                log.info("Deezer indisponível (%s)", e)
                break
            have = _library_titles(conn, a["id"])
            for al in albums:
                rd = al.get("release_date") or ""
                if rd < since or rd > dt.date.today().isoformat() or dedupe.norm(al.get("title", "")) in have:
                    continue
                cur = conn.execute(
                    """INSERT OR IGNORE INTO releases(id, artist_id, artist, title, cover, release_date, kind, nb_tracks, found_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (al["id"], a["id"], a["name"], al.get("title", ""), al.get("cover_big") or al.get("cover_medium"),
                     rd, al.get("record_type"), al.get("nb_tracks"), int(time.time())))
                if cur.rowcount:
                    found += 1
                    log.info("Novo lançamento: %s — %s (%s)", a["name"], al.get("title"), rd)
                    if auto:
                        queue(al["id"], conn)
            conn.commit()
            state["checked"] += 1
            time.sleep(pause)
        state.update(last_run=int(time.time()), found=found)
    finally:
        state["running"] = False
        _lock.release()
    return found


def queue(rid, conn=None):
    """Descarrega o lançamento (todas as faixas)."""
    conn = conn or get_conn()
    r = conn.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone()
    d = discover.album_detail(rid)
    items = [{"artist": t["artist"], "title": t["title"], "duration": t["duration"]} for t in d["tracks_list"]]
    job_id, queued, skipped = downloader.enqueue_items(items, f"Novo lançamento: {d['title']} — {d['artist']}")
    if r:
        conn.execute("UPDATE releases SET queued=1 WHERE id=?", (rid,))
        conn.commit()
    return {"job_id": job_id, "queued": queued, "skipped": skipped}


def listing(limit=20):
    """Lançamentos por ouvir: recentes, não dispensados e que ainda não estão na biblioteca."""
    conn = get_conn()
    since = (dt.date.today() - dt.timedelta(days=RECENT_DAYS)).isoformat()
    out = []
    for r in conn.execute("""SELECT * FROM releases WHERE dismissed=0 AND release_date >= ?
                             ORDER BY release_date DESC, found_at DESC LIMIT ?""", (since, limit * 2)).fetchall():
        item = dict(r)
        lib_album = conn.execute("SELECT id FROM albums WHERE artist_id=? AND title=? COLLATE NOCASE",
                                 (r["artist_id"], r["title"])).fetchone()
        if not lib_album:      # single: procura a faixa com o mesmo título
            want = dedupe.norm(r["title"])
            lib_album = next(((t[1],) for t in conn.execute("SELECT title, album_id FROM tracks WHERE artist_id=?",
                                                              (r["artist_id"],)) if dedupe.norm(t[0]) == want), None)
        item["album_id"] = lib_album[0] if lib_album else None     # já descarregado: toca da biblioteca
        cover = conn.execute("""SELECT al.id FROM albums al WHERE al.artist_id=? AND al.cover IS NOT NULL LIMIT 1""",
                             (r["artist_id"],)).fetchone()
        item["artist_cover_album"] = cover[0] if cover else None
        out.append(item)
        if len(out) >= limit:
            break
    return out


def dismiss(rid):
    conn = get_conn()
    conn.execute("UPDATE releases SET dismissed=1 WHERE id=?", (rid,))
    conn.commit()


def loop_once_due():
    hours = app_settings.get_all().get("rel_check_hours", 12) or 12
    last = state["last_run"] or 0
    return time.time() - last >= hours * 3600


def start_check():
    threading.Thread(target=check, daemon=True, name="releases").start()
