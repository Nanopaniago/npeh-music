import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import downloader, scanner
from .config import APP_PASSWORD, COVERS_DIR, MUSIC_DIR, SCAN_ON_START, SECRET_KEY
from .db import get_conn, init_db, row, rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
STATIC = Path(__file__).parent / "static"
COOKIE = "npeh_session"


@asynccontextmanager
async def lifespan(app):
    init_db()
    tasks = [asyncio.create_task(downloader.worker())]
    if SCAN_ON_START:
        tasks.append(asyncio.create_task(asyncio.to_thread(scanner.scan)))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Pobrefy", lifespan=lifespan, docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- auth
def _token():
    return hmac.new(SECRET_KEY.encode(), f"ok:{APP_PASSWORD}".encode(), hashlib.sha256).hexdigest()


def _authed(request: Request):
    if not APP_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _token())


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    p = request.url.path
    if p.startswith("/api/") and p not in ("/api/login", "/api/me") and not _authed(request):
        return JSONResponse({"detail": "login necessário"}, status_code=401)
    return await call_next(request)


@app.get("/api/me")
def me(request: Request):
    return {"authed": _authed(request), "auth_enabled": bool(APP_PASSWORD)}


@app.post("/api/login")
def login(response: Response, password: str = Body(..., embed=True)):
    if not APP_PASSWORD or hmac.compare_digest(password, APP_PASSWORD):
        response.set_cookie(COOKIE, _token(), max_age=60 * 60 * 24 * 180, httponly=True, samesite="lax")
        return {"ok": True}
    time.sleep(1)
    raise HTTPException(401, "Palavra-passe errada")


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------- helpers
TRACK_SELECT = """
SELECT t.id, t.title, t.artist, t.artist_id, t.album_id, t.track_no, t.disc_no, t.duration, t.added_at,
       al.title AS album, al.cover IS NOT NULL AS has_cover, ar.name AS album_artist,
       (SELECT 1 FROM likes l WHERE l.track_id=t.id) IS NOT NULL AS liked
FROM tracks t
LEFT JOIN albums al ON al.id=t.album_id
LEFT JOIN artists ar ON ar.id=al.artist_id
"""

ALBUM_SELECT = """
SELECT al.id, al.title, al.year, al.artist_id, ar.name AS artist, al.cover IS NOT NULL AS has_cover, al.added_at,
       (SELECT COUNT(*) FROM tracks t WHERE t.album_id=al.id) AS track_count,
       (SELECT SUM(duration) FROM tracks t WHERE t.album_id=al.id) AS duration
FROM albums al JOIN artists ar ON ar.id=al.artist_id
"""


def _fix(items):
    for i in items:
        for k in ("has_cover", "liked"):
            if k in i:
                i[k] = bool(i[k])
    return items


def tracks_q(where="", params=(), order="", limit=None):
    sql = TRACK_SELECT + (f" WHERE {where}" if where else "") + (f" ORDER BY {order}" if order else "")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return _fix(rows(sql, params))


def albums_q(where="", params=(), order="al.title COLLATE NOCASE", limit=None):
    sql = ALBUM_SELECT + (f" WHERE {where}" if where else "") + f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return _fix(rows(sql, params))


def artist_cover_sql():
    return "(SELECT al.id FROM albums al WHERE al.artist_id=a.id AND al.cover IS NOT NULL ORDER BY al.year DESC LIMIT 1)"


# ---------------------------------------------------------------- library
@app.get("/api/home")
def home():
    recent_albums = albums_q(order="al.added_at DESC, al.id DESC", limit=12)
    recently_played = tracks_q(
        "t.id IN (SELECT track_id FROM plays GROUP BY track_id ORDER BY MAX(played_at) DESC LIMIT 12)")
    order = {r["track_id"]: i for i, r in enumerate(rows(
        "SELECT track_id FROM plays GROUP BY track_id ORDER BY MAX(played_at) DESC LIMIT 12"))}
    recently_played.sort(key=lambda t: order.get(t["id"], 99))
    top = tracks_q("t.id IN (SELECT track_id FROM plays WHERE played_at > strftime('%s','now') - 2592000 "
                   "GROUP BY track_id ORDER BY COUNT(*) DESC LIMIT 10)")
    artists = rows(f"""SELECT a.id, a.name, {artist_cover_sql()} AS cover_album,
                       (SELECT COUNT(*) FROM plays p JOIN tracks t ON t.id=p.track_id WHERE t.artist_id=a.id) AS plays
                       FROM artists a WHERE EXISTS (SELECT 1 FROM albums al WHERE al.artist_id=a.id)
                       ORDER BY plays DESC, a.name LIMIT 10""")
    stats = row("""SELECT (SELECT COUNT(*) FROM tracks) AS tracks, (SELECT COUNT(*) FROM albums) AS albums,
                   (SELECT COUNT(*) FROM artists) AS artists, (SELECT COALESCE(SUM(duration),0) FROM tracks) AS duration""")
    return {"recent_albums": recent_albums, "recently_played": recently_played, "top_tracks": top,
            "artists": artists, "playlists": list_playlists(), "stats": stats}


