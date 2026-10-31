"""Varre a pasta de música, lê as tags com mutagen e mantém o SQLite atualizado."""
import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from .config import AUDIO_EXTS, COVERS_DIR, MUSIC_DIR
from .db import get_conn

log = logging.getLogger("scanner")
_lock = threading.Lock()
state = {"running": False, "last_run": None, "scanned": 0, "added": 0, "removed": 0}

FOLDER_COVERS = ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "album.jpg")


def _first(tags, key, default=""):
    if not tags:
        return default
    v = tags.get(key)
    if not v:
        return default
    v = v[0] if isinstance(v, list) else v
    return str(v).strip() or default


def _int(value):
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) if m else None


def _extract_cover(path: Path):
    """Devolve bytes da capa embutida (ou None)."""
    try:
        ext = path.suffix.lower()
        if ext == ".mp3":
            tags = ID3(path)
            pics = tags.getall("APIC")
            if pics:
                front = [p for p in pics if p.type == 3]
                return (front or pics)[0].data
        elif ext == ".flac":
            pics = FLAC(path).pictures
            if pics:
                return pics[0].data
        elif ext in (".m4a", ".mp4", ".aac"):
            covr = (MP4(path).tags or {}).get("covr")
            if covr:
                return bytes(covr[0])
        else:
            f = mutagen.File(path)
            if f is not None and hasattr(f, "pictures") and f.pictures:
                return f.pictures[0].data
            if f is not None and f.tags and "metadata_block_picture" in f.tags:
                import base64
                from mutagen.flac import Picture
                pic = Picture(base64.b64decode(f.tags["metadata_block_picture"][0]))
                return pic.data
    except Exception:
        pass
    return None


def _save_cover(album_id: int, path: Path):
    target = COVERS_DIR / f"album_{album_id}.jpg"
    if target.exists():
        return target.name
    data = _extract_cover(path)
    if not data:
        for name in FOLDER_COVERS:
            p = path.parent / name
            if p.exists():
                data = p.read_bytes()
                break
    if not data:
        return None
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data)).convert("RGB")
        img.thumbnail((600, 600))
        img.save(target, "JPEG", quality=85)
    except Exception:
        target.write_bytes(data)
    return target.name


def _read_tags(path: Path):
    f = mutagen.File(path, easy=True)
    tags = f.tags if f is not None else None
    stem = path.stem
    # fallback "01 - Titulo" / "Artista - Titulo"
    guess_title = re.sub(r"^\d+\s*[-.]\s*", "", stem)
    title = _first(tags, "title", guess_title)
    artist = _first(tags, "artist", "")
    if not artist:
        depth = len(path.relative_to(MUSIC_DIR).parts)
        artist = path.parent.parent.name if depth >= 3 else "Desconhecido"
    album_artist = _first(tags, "albumartist", "") or artist.split(",")[0].split(";")[0].split(" feat")[0].strip()
    album = _first(tags, "album", path.parent.name if path.parent != MUSIC_DIR else "Singles")
    return {
        "title": title,
        "artist": artist.replace(";", ", "),
        "album_artist": album_artist or "Desconhecido",
        "album": album,
        "track_no": _int(_first(tags, "tracknumber")),
        "disc_no": _int(_first(tags, "discnumber")),
        "year": (_first(tags, "date") or _first(tags, "originaldate"))[:4] or None,
        "duration": round(f.info.length, 2) if f is not None and f.info else None,
    }


def _artist_id(conn, name):
    conn.execute("INSERT OR IGNORE INTO artists(name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM artists WHERE name=?", (name,)).fetchone()[0]


def _album_id(conn, title, artist_id, year):
    conn.execute(
        "INSERT OR IGNORE INTO albums(title, artist_id, year) VALUES (?,?,?)",
        (title, artist_id, year),
    )
    r = conn.execute("SELECT id, year FROM albums WHERE title=? AND artist_id=?", (title, artist_id)).fetchone()
    if year and not r["year"]:
        conn.execute("UPDATE albums SET year=? WHERE id=?", (year, r["id"]))
    return r["id"]


def scan(full=False):
    if not _lock.acquire(blocking=False):
        return state
    try:
        state.update(running=True, scanned=0, added=0, removed=0)
        conn = get_conn()
        known = {r["path"]: r["mtime"] for r in conn.execute("SELECT path, mtime FROM tracks")}
        seen = set()
        for root, _dirs, files in os.walk(MUSIC_DIR):
            for fn in files:
                p = Path(root) / fn
                if p.suffix.lower() not in AUDIO_EXTS or fn.startswith("."):
                    continue
                rel = str(p.relative_to(MUSIC_DIR))
                seen.add(rel)
                state["scanned"] += 1
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not full and rel in known and abs((known[rel] or 0) - st.st_mtime) < 1:
                    continue
                try:
                    t = _read_tags(p)
                except Exception as e:
                    log.warning("Falha a ler %s: %s", rel, e)
                    continue
                aa_id = _artist_id(conn, t["album_artist"])
                main_artist = t["artist"].split(",")[0].strip() or t["album_artist"]
                ar_id = _artist_id(conn, main_artist)
                al_id = _album_id(conn, t["album"], aa_id, t["year"])
                if rel in known:
                    conn.execute(
                        """UPDATE tracks SET title=?, artist=?, artist_id=?, album_id=?, track_no=?, disc_no=?,
                           duration=?, size=?, mtime=? WHERE path=?""",
                        (t["title"], t["artist"], ar_id, al_id, t["track_no"], t["disc_no"],
                         t["duration"], st.st_size, st.st_mtime, rel),
                    )
                else:
                    conn.execute(
                        """INSERT INTO tracks(path, title, artist, artist_id, album_id, track_no, disc_no,
                           duration, size, mtime) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (rel, t["title"], t["artist"], ar_id, al_id, t["track_no"], t["disc_no"],
                         t["duration"], st.st_size, st.st_mtime),
                    )
                    state["added"] += 1
                cover = conn.execute("SELECT cover FROM albums WHERE id=?", (al_id,)).fetchone()[0]
                if not cover:
                    name = _save_cover(al_id, p)
                    if name:
                        conn.execute("UPDATE albums SET cover=? WHERE id=?", (name, al_id))
                if state["scanned"] % 200 == 0:
                    conn.commit()
        gone = [p for p in known if p not in seen]
        for chunk in range(0, len(gone), 500):
            part = gone[chunk:chunk + 500]
            conn.execute(f"DELETE FROM tracks WHERE path IN ({','.join('?' * len(part))})", part)
        state["removed"] = len(gone)
        # limpar órfãos
        orphan_albums = [r[0] for r in conn.execute(
            "SELECT id FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM tracks WHERE album_id IS NOT NULL)")]
        for aid in orphan_albums:
            (COVERS_DIR / f"album_{aid}.jpg").unlink(missing_ok=True)
        conn.execute("DELETE FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM tracks WHERE album_id IS NOT NULL)")
        conn.execute("""DELETE FROM artists WHERE id NOT IN (SELECT DISTINCT artist_id FROM tracks WHERE artist_id IS NOT NULL)
                        AND id NOT IN (SELECT artist_id FROM albums)""")
        conn.commit()
        state["last_run"] = int(time.time())
        log.info("Scan concluído: %s", state)
    finally:
        state["running"] = False
        _lock.release()
    return state
