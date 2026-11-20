"""Identificar músicas pelo microfone (como o Shazam).

O browser grava alguns segundos e envia o áudio. Aqui é convertido para WAV mono e é gerada a
"impressão digital" do som, que é comparada com o catálogo do Shazam (biblioteca shazamio).
A música encontrada é depois procurada no Deezer para ter capa, prévia de 30 s e download.
"""
import asyncio
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from . import dedupe, discover
from .db import get_conn

log = logging.getLogger("recognize")

SCHEMA = """
CREATE TABLE IF NOT EXISTS recognitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, artist TEXT, cover TEXT, dz_id INTEGER, shazam_url TEXT, at INTEGER, user_id INTEGER DEFAULT 1
);
"""


def init():
    conn = get_conn()
    conn.executescript(SCHEMA)
    if "user_id" not in {r[1] for r in conn.execute("PRAGMA table_info(recognitions)")}:
        conn.execute("ALTER TABLE recognitions ADD COLUMN user_id INTEGER DEFAULT 1")
    conn.commit()


def _to_wav(raw: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in", Path(tmp) / "out.wav"
        src.write_bytes(raw)
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
                            "-t", "25", str(dst)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not dst.exists():
            raise RuntimeError("não consegui ler o áudio gravado")
        return dst.read_bytes()


async def _shazam(wav: bytes):
    from shazamio import Shazam   # importado só quando é preciso
    shz = Shazam()
    try:
        out = await shz.recognize(wav)
    finally:
        close = getattr(shz, "close", None)
        if close:
            try:
                await close()
            except Exception:  # noqa
                pass
    t = out.get("track") if isinstance(out, dict) else None
    if not t:
        return None
    imgs = t.get("images") or {}
    return {"title": t.get("title") or "", "artist": t.get("subtitle") or "",
            "cover": imgs.get("coverarthq") or imgs.get("coverart"), "shazam_url": t.get("url")}


def _deezer_match(title, artist):
    """A mesma música no Deezer (prévia, capa, download) e na biblioteca."""
    main = dedupe.primary_artist(artist) or artist
    res = discover._get("/search", {"q": f'artist:"{main}" track:"{title}"', "limit": 5}, ttl=3600).get("data") or []
    if not res:
        res = discover._get("/search", {"q": f"{artist} {title}", "limit": 5}, ttl=3600).get("data") or []
    want = dedupe.key_for(artist, title)
    best = next((t for t in res if dedupe.key_for((t.get("artist") or {}).get("name", ""), t.get("title", "")) == want), None)
    best = best or (res[0] if res else None)
    return discover.mark_library([discover._track(best)])[0] if best else None


def _library_id(title, artist, duration):
    hits = [r for r in get_conn().execute("SELECT id, title, artist, duration FROM tracks")
            if dedupe.key_for(r["artist"], r["title"]) == dedupe.key_for(artist, title)]
    best = next((r for r in hits if dedupe._close(duration, r["duration"])), hits[0] if hits else None)
    return best["id"] if best else None


async def identify(raw: bytes):
    wav = await asyncio.to_thread(_to_wav, raw)
    found = await _shazam(wav)
    if not found:
        return {"found": False}
    track = None
    try:
        track = await asyncio.to_thread(_deezer_match, found["title"], found["artist"])
    except Exception as e:  # noqa
        log.info("Deezer indisponível: %s", e)
    lib = await asyncio.to_thread(_library_id, found["title"], found["artist"], (track or {}).get("duration"))
    conn = get_conn()
    conn.execute("INSERT INTO recognitions(title, artist, cover, dz_id, shazam_url, at, user_id) VALUES (?,?,?,?,?,?,uid())",
                 (found["title"], found["artist"], (track or {}).get("cover") or found["cover"],
                  (track or {}).get("id"), found["shazam_url"], int(time.time())))
    conn.commit()
    return {"found": True, **found, "track": track, "library_id": lib}


def history(limit=12):
    rows = get_conn().execute("SELECT * FROM recognitions WHERE user_id=uid() ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