@app.get("/api/albums")
def list_albums(sort: str = "title"):
    order = {"title": "al.title COLLATE NOCASE", "recent": "al.added_at DESC, al.id DESC",
             "artist": "ar.name COLLATE NOCASE, al.year", "year": "al.year DESC"}.get(sort, "al.title COLLATE NOCASE")
    return albums_q(order=order)


@app.get("/api/albums/{album_id}")
def get_album(album_id: int):
    al = albums_q("al.id=?", (album_id,))
    if not al:
        raise HTTPException(404)
    album = al[0]
    album["tracks"] = tracks_q("t.album_id=?", (album_id,),
                               "COALESCE(t.disc_no,1), COALESCE(t.track_no,999), t.title")
    album["more"] = albums_q("al.artist_id=? AND al.id<>?", (album["artist_id"], album_id), limit=8)
    return album


@app.get("/api/artists")
def list_artists():
    return rows(f"""SELECT a.id, a.name, {artist_cover_sql()} AS cover_album,
                    (SELECT COUNT(*) FROM tracks t WHERE t.artist_id=a.id) AS track_count
                    FROM artists a
                    WHERE EXISTS (SELECT 1 FROM tracks t WHERE t.artist_id=a.id)
                       OR EXISTS (SELECT 1 FROM albums al WHERE al.artist_id=a.id)
                    ORDER BY a.name COLLATE NOCASE""")


@app.get("/api/artists/{artist_id}")
def get_artist(artist_id: int):
    a = row(f"SELECT a.id, a.name, {artist_cover_sql()} AS cover_album FROM artists a WHERE a.id=?", (artist_id,))
    if not a:
        raise HTTPException(404)
    name_like = f"%{a['name']}%"
    a["top_tracks"] = tracks_q(
        "t.artist_id=? OR al.artist_id=? OR t.artist LIKE ?", (artist_id, artist_id, name_like),
        "(SELECT COUNT(*) FROM plays p WHERE p.track_id=t.id) DESC, t.added_at DESC", limit=10)
    a["albums"] = albums_q("al.artist_id=?", (artist_id,), "al.year DESC, al.title")
    a["appears_on"] = albums_q(
        "al.artist_id<>? AND al.id IN (SELECT album_id FROM tracks WHERE artist_id=? OR artist LIKE ?)",
        (artist_id, artist_id, name_like), limit=12)
    a["track_count"] = row("SELECT COUNT(*) c FROM tracks t LEFT JOIN albums al ON al.id=t.album_id "
                           "WHERE t.artist_id=? OR al.artist_id=?", (artist_id, artist_id))["c"]
    return a


@app.get("/api/artists/{artist_id}/tracks")
def artist_all_tracks(artist_id: int):
    a = row("SELECT name FROM artists WHERE id=?", (artist_id,))
    if not a:
        raise HTTPException(404)
    return tracks_q("t.artist_id=? OR al.artist_id=? OR t.artist LIKE ?", (artist_id, artist_id, f"%{a['name']}%"),
                    "al.year DESC, al.title, COALESCE(t.disc_no,1), t.track_no")


@app.get("/api/tracks")
def list_tracks(limit: int = 500, offset: int = 0, sort: str = "recent"):
    order = {"recent": "t.added_at DESC, t.id DESC", "title": "t.title COLLATE NOCASE",
             "artist": "t.artist COLLATE NOCASE, al.title, t.track_no"}.get(sort, "t.added_at DESC")
    return tracks_q(order=f"{order} LIMIT {int(limit)} OFFSET {int(offset)}")


@app.get("/api/search")
def search(q: str):
    q = q.strip()
    if not q:
        return {"tracks": [], "albums": [], "artists": [], "playlists": []}
    return _search(q, f"%{q}%")


def _search(q, like):
    tracks = _fix(rows(TRACK_SELECT + " WHERE t.title LIKE ? OR t.artist LIKE ? OR al.title LIKE ? "
                       "ORDER BY CASE WHEN t.title LIKE ? THEN 0 ELSE 1 END, t.title LIMIT 30",
                       (like, like, like, like)))
    return {
        "tracks": tracks,
        "albums": albums_q("al.title LIKE ? OR ar.name LIKE ?", (like, like), limit=12),
        "artists": rows(f"SELECT a.id, a.name, {artist_cover_sql()} AS cover_album FROM artists a "
                        "WHERE a.name LIKE ? ORDER BY a.name LIMIT 12", (like,)),
        "playlists": [p for p in list_playlists() if q.lower() in p["name"].lower()],
    }


# ---------------------------------------------------------------- likes & plays
@app.get("/api/liked")
def liked():
    return tracks_q("t.id IN (SELECT track_id FROM likes)", order="(SELECT liked_at FROM likes WHERE track_id=t.id) DESC")


@app.put("/api/likes/{track_id}")
def like(track_id: int):
    c = get_conn()
    c.execute("INSERT OR IGNORE INTO likes(track_id) VALUES (?)", (track_id,))
    c.commit()
    return {"liked": True}


@app.delete("/api/likes/{track_id}")
def unlike(track_id: int):
    c = get_conn()
    c.execute("DELETE FROM likes WHERE track_id=?", (track_id,))
    c.commit()
    return {"liked": False}


@app.post("/api/plays/{track_id}")
def played(track_id: int):
    c = get_conn()
    c.execute("INSERT INTO plays(track_id) VALUES (?)", (track_id,))
    c.execute("DELETE FROM plays WHERE played_at < strftime('%s','now') - 31536000")
    c.commit()
    return {"ok": True}


# ---------------------------------------------------------------- playlists
def list_playlists():
    pls = rows("""SELECT p.id, p.name, p.description, p.updated_at,
                  (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id=p.id) AS track_count
                  FROM playlists p ORDER BY p.updated_at DESC""")
    for p in pls:
        p["covers"] = [r["album_id"] for r in rows(
            """SELECT DISTINCT t.album_id FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id
               JOIN albums al ON al.id=t.album_id WHERE pt.playlist_id=? AND al.cover IS NOT NULL
               ORDER BY pt.position LIMIT 4""", (p["id"],))]
    return pls


@app.get("/api/playlists")
def get_playlists():
    return list_playlists()


@app.post("/api/playlists")
def create_playlist(name: str = Body("Nova playlist", embed=True)):
    c = get_conn()
    cur = c.execute("INSERT INTO playlists(name) VALUES (?)", (name.strip() or "Nova playlist",))
    c.commit()
    return get_playlist(cur.lastrowid)


@app.get("/api/playlists/{pid}")
def get_playlist(pid: int):
    p = next((x for x in list_playlists() if x["id"] == pid), None)
    if not p:
        raise HTTPException(404)
    items = _fix(rows(TRACK_SELECT.replace("SELECT t.id,", "SELECT pt.id AS entry_id, pt.added_at AS entry_added, t.id,")
                      + " JOIN playlist_tracks pt ON pt.track_id=t.id WHERE pt.playlist_id=? ORDER BY pt.position",
                      (pid,)))
    p["tracks"] = items
    p["duration"] = sum(t["duration"] or 0 for t in items)
    return p


@app.patch("/api/playlists/{pid}")
def update_playlist(pid: int, data: dict = Body(...)):
    c = get_conn()
    for field in ("name", "description"):
        if field in data:
            c.execute(f"UPDATE playlists SET {field}=?, updated_at=strftime('%s','now') WHERE id=?",
                      (str(data[field])[:200], pid))
    c.commit()
    return get_playlist(pid)


@app.delete("/api/playlists/{pid}")
def delete_playlist(pid: int):
    c = get_conn()
    c.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
    c.execute("DELETE FROM playlists WHERE id=?", (pid,))
    c.commit()
    return {"ok": True}


@app.post("/api/playlists/{pid}/tracks")
def add_to_playlist(pid: int, track_ids: list[int] = Body(..., embed=True)):
    c = get_conn()
    if not c.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone():
        raise HTTPException(404)
    pos = c.execute("SELECT COALESCE(MAX(position),0) FROM playlist_tracks WHERE playlist_id=?", (pid,)).fetchone()[0]
    for tid in track_ids:
        pos += 1
        c.execute("INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES (?,?,?)", (pid, tid, pos))
    c.execute("UPDATE playlists SET updated_at=strftime('%s','now') WHERE id=?", (pid,))
    c.commit()
    return {"ok": True, "added": len(track_ids)}


@app.delete("/api/playlists/{pid}/entries/{entry_id}")
def remove_from_playlist(pid: int, entry_id: int):
    c = get_conn()
    c.execute("DELETE FROM playlist_tracks WHERE id=? AND playlist_id=?", (entry_id, pid))
    c.execute("UPDATE playlists SET updated_at=strftime('%s','now') WHERE id=?", (pid,))
    c.commit()
    return {"ok": True}


@app.post("/api/playlists/{pid}/reorder")
def reorder_playlist(pid: int, entry_ids: list[int] = Body(..., embed=True)):
    c = get_conn()
    for i, eid in enumerate(entry_ids, 1):
        c.execute("UPDATE playlist_tracks SET position=? WHERE id=? AND playlist_id=?", (i, eid, pid))
    c.commit()
    return {"ok": True}


# ---------------------------------------------------------------- media
@app.get("/api/cover/{album_id}")
def cover(album_id: int):
    p = COVERS_DIR / f"album_{album_id}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


CHUNK = 256 * 1024


@app.get("/api/stream/{track_id}")
def stream(track_id: int, request: Request):
    t = row("SELECT path FROM tracks WHERE id=?", (track_id,))
    if not t:
        raise HTTPException(404)
    path = (MUSIC_DIR / t["path"]).resolve()
    if MUSIC_DIR not in path.parents or not path.exists():
        raise HTTPException(404)
    size = path.stat().st_size
    ctype = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    if path.suffix.lower() == ".opus":
        ctype = "audio/ogg"
    start, end = 0, size - 1
    rng = request.headers.get("range")
    status = 200
    if rng and rng.startswith("bytes="):
        s, _, e = rng[6:].split(",")[0].partition("-")
        try:
            if s:
                start = int(s)
                end = int(e) if e else size - 1
            else:
                start = size - int(e)
        except ValueError:
            raise HTTPException(416)
        end = min(end, size - 1)
        if start > end or start < 0:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                data = f.read(min(CHUNK, left))
                if not data:
                    break
                left -= len(data)
                yield data

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(gen(), status_code=status, media_type=ctype, headers=headers)


# ---------------------------------------------------------------- downloads & scan
@app.post("/api/downloads")
def new_download(query: str = Body(..., embed=True)):
    q = query.strip()
    if not q:
        raise HTTPException(400, "Link ou pesquisa vazia")
    ids = [downloader.enqueue(part.strip()) for part in q.splitlines() if part.strip()]
    return {"ids": ids}


@app.get("/api/downloads")
def list_downloads():
    jobs = rows("SELECT id, query, status, total, done, failed, created_at, finished_at, "
                "substr(log, -1500) AS log_tail FROM jobs ORDER BY id DESC LIMIT 50")
    return {"jobs": jobs, "spotdl": downloader.spotdl_available()}


@app.delete("/api/downloads/{job_id}")
def cancel_download(job_id: int):
    j = row("SELECT status FROM jobs WHERE id=?", (job_id,))
    if not j:
        raise HTTPException(404)
    if j["status"] in ("queued", "running"):
        downloader.cancel(job_id)
    else:
        c = get_conn()
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        c.commit()
    return {"ok": True}


@app.post("/api/downloads/clear")
def clear_downloads():
    c = get_conn()
    c.execute("DELETE FROM jobs WHERE status NOT IN ('queued','running')")
    c.commit()
    return {"ok": True}


@app.post("/api/scan")
async def rescan(full: bool = False):
    if not scanner.state["running"]:
        asyncio.create_task(asyncio.to_thread(scanner.scan, full))
    return {"started": True}


@app.get("/api/scan")
def scan_status():
    return scanner.state


# ---------------------------------------------------------------- frontend
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/{full_path:path}")
def spa(full_path: str):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
